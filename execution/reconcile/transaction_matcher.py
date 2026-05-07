"""Transaction-first matcher for bank statement reconciliation.

Matches bank transactions against:
1. Captured invoices (full scoring: amount, date, vendor)
2. Unprocessed emails (sender domain + date proximity only)

This is the inverse of match.py which matches invoices to transactions.
Here we start from bank transactions and find documentation (invoices or emails).

Email matches are candidates for auto-processing (U4), not direct reconciliation.
The email must be processed first to extract an invoice with amount data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from rapidfuzz import fuzz

from execution.reconcile.match import (
    InvoiceCandidate,
    MatchPolicy,
    MatchState,
    ScoreBreakdown,
    TransactionCandidate,
    score_pair,
)

# Date proximity window for email matching (days)
EMAIL_DATE_WINDOW_DAYS: Final[int] = 14

# Minimum vendor similarity for email matching
EMAIL_VENDOR_MIN_SCORE: Final[Decimal] = Decimal("0.50")


class EmailMatchType:
    """Classification of email attachment type."""

    INLINE_INVOICE = "inline_invoice"
    THIRD_PARTY_LINK = "third_party_link"
    NEEDS_EVALUATION = "needs_evaluation"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class TransactionMatchResult:
    """Result of matching a single transaction."""

    txn_id: str
    state: MatchState
    invoice_id: str | None = None
    invoice_score: Decimal | None = None
    invoice_breakdown: ScoreBreakdown | None = None
    email_msg_id: str | None = None
    email_match_type: str | None = None
    reason: str = ""
    needs_manual_download: bool = False


@dataclass(frozen=True, slots=True)
class EmailCandidate:
    """Unprocessed email that might match a transaction."""

    msg_id: str
    from_addr: str
    received_at: date
    has_pdf_attachment: bool
    has_download_link: bool


def match_transaction(
    conn: sqlite3.Connection,
    txn: TransactionCandidate,
    *,
    policy: MatchPolicy | None = None,
    fiscal_year: str | None = None,
) -> TransactionMatchResult:
    """Match a single transaction against invoices and emails.

    Steps:
    1. Search invoices table for candidates (amount tolerance, date window, vendor)
    2. If invoice match ≥0.70, return it
    3. If no invoice match, search emails by sender domain + date
    4. Classify email match as inline_invoice or third_party_link
    5. Return result with appropriate state and flags

    Args:
        conn: Database connection
        txn: Transaction to match
        policy: Optional match policy (uses defaults if not provided)
        fiscal_year: Optional FY filter for invoice search

    Returns:
        TransactionMatchResult with match state and details
    """
    if policy is None:
        policy = MatchPolicy()

    # Step 1: Search invoices
    invoice_candidates = _get_invoice_candidates(conn, txn, policy=policy, fiscal_year=fiscal_year)

    if invoice_candidates:
        # Score each invoice candidate
        # Invoices store positive amounts, transactions store signed amounts
        # Create a normalized transaction for scoring with absolute amount
        txn_for_scoring = TransactionCandidate(
            txn_id=txn.txn_id,
            description_canonical=txn.description_canonical,
            booking_date=txn.booking_date,
            currency=txn.currency,
            amount=abs(txn.amount),  # Use absolute value for comparison
            amount_gbp=abs(txn.amount_gbp),
        )

        best_inv: InvoiceCandidate | None = None
        best_score = Decimal("-1")
        best_breakdown: ScoreBreakdown | None = None

        for inv in invoice_candidates:
            score, breakdown = score_pair(inv, txn_for_scoring, policy=policy)
            if score > best_score:
                best_inv = inv
                best_score = score
                best_breakdown = breakdown

        if best_inv is not None:
            # Determine state based on score
            if best_score >= policy.auto_threshold:
                return TransactionMatchResult(
                    txn_id=txn.txn_id,
                    state=MatchState.AUTO_MATCHED,
                    invoice_id=best_inv.invoice_id,
                    invoice_score=best_score,
                    invoice_breakdown=best_breakdown,
                    reason=f"invoice match score {best_score}",
                )
            elif best_score >= policy.suggested_threshold:
                return TransactionMatchResult(
                    txn_id=txn.txn_id,
                    state=MatchState.SUGGESTED,
                    invoice_id=best_inv.invoice_id,
                    invoice_score=best_score,
                    invoice_breakdown=best_breakdown,
                    reason=f"invoice suggested score {best_score}",
                )

    # Step 2: No good invoice match, search emails
    email_candidates = _get_email_candidates(conn, txn)

    if email_candidates:
        # Score emails by sender domain vs transaction vendor + date
        best_email = _find_best_email_match(txn, email_candidates)

        if best_email is not None:
            match_type = _classify_email_attachment(best_email)

            if match_type == EmailMatchType.THIRD_PARTY_LINK:
                return TransactionMatchResult(
                    txn_id=txn.txn_id,
                    state=MatchState.UNMATCHED,
                    email_msg_id=best_email.msg_id,
                    email_match_type=match_type,
                    reason="email match requires manual download",
                    needs_manual_download=True,
                )
            elif match_type == EmailMatchType.INLINE_INVOICE:
                return TransactionMatchResult(
                    txn_id=txn.txn_id,
                    state=MatchState.UNMATCHED,
                    email_msg_id=best_email.msg_id,
                    email_match_type=match_type,
                    reason="email match with inline invoice (needs processing)",
                )
            else:
                # NEEDS_EVALUATION: email matched but attachment type unknown
                return TransactionMatchResult(
                    txn_id=txn.txn_id,
                    state=MatchState.UNMATCHED,
                    email_msg_id=best_email.msg_id,
                    email_match_type=match_type,
                    reason="email match (attachment type needs evaluation)",
                )

    # No match found
    return TransactionMatchResult(
        txn_id=txn.txn_id,
        state=MatchState.UNMATCHED,
        reason="no invoice or email match found",
    )


def match_all_transactions(
    conn: sqlite3.Connection,
    transactions: list[TransactionCandidate],
    *,
    policy: MatchPolicy | None = None,
    fiscal_year: str | None = None,
) -> list[TransactionMatchResult]:
    """Match multiple transactions.

    Args:
        conn: Database connection
        transactions: List of transactions to match
        policy: Optional match policy
        fiscal_year: Optional FY filter

    Returns:
        List of match results, one per transaction
    """
    results = []
    for txn in transactions:
        result = match_transaction(conn, txn, policy=policy, fiscal_year=fiscal_year)
        results.append(result)
    return results


def _get_invoice_candidates(
    conn: sqlite3.Connection,
    txn: TransactionCandidate,
    *,
    policy: MatchPolicy,
    fiscal_year: str | None = None,
) -> list[InvoiceCandidate]:
    """Get invoices that might match this transaction.

    Filters:
    - Amount within 10% (loose filter, scorer does precise matching)
    - Date within ±14 days of transaction
    - Not deleted
    """
    params: list[str | int] = []
    where_clauses = ["i.deleted_at IS NULL"]

    # Amount tolerance (loose filter - 10% to catch FX variations)
    amount_abs = abs(txn.amount)
    amount_low = amount_abs * Decimal("0.90")
    amount_high = amount_abs * Decimal("1.10")
    where_clauses.append("CAST(i.amount_gross AS REAL) BETWEEN ? AND ?")
    params.extend([str(amount_low), str(amount_high)])

    # Date window
    where_clauses.append("DATE(i.invoice_date) BETWEEN DATE(?, '-14 days') AND DATE(?, '+14 days')")
    params.extend([txn.booking_date.isoformat(), txn.booking_date.isoformat()])

    if fiscal_year:
        from execution.shared.fiscal import fy_bounds

        start, end = fy_bounds(fiscal_year)
        where_clauses.append("DATE(i.invoice_date) >= ? AND DATE(i.invoice_date) <= ?")
        params.extend([start.isoformat(), end.isoformat()])

    where_sql = " AND ".join(where_clauses)
    query = f"""
        SELECT
            i.invoice_id,
            v.canonical_name as supplier_name,
            i.invoice_date,
            i.currency,
            i.amount_gross,
            i.amount_gross_gbp
        FROM invoices i
        JOIN vendors v ON i.vendor_id = v.vendor_id
        WHERE {where_sql}
        ORDER BY i.invoice_date DESC
        LIMIT 50
    """  # noqa: S608

    rows = conn.execute(query, params).fetchall()

    candidates = []
    for row in rows:
        inv_date = date.fromisoformat(row["invoice_date"]) if row["invoice_date"] else None
        amount_gbp = Decimal(row["amount_gross_gbp"]) if row["amount_gross_gbp"] else None
        candidates.append(
            InvoiceCandidate(
                invoice_id=row["invoice_id"],
                supplier_name=row["supplier_name"] or "",
                invoice_date=inv_date,
                currency=row["currency"],
                amount_gross=Decimal(row["amount_gross"]),
                amount_gbp_converted=amount_gbp,
            )
        )

    return candidates


def _get_email_candidates(
    conn: sqlite3.Connection,
    txn: TransactionCandidate,
) -> list[EmailCandidate]:
    """Get unprocessed emails that might match this transaction.

    Searches for emails:
    - Not yet processed
    - Received within ±14 days of transaction
    - Classified as invoice/receipt or not yet classified
    """
    query = """
        SELECT
            msg_id,
            from_addr,
            received_at,
            outcome
        FROM emails
        WHERE processed_at IS NULL
          AND DATE(received_at) BETWEEN DATE(?, '-14 days') AND DATE(?, '+14 days')
        ORDER BY received_at DESC
        LIMIT 100
    """

    rows = conn.execute(
        query, (txn.booking_date.isoformat(), txn.booking_date.isoformat())
    ).fetchall()

    candidates = []
    for row in rows:
        received_date = date.fromisoformat(row["received_at"][:10])
        candidates.append(
            EmailCandidate(
                msg_id=row["msg_id"],
                from_addr=row["from_addr"],
                received_at=received_date,
                has_pdf_attachment=False,  # Will be determined during auto-process
                has_download_link=False,
            )
        )

    return candidates


def _find_best_email_match(
    txn: TransactionCandidate,
    candidates: list[EmailCandidate],
) -> EmailCandidate | None:
    """Find the best matching email based on sender domain vs vendor + date.

    Email matching is simpler than invoice matching:
    - Compare sender domain to transaction description (vendor)
    - Check date proximity
    - No amount scoring (emails don't have stored amounts)
    """
    best_email: EmailCandidate | None = None
    best_score = Decimal("-1")

    for email in candidates:
        # Extract domain from email address
        domain = _extract_domain(email.from_addr)
        if not domain:
            continue

        # Score vendor similarity
        vendor_score = _vendor_match_score(domain, txn.description_canonical)
        if vendor_score < EMAIL_VENDOR_MIN_SCORE:
            continue

        # Score date proximity
        delta_days = abs((txn.booking_date - email.received_at).days)
        if delta_days > EMAIL_DATE_WINDOW_DAYS:
            continue
        date_score = Decimal("1.0") - Decimal(delta_days) / Decimal(EMAIL_DATE_WINDOW_DAYS)

        # Combined score (simple average - emails are just candidates)
        combined = (vendor_score + date_score) / 2

        if combined > best_score:
            best_score = combined
            best_email = email

    return best_email


def _vendor_match_score(domain: str, description: str) -> Decimal:
    """Score how well an email domain matches a transaction description.

    Handles subdomains like billing.zoom.us by extracting the main domain part.
    """
    # Extract meaningful domain parts (handle subdomains)
    # billing.zoom.us -> zoom, anthropic.com -> anthropic
    parts = domain.lower().split(".")
    if len(parts) >= 2:
        # Try the second-to-last part if it's longer (likely the company name)
        # billing.zoom.us -> zoom, mail.anthropic.com -> anthropic
        candidate = parts[-2] if len(parts[-2]) > len(parts[0]) else parts[0]
        # Also try matching against the full domain without TLD
        domain_without_tld = ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
    else:
        candidate = parts[0]
        domain_without_tld = parts[0]

    # Try both the candidate and full domain, take best score
    ratio1 = fuzz.token_set_ratio(candidate.upper(), description.upper())
    ratio2 = fuzz.token_set_ratio(domain_without_tld.upper(), description.upper())
    ratio = max(ratio1, ratio2)

    return (Decimal(str(ratio)) / Decimal("100")).quantize(Decimal("0.0001"))


def _extract_domain(email_addr: str) -> str | None:
    """Extract domain from an email address."""
    if "@" not in email_addr:
        return None
    return email_addr.split("@")[-1].lower().strip()


def _classify_email_attachment(email: EmailCandidate) -> str:
    """Classify email based on available attachment information.

    Returns NEEDS_EVALUATION when attachment type is unknown,
    allowing downstream code to handle the ambiguity explicitly.
    """
    if email.has_pdf_attachment:
        return EmailMatchType.INLINE_INVOICE
    if email.has_download_link:
        return EmailMatchType.THIRD_PARTY_LINK
    return EmailMatchType.NEEDS_EVALUATION


__all__ = [
    "EMAIL_DATE_WINDOW_DAYS",
    "EMAIL_VENDOR_MIN_SCORE",
    "EmailCandidate",
    "EmailMatchType",
    "TransactionMatchResult",
    "match_all_transactions",
    "match_transaction",
]
