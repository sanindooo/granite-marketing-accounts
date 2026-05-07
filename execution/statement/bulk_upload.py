"""Bulk PDF upload for resolving flagged transactions.

Accepts multiple invoice PDFs uploaded manually (e.g., downloaded from billing
portals), extracts invoice data, files to Drive, and matches against
transactions flagged for manual download.

This is the resolution path for transactions with needs_manual_download=true
or UNMATCHED state where the user has obtained the invoice from a portal.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pdfplumber

from execution.invoice.category import resolve_category
from execution.invoice.extractor import ExtractorInput, extract_invoice
from execution.invoice.filer import FilerInput, FilerOutcome, file_invoice
from execution.reconcile.match import (
    InvoiceCandidate,
    MatchPolicy,
    TransactionCandidate,
    score_pair,
)
from execution.reconcile.state import (
    RowState,
    Trigger,
    append_history,
    compute_row_id,
    transition,
)
from execution.shared.fiscal import fy_of

if TYPE_CHECKING:
    from execution.invoice.extractor import ExtractorResult
    from execution.shared.llm_client import LLMClient
    from execution.shared.prompts import LoadedPrompt
    from execution.shared.sheet import GoogleClients


@dataclass
class UploadResult:
    """Result of uploading a single PDF."""

    file_path: str
    success: bool
    invoice_id: str | None = None
    matched_txn_id: str | None = None
    error: str | None = None
    skipped_duplicate: bool = False


@dataclass
class BulkUploadStats:
    """Summary of bulk upload operation."""

    total_files: int = 0
    processed: int = 0
    filed: int = 0
    matched: int = 0
    unmatched: int = 0
    duplicates: int = 0
    errors: int = 0
    results: list[UploadResult] = field(default_factory=list)


def bulk_upload_pdfs(
    conn: sqlite3.Connection,
    pdf_paths: list[Path],
    *,
    llm_client: LLMClient,
    google: GoogleClients,
    extractor_prompt: LoadedPrompt,
    tmp_root: Path,
    fiscal_year: str | None = None,
    now: datetime | None = None,
) -> Iterator[tuple[int, int, str, UploadResult | None]]:
    """Process multiple PDF files and match against flagged transactions.

    Yields progress events (current, total, message, result) for SSE streaming.
    Each yield represents one PDF processed.

    Args:
        conn: Database connection
        pdf_paths: List of PDF file paths to process
        llm_client: LLM client for extraction
        google: Google clients for Drive upload
        extractor_prompt: Loaded extractor prompt
        tmp_root: Temporary directory
        fiscal_year: Optional FY filter for matching
        now: Optional timestamp

    Yields:
        Tuples of (current_index, total_count, status_message, result)
    """
    now = now or datetime.now(tz=UTC)
    total = len(pdf_paths)

    # Get flagged transactions to match against
    flagged_txns = _get_flagged_transactions(conn, fiscal_year=fiscal_year)

    for idx, pdf_path in enumerate(pdf_paths, start=1):
        try:
            result = _process_single_pdf(
                conn=conn,
                pdf_path=pdf_path,
                llm_client=llm_client,
                google=google,
                extractor_prompt=extractor_prompt,
                tmp_root=tmp_root,
                flagged_txns=flagged_txns,
                now=now,
            )
            yield idx, total, f"Processed {pdf_path.name}", result

            # Remove matched transaction from candidates
            if result.matched_txn_id:
                flagged_txns = [t for t in flagged_txns if t.txn_id != result.matched_txn_id]

        except Exception as e:
            error_result = UploadResult(
                file_path=str(pdf_path),
                success=False,
                error=str(e),
            )
            yield idx, total, f"Error processing {pdf_path.name}: {e}", error_result


def process_bulk_upload(
    conn: sqlite3.Connection,
    pdf_paths: list[Path],
    *,
    llm_client: LLMClient,
    google: GoogleClients,
    extractor_prompt: LoadedPrompt,
    tmp_root: Path,
    fiscal_year: str | None = None,
    on_progress: callable | None = None,
) -> BulkUploadStats:
    """Process bulk upload and return summary stats.

    This is the synchronous wrapper for callers who don't need streaming.

    Args:
        conn: Database connection
        pdf_paths: List of PDF file paths
        llm_client: LLM client
        google: Google clients
        extractor_prompt: Loaded prompt
        tmp_root: Temporary directory
        fiscal_year: Optional FY filter
        on_progress: Optional callback(current, total, message)

    Returns:
        BulkUploadStats with summary
    """
    stats = BulkUploadStats(total_files=len(pdf_paths))

    for current, total, message, result in bulk_upload_pdfs(
        conn,
        pdf_paths,
        llm_client=llm_client,
        google=google,
        extractor_prompt=extractor_prompt,
        tmp_root=tmp_root,
        fiscal_year=fiscal_year,
    ):
        if on_progress:
            on_progress(current, total, message)

        if result:
            stats.results.append(result)
            stats.processed += 1

            if result.success:
                if result.skipped_duplicate:
                    stats.duplicates += 1
                else:
                    stats.filed += 1
                    if result.matched_txn_id:
                        stats.matched += 1
                    else:
                        stats.unmatched += 1
            else:
                stats.errors += 1

    return stats


def _process_single_pdf(
    *,
    conn: sqlite3.Connection,
    pdf_path: Path,
    llm_client: LLMClient,
    google: GoogleClients,
    extractor_prompt: LoadedPrompt,
    tmp_root: Path,
    flagged_txns: list[TransactionCandidate],
    now: datetime,
) -> UploadResult:
    """Process a single PDF file."""
    # Read PDF
    if not pdf_path.exists():
        return UploadResult(
            file_path=str(pdf_path),
            success=False,
            error=f"File not found: {pdf_path}",
        )

    pdf_bytes = pdf_path.read_bytes()

    # Generate content hash for synthetic msg_id
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()[:16]

    # Extract text
    source_text = _extract_pdf_text(pdf_bytes)
    if not source_text.strip():
        return UploadResult(
            file_path=str(pdf_path),
            success=False,
            error="Could not extract text from PDF",
        )

    # Run extraction
    extractor_input = ExtractorInput(
        subject=f"Manual upload: {pdf_path.name}",
        sender="manual_upload@localhost",
        source_text=source_text,
        email_received_date=now.date(),
    )
    extraction_outcome = extract_invoice(llm_client, extractor_prompt, extractor_input)
    extraction = extraction_outcome.result

    # Resolve category
    category_decision = resolve_category(
        vendor_name=extraction.supplier_name,
        sender_domain=None,
    )

    # Generate synthetic msg_id for manual uploads
    synthetic_msg_id = f"manual_{content_hash}"

    # File to Drive
    filer_input = FilerInput(
        source_msg_id=synthetic_msg_id,
        attachment_index=0,
        pdf_bytes=pdf_bytes,
        extraction=extraction,
        extractor_version=extractor_prompt.version,
        invoice_number_confidence=extraction.field_confidence.invoice_number,
        category=category_decision.category,
        sender_domain=None,
        tmp_root=tmp_root,
    )

    try:
        filed = file_invoice(google, conn, filer_input)
    except Exception as e:
        return UploadResult(
            file_path=str(pdf_path),
            success=False,
            error=f"Failed to file invoice: {e}",
        )

    if filed.outcome == FilerOutcome.DUPLICATE_RESEND:
        return UploadResult(
            file_path=str(pdf_path),
            success=True,
            invoice_id=filed.invoice_id,
            skipped_duplicate=True,
        )

    # Try to match against flagged transactions
    matched_txn = _match_invoice_to_flagged(
        conn=conn,
        invoice_id=filed.invoice_id,
        extraction=extraction,
        flagged_txns=flagged_txns,
        now=now,
    )

    return UploadResult(
        file_path=str(pdf_path),
        success=True,
        invoice_id=filed.invoice_id,
        matched_txn_id=matched_txn,
    )


def _get_flagged_transactions(
    conn: sqlite3.Connection,
    *,
    fiscal_year: str | None = None,
) -> list[TransactionCandidate]:
    """Get transactions flagged for manual download or unmatched."""
    from execution.shared.fiscal import fy_bounds

    params: list[str] = []
    where_clauses = [
        "t.deleted_at IS NULL",
        "t.status = 'settled'",
        "(t.needs_manual_download = 1 OR NOT EXISTS ("
        "    SELECT 1 FROM reconciliation_rows r "
        "    WHERE r.txn_id = t.txn_id AND r.state NOT IN ('unmatched', 'suggested')"
        "))",
    ]

    if fiscal_year:
        start, end = fy_bounds(fiscal_year)
        where_clauses.append("DATE(t.booking_date) >= ? AND DATE(t.booking_date) <= ?")
        params.extend([start.isoformat(), end.isoformat()])

    where_sql = " AND ".join(where_clauses)
    query = f"""
        SELECT
            t.txn_id,
            t.description_canonical,
            t.booking_date,
            t.currency,
            t.amount,
            t.amount_gbp
        FROM transactions t
        WHERE {where_sql}
        ORDER BY t.booking_date DESC
        LIMIT 500
    """  # noqa: S608

    rows = conn.execute(query, params).fetchall()
    candidates = []
    for row in rows:
        candidates.append(
            TransactionCandidate(
                txn_id=row["txn_id"],
                description_canonical=row["description_canonical"] or "",
                booking_date=date.fromisoformat(row["booking_date"]),
                currency=row["currency"] or "GBP",
                amount=abs(Decimal(row["amount"])),
                amount_gbp=abs(Decimal(row["amount_gbp"])),
            )
        )
    return candidates


def _match_invoice_to_flagged(
    *,
    conn: sqlite3.Connection,
    invoice_id: str,
    extraction: ExtractorResult,
    flagged_txns: list[TransactionCandidate],
    now: datetime,
) -> str | None:
    """Try to match a newly filed invoice to a flagged transaction."""
    # Parse extraction fields to proper types
    invoice_date: date | None = None
    if extraction.invoice_date:
        invoice_date = date.fromisoformat(extraction.invoice_date)

    amount_gross = Decimal(extraction.amount_gross) if extraction.amount_gross else Decimal("0")
    currency = extraction.currency or "GBP"

    amount_gbp: Decimal | None = None
    if currency == "GBP":
        amount_gbp = amount_gross

    inv_candidate = InvoiceCandidate(
        invoice_id=invoice_id,
        supplier_name=extraction.supplier_name or "",
        invoice_date=invoice_date,
        currency=currency,
        amount_gross=amount_gross,
        amount_gbp_converted=amount_gbp,
    )

    # Score against all flagged transactions
    policy = MatchPolicy()
    best_txn: TransactionCandidate | None = None
    best_score = Decimal("-1")

    for txn in flagged_txns:
        # Date proximity check
        if invoice_date:
            delta_days = abs((txn.booking_date - invoice_date).days)
            if delta_days > policy.date_window_days:
                continue

        score, _breakdown = score_pair(inv_candidate, txn, policy=policy)
        if score > best_score:
            best_score = score
            best_txn = txn

    # Link if score is good enough (using suggested threshold for manual uploads)
    if best_txn and best_score >= policy.suggested_threshold:
        _link_invoice_to_transaction(
            conn=conn,
            invoice_id=invoice_id,
            txn_id=best_txn.txn_id,
            invoice_date=invoice_date,
            amount_gross=amount_gross,
            score=best_score,
            now=now,
        )

        # Clear the needs_manual_download flag
        conn.execute(
            "UPDATE transactions SET needs_manual_download = 0 WHERE txn_id = ?",
            (best_txn.txn_id,),
        )
        conn.commit()

        return best_txn.txn_id

    return None


def _link_invoice_to_transaction(
    *,
    conn: sqlite3.Connection,
    invoice_id: str,
    txn_id: str,
    invoice_date: date | None,
    amount_gross: Decimal,
    score: Decimal,
    now: datetime,
) -> None:
    """Create reconciliation row linking invoice to transaction.

    Uses USER_VERIFIED state since this is a manual upload action.
    """
    fiscal_year = fy_of(invoice_date) if invoice_date else "FY-UNKNOWN"
    link_kind = "full"
    row_id = compute_row_id(
        fiscal_year=fiscal_year,
        invoice_id=invoice_id,
        txn_id=txn_id,
        link_kind=link_kind,
    )

    # Check for existing row
    existing = conn.execute(
        "SELECT state, override_history FROM reconciliation_rows WHERE row_id = ?",
        (row_id,),
    ).fetchone()

    if existing is None:
        current_state = RowState.NEW
        history = ""
    else:
        current_state = RowState(existing["state"])
        history = existing["override_history"] or ""

    target_state = RowState.USER_VERIFIED

    if current_state != target_state:
        record = transition(
            current=current_state,
            proposed=target_state,
            trigger=Trigger.USER,
            at=now,
            note=f"manual upload matched with score {score}",
        )
        history = append_history(history, record)

    with conn:
        conn.execute(
            """
            INSERT INTO reconciliation_rows (
                row_id, invoice_id, txn_id, fiscal_year, state,
                match_score, match_reason, override_history, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(row_id) DO UPDATE SET
                state = excluded.state,
                match_score = excluded.match_score,
                match_reason = excluded.match_reason,
                override_history = excluded.override_history,
                updated_at = excluded.updated_at
            """,
            (
                row_id,
                invoice_id,
                txn_id,
                fiscal_year,
                target_state.value,
                format(score, "f"),
                f"manual upload matched with score {score}",
                history,
                now.isoformat(),
            ),
        )

        conn.execute(
            """
            INSERT INTO reconciliation_links (
                row_id, invoice_id, txn_id,
                allocated_amount_gbp, link_kind
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(row_id, COALESCE(invoice_id, ''), COALESCE(txn_id, ''))
            DO UPDATE SET allocated_amount_gbp = excluded.allocated_amount_gbp,
                          link_kind = excluded.link_kind
            """,
            (
                row_id,
                invoice_id,
                txn_id,
                format(amount_gross, "f"),
                link_kind,
            ),
        )


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF using pdfplumber."""
    text_parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[:10]:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
    except Exception:
        return ""
    return "\n\n".join(text_parts)


__all__ = [
    "BulkUploadStats",
    "UploadResult",
    "bulk_upload_pdfs",
    "process_bulk_upload",
]
