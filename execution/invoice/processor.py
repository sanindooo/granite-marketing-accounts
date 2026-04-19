"""Invoice processing orchestrator — email → classify → extract → file.

Coordinates the full pipeline:

1. Query ``emails`` rows with ``processed_at IS NULL``.
2. For each email, fetch full body + attachments via MS Graph.
3. Run the classifier (Haiku 4.5).
4. If classified as invoice/receipt:
   a. Extract PDF text via pdfplumber (text-path) or base64 vision.
   b. Run the extractor with Haiku → Sonnet escalation.
   c. Assign category (override → domain-hint → LLM fallback).
   d. File PDF to Google Drive + write ``invoices`` row.
5. Update ``emails.processed_at`` and ``emails.outcome``.

Budget controls: per-run ceiling (default £2, backfill £20), per-invoice
token cap, circuit breaker on 3 consecutive budget breaches.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import pdfplumber

from execution.invoice.category import resolve_category
from execution.invoice.classifier import (
    EmailInput,
    classify_email,
)
from execution.invoice.extractor import (
    ExtractorInput,
    extract_invoice,
)
from execution.invoice.filer import (
    FiledInvoice,
    FilerInput,
    FilerOutcome,
    file_invoice,
)
from execution.invoice.pdf_fetcher import FetchOutcome, FetchStatus, fetch_invoice_pdf
from execution.shared.claude_client import ClaudeClient
from execution.shared.clock import now_utc
from execution.shared.errors import BudgetExceededError, PipelineError
from execution.shared.http import SafeHttpClient
from execution.shared.prompts import LoadedPrompt

if TYPE_CHECKING:  # pragma: no cover
    from execution.adapters.ms365 import Ms365Adapter
    from execution.shared.sheet import GoogleClients


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BUDGET_GBP: Decimal = Decimal("2.00")
BACKFILL_BUDGET_GBP: Decimal = Decimal("20.00")
DEFAULT_BATCH_SIZE: int = 50
MAX_PDF_SIZE_BYTES: int = 20 * 1024 * 1024  # 20 MB

_PDF_URL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"https?://[^\s<>\"']+\.pdf\b",
        r"https?://pay\.stripe\.com/[^\s<>\"']+",
        r"https?://invoice\.stripe\.com/[^\s<>\"']+",
        r"https?://[^\s<>\"']*paddle[^\s<>\"']+/invoice[^\s<>\"']*",
        # Billing portal URLs (will return needs_manual_download via login-gated check)
        r"https?://platform\.openai\.com/[^\s<>\"']*billing[^\s<>\"']*",
        r"https?://[^\s<>\"']*\.zoom\.us/[^\s<>\"']*invoice[^\s<>\"']*",
        r"https?://[^\s<>\"']*\.zoom\.us/[^\s<>\"']*billing[^\s<>\"']*",
        r"https?://dashboard\.heroku\.com/[^\s<>\"']*invoice[^\s<>\"']*",
        r"https?://vercel\.com/[^\s<>\"']*billing[^\s<>\"']*",
        r"https?://railway\.app/[^\s<>\"']*billing[^\s<>\"']*",
    ]
)

Outcome = Literal[
    "invoice",
    "receipt",
    "statement",
    "neither",
    "error",
    "no_attachment",
    "needs_manual_download",
    "duplicate_resend",
]


@dataclass
class ProcessStats:
    """Per-run processing summary."""

    processed: int = 0
    classified_invoice: int = 0
    classified_receipt: int = 0
    classified_statement: int = 0
    classified_neither: int = 0
    filed: int = 0
    duplicates: int = 0
    errors: int = 0
    needs_manual_download: int = 0
    cost_gbp: Decimal = Decimal("0.00")
    error_details: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EmailRow:
    """Minimal email data from the DB for processing."""

    msg_id: str
    source_adapter: str
    from_addr: str
    subject: str
    received_at: str


# ---------------------------------------------------------------------------
# Main processor
# ---------------------------------------------------------------------------


def process_pending_emails(
    conn: sqlite3.Connection,
    *,
    adapter: Ms365Adapter,
    claude: ClaudeClient,
    google: GoogleClients,
    classifier_prompt: LoadedPrompt,
    extractor_prompt: LoadedPrompt,
    tmp_root: Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    on_progress: Any | None = None,
) -> ProcessStats:
    """Process all unprocessed emails and return stats.

    Commits each email individually so partial progress is preserved on crash.

    Args:
        on_progress: Optional callback(current, total, detail) called after each email.
    """
    stats = ProcessStats()
    http_client = SafeHttpClient()

    # Load dismissed sender domains for auto-skip (learning from user feedback)
    dismissed_domains = _load_dismissed_domains(conn)

    # Get total count for progress reporting
    total = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE processed_at IS NULL"
    ).fetchone()[0]
    if limit and limit < total:
        total = limit

    try:
        for email_row in _pending_emails(conn, batch_size=batch_size, limit=limit):
            try:
                # Check if sender domain has been frequently dismissed as "not_invoice"
                sender_domain = _extract_domain(email_row.from_addr)
                if sender_domain and sender_domain in dismissed_domains:
                    _update_email_outcome(conn, email_row.msg_id, "neither")
                    stats.processed += 1
                    stats.classified_neither += 1
                    if on_progress:
                        on_progress(stats.processed, total, "Processed: neither (learned)")
                    continue

                outcome, invoice = _process_one(
                    conn=conn,
                    email_row=email_row,
                    adapter=adapter,
                    claude=claude,
                    google=google,
                    classifier_prompt=classifier_prompt,
                    extractor_prompt=extractor_prompt,
                    http_client=http_client,
                    tmp_root=tmp_root,
                )
                _update_email_outcome(conn, email_row.msg_id, outcome)
                stats.processed += 1

                if outcome == "invoice":
                    stats.classified_invoice += 1
                    if invoice:
                        if invoice.outcome == FilerOutcome.DUPLICATE_RESEND:
                            stats.duplicates += 1
                        else:
                            stats.filed += 1
                elif outcome == "receipt":
                    stats.classified_receipt += 1
                    if invoice:
                        stats.filed += 1
                elif outcome == "statement":
                    stats.classified_statement += 1
                elif outcome == "neither":
                    stats.classified_neither += 1
                elif outcome == "needs_manual_download":
                    stats.needs_manual_download += 1

                # Emit progress if callback provided
                if on_progress:
                    on_progress(stats.processed, total, f"Processed: {outcome}")

            except BudgetExceededError:
                raise
            except PipelineError as err:
                stats.errors += 1
                stats.error_details.append(
                    {"msg_id": email_row.msg_id, "error": str(err)}
                )
                _update_email_outcome(
                    conn, email_row.msg_id, "error", error_code=err.error_code
                )
            except Exception as err:
                stats.errors += 1
                stats.error_details.append(
                    {"msg_id": email_row.msg_id, "error": str(err)}
                )
                _update_email_outcome(conn, email_row.msg_id, "error", error_code="unexpected")

        stats.cost_gbp = claude.budget.spent_gbp
        return stats

    finally:
        http_client.close()


def _process_one(
    *,
    conn: sqlite3.Connection,
    email_row: EmailRow,
    adapter: Ms365Adapter,
    claude: ClaudeClient,
    google: GoogleClients,
    classifier_prompt: LoadedPrompt,
    extractor_prompt: LoadedPrompt,
    http_client: SafeHttpClient,
    tmp_root: Path,
) -> tuple[Outcome, FiledInvoice | None]:
    """Process a single email through the full pipeline."""
    # Fetch full body
    body_text = adapter.fetch_message_body(email_row.msg_id)

    # Classify
    email_input = EmailInput(
        subject=email_row.subject,
        sender=email_row.from_addr,
        body=body_text,
    )
    result, _call = classify_email(claude, classifier_prompt, email_input)

    if result.classification not in ("invoice", "receipt"):
        return result.classification, None

    # Fetch attachments
    attachments = adapter.fetch_attachments(email_row.msg_id)
    pdf_attachments = [
        a for a in attachments
        if a.content_type == "application/pdf" or a.name.lower().endswith(".pdf")
    ]

    if not pdf_attachments:
        # Check email body for invoice URLs (Stripe, Paddle, etc.)
        pdf_bytes, fetch_outcome = _try_fetch_pdf_from_body(
            body_text, http_client=http_client
        )
        if pdf_bytes is None:
            if fetch_outcome and fetch_outcome.status == FetchStatus.NEEDS_MANUAL_DOWNLOAD:
                return "needs_manual_download", None
            return "no_attachment", None
        attachment_index = 0
    else:
        pdf_bytes = pdf_attachments[0].content
        attachment_index = 0

    if len(pdf_bytes) > MAX_PDF_SIZE_BYTES:
        return "error", None

    # Extract text from PDF
    source_text = _extract_pdf_text(pdf_bytes)

    # Run extractor
    received_date = _parse_received_date(email_row.received_at)
    extractor_input = ExtractorInput(
        subject=email_row.subject,
        sender=email_row.from_addr,
        source_text=source_text,
        email_received_date=received_date,
    )
    extraction_outcome = extract_invoice(claude, extractor_prompt, extractor_input)
    extraction = extraction_outcome.result

    # Assign category
    category_decision = resolve_category(
        vendor_name=extraction.supplier_name,
        sender_domain=_extract_domain(email_row.from_addr),
    )

    # File to Drive
    filer_input = FilerInput(
        source_msg_id=email_row.msg_id,
        attachment_index=attachment_index,
        pdf_bytes=pdf_bytes,
        extraction=extraction,
        extractor_version=extractor_prompt.version,
        invoice_number_confidence=extraction.field_confidence.invoice_number,
        category=category_decision.category,
        sender_domain=_extract_domain(email_row.from_addr),
        tmp_root=tmp_root,
    )
    filed = file_invoice(google, conn, filer_input)

    return result.classification, filed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pending_emails(
    conn: sqlite3.Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
) -> Iterator[EmailRow]:
    """Yield unprocessed email rows."""
    query = """
        SELECT msg_id, source_adapter, from_addr, subject, received_at
        FROM emails
        WHERE processed_at IS NULL
        ORDER BY received_at ASC
    """
    params: tuple[int, ...] = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)

    cursor = conn.execute(query, params)
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            yield EmailRow(
                msg_id=row[0],
                source_adapter=row[1],
                from_addr=row[2],
                subject=row[3],
                received_at=row[4],
            )


def _update_email_outcome(
    conn: sqlite3.Connection,
    msg_id: str,
    outcome: str,
    *,
    error_code: str | None = None,
) -> None:
    """Mark an email as processed with the given outcome."""
    with conn:
        conn.execute(
            """
            UPDATE emails
            SET processed_at = ?, outcome = ?, error_code = ?
            WHERE msg_id = ?
            """,
            (now_utc().isoformat(), outcome, error_code, msg_id),
        )


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF using pdfplumber."""
    import io

    text_parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[:10]:  # Cap at 10 pages
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
    except Exception:
        return ""
    return "\n\n".join(text_parts)


def _try_fetch_pdf_from_body(
    body_text: str,
    *,
    http_client: SafeHttpClient,
) -> tuple[bytes | None, FetchOutcome | None]:
    """Try to extract and fetch a PDF URL from the email body."""
    for pattern in _PDF_URL_PATTERNS:
        match = pattern.search(body_text)
        if match:
            url = match.group(0).rstrip(".,;:)")
            outcome = fetch_invoice_pdf(url, client=http_client)
            if outcome.status == FetchStatus.OK and outcome.body:
                return outcome.body, outcome
            if outcome.status == FetchStatus.NEEDS_MANUAL_DOWNLOAD:
                return None, outcome
    return None, None


def _extract_domain(email_addr: str) -> str | None:
    """Extract domain from an email address."""
    if "@" not in email_addr:
        return None
    return email_addr.split("@")[-1].lower().strip()


def _load_dismissed_domains(conn: sqlite3.Connection, threshold: int = 2) -> set[str]:
    """Load sender domains that have been dismissed multiple times.

    Returns domains where the user has marked emails as "not_invoice" at least
    `threshold` times. This enables learning from user feedback.
    """
    try:
        rows = conn.execute(
            """
            SELECT sender_domain, COUNT(*) as dismiss_count
            FROM email_feedback
            WHERE feedback_type = 'dismiss' AND feedback_value = 'not_invoice'
            GROUP BY sender_domain
            HAVING dismiss_count >= ?
            """,
            (threshold,),
        ).fetchall()
        return {row[0] for row in rows if row[0]}
    except Exception:
        # Table might not exist yet or other error - return empty set
        return set()


def _parse_received_date(received_at: str) -> date:
    """Parse ISO 8601 datetime string to date.

    Handles both "T" and space-separated formats from MS Graph.
    """
    normalized = received_at.replace(" ", "T").replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).date()


__all__ = [
    "BACKFILL_BUDGET_GBP",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_BUDGET_GBP",
    "Outcome",
    "ProcessStats",
    "process_pending_emails",
]
