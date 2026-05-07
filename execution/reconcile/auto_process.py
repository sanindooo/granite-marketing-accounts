"""Auto-process trigger for transaction-matched emails.

When a bank transaction matches an unprocessed email with an inline invoice,
this module triggers email processing and links the resulting invoice to the
transaction via reconciliation_rows.

This is the bridge between transaction_matcher (U3) and the email processing
pipeline (processor.py), enabling automatic invoice capture during
bank-statement-anchored reconciliation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from execution.reconcile.state import (
    RowState,
    Trigger,
    append_history,
    compute_row_id,
    transition,
)
from execution.reconcile.transaction_matcher import (
    EmailMatchType,
    TransactionMatchResult,
)
from execution.shared.fiscal import fy_of

if TYPE_CHECKING:
    from execution.adapters.ms365 import Ms365Adapter
    from execution.shared.llm_client import LLMClient
    from execution.shared.prompts import LoadedPrompt
    from execution.shared.sheet import GoogleClients


@dataclass(frozen=True, slots=True)
class AutoProcessResult:
    """Result of attempting to auto-process a matched email."""

    success: bool
    invoice_id: str | None = None
    error: str | None = None
    already_processed: bool = False


def auto_process_matched_email(
    conn: sqlite3.Connection,
    match_result: TransactionMatchResult,
    *,
    adapter: Ms365Adapter,
    llm_client: LLMClient,
    google: GoogleClients,
    classifier_prompt: LoadedPrompt,
    extractor_prompt: LoadedPrompt,
    tmp_root: Path,
    now: datetime | None = None,
) -> AutoProcessResult:
    """Process a matched email and link the resulting invoice to the transaction.

    Args:
        conn: Database connection
        match_result: Result from transaction_matcher containing email match
        adapter: MS365 adapter for fetching email content
        llm_client: LLM client for classification and extraction
        google: Google clients for filing to Drive
        classifier_prompt: Loaded classifier prompt
        extractor_prompt: Loaded extractor prompt
        tmp_root: Temporary directory for PDF processing
        now: Optional timestamp (defaults to UTC now)

    Returns:
        AutoProcessResult with success status and invoice_id if created
    """
    now = now or datetime.now(tz=UTC)

    if not match_result.email_msg_id:
        return AutoProcessResult(success=False, error="no email match in result")

    if match_result.email_match_type != EmailMatchType.INLINE_INVOICE:
        if match_result.email_match_type == EmailMatchType.NEEDS_EVALUATION:
            error_msg = "email attachment type unknown - needs manual review"
        else:
            error_msg = f"email match type '{match_result.email_match_type}' requires manual download"
        return AutoProcessResult(
            success=False,
            error=error_msg,
        )

    msg_id = match_result.email_msg_id

    # Check if email is already processed
    existing = conn.execute(
        "SELECT processed_at, outcome FROM emails WHERE msg_id = ?", (msg_id,)
    ).fetchone()

    if existing is None:
        return AutoProcessResult(success=False, error=f"email {msg_id} not found")

    if existing["processed_at"] is not None:
        # Email already processed - check if there's an invoice we can link
        invoice_id = _find_invoice_for_email(conn, msg_id)
        if invoice_id:
            _link_transaction_to_invoice(
                conn,
                txn_id=match_result.txn_id,
                invoice_id=invoice_id,
                now=now,
                reason="email already processed, linked existing invoice",
            )
            return AutoProcessResult(
                success=True,
                invoice_id=invoice_id,
                already_processed=True,
            )
        return AutoProcessResult(
            success=False,
            error=f"email already processed with outcome '{existing['outcome']}' but no invoice found",
            already_processed=True,
        )

    # Process the email
    from execution.invoice.processor import process_pending_emails

    try:
        stats = process_pending_emails(
            conn,
            adapter=adapter,
            llm_client=llm_client,
            google=google,
            classifier_prompt=classifier_prompt,
            extractor_prompt=extractor_prompt,
            tmp_root=tmp_root,
            msg_ids=[msg_id],
        )
    except Exception as e:
        _flag_transaction_for_review(conn, match_result.txn_id, error=f"processing failed: {e}")
        return AutoProcessResult(success=False, error=str(e))

    if stats.errors > 0:
        error_detail = stats.error_details[0] if stats.error_details else {}
        error_msg = error_detail.get("error", "unknown error during processing")
        _flag_transaction_for_review(conn, match_result.txn_id, error=error_msg)
        return AutoProcessResult(success=False, error=error_msg)

    # Find the invoice created from this email
    invoice_id = _find_invoice_for_email(conn, msg_id)

    if not invoice_id:
        # Processing succeeded but no invoice created (e.g., classified as 'neither')
        return AutoProcessResult(
            success=False,
            error="email processed but no invoice created (may not be an invoice)",
        )

    # Link the transaction to the invoice
    _link_transaction_to_invoice(
        conn,
        txn_id=match_result.txn_id,
        invoice_id=invoice_id,
        now=now,
        reason="auto-processed from matched email",
    )

    return AutoProcessResult(success=True, invoice_id=invoice_id)


def _find_invoice_for_email(conn: sqlite3.Connection, msg_id: str) -> str | None:
    """Find the invoice created from a specific email."""
    row = conn.execute(
        "SELECT invoice_id FROM invoices WHERE source_msg_id = ? AND deleted_at IS NULL",
        (msg_id,),
    ).fetchone()
    return row["invoice_id"] if row else None


def _link_transaction_to_invoice(
    conn: sqlite3.Connection,
    *,
    txn_id: str,
    invoice_id: str,
    now: datetime,
    reason: str,
) -> None:
    """Create a reconciliation_row linking a transaction to an invoice.

    Sets state to AUTO_MATCHED since this is from automatic processing.
    """
    # Get invoice date for fiscal year
    inv_row = conn.execute(
        "SELECT invoice_date, amount_gross FROM invoices WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchone()

    if inv_row is None:
        return

    invoice_date_str = inv_row["invoice_date"]
    amount_gross = Decimal(inv_row["amount_gross"]) if inv_row["amount_gross"] else Decimal("0")

    # Parse invoice date
    if invoice_date_str:
        if "T" in invoice_date_str:
            from datetime import datetime as dt

            invoice_date = dt.fromisoformat(invoice_date_str).date()
        else:
            from datetime import date

            invoice_date = date.fromisoformat(invoice_date_str)
        fiscal_year = fy_of(invoice_date)
    else:
        fiscal_year = "FY-UNKNOWN"

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

    target_state = RowState.AUTO_MATCHED

    # Record transition
    if current_state != target_state:
        record = transition(
            current=current_state,
            proposed=target_state,
            trigger=Trigger.SCRIPT,
            at=now,
            note=reason[:120],
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
                "1.00",  # Score 1.0 for auto-processed matches
                reason[:512],
                history,
                now.isoformat(),
            ),
        )

        # Write reconciliation_links entry
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


def _flag_transaction_for_review(conn: sqlite3.Connection, txn_id: str, *, error: str) -> None:
    """Flag a transaction for manual review after auto-processing failed."""
    # Set needs_manual_download flag on the transaction
    with conn:
        conn.execute(
            """
            UPDATE transactions
            SET needs_manual_download = 1
            WHERE txn_id = ? AND deleted_at IS NULL
            """,
            (txn_id,),
        )


__all__ = [
    "AutoProcessResult",
    "auto_process_matched_email",
]
