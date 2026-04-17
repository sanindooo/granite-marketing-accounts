"""Amex transaction-notification + statement-closing email parser.

Parses two flavours of email that arrive at the user's inbox from
``americanexpress@welcome.aexp.com`` / ``no-reply@amex.co.uk``:

1. **Per-charge notifications** ("You used your card at X for £Y on Z").
   We extract amount, merchant, posted date, and the approval code when
   present. These rows are **preview-only** — they do not count toward
   the ledger until the monthly CSV confirms them. This kills the
   "spoofed notification email inflates the ledger" attack vector.

2. **Statement-closing emails** ("Your statement is ready — balance
   £X"). We extract the ``statement_billed_amount`` and the statement
   close date. These values feed the Wise→Amex clearing detector in
   :mod:`reconcile.pending_link` which pairs a statement's billed
   amount with the debit on the Wise/Monzo account that paid it.

**DMARC** is non-negotiable on both flavours. The caller (typically
the MS 365 adapter) is responsible for passing the
``authentication_results`` header verbatim; we hard-require
``dmarc=pass`` before parsing. Without MS Graph having returned the
header, the caller must reject the email — no fallback.

Everything here is pure: takes the email payload, returns a structured
result, writes no state. The orchestrator decides what to do with the
parsed values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from execution.shared.errors import DataQualityError, SchemaViolationError

SOURCE_ID: Final[str] = "amex_email"

ALLOWED_SENDER_PREFIXES: Final[tuple[str, ...]] = (
    "americanexpress@",
    "no-reply@amex",
    "noreply@amex",
    "statements@amex",
)

# Accepted DMARC outcomes inside an Authentication-Results header.
_DMARC_PASS: Final[re.Pattern[str]] = re.compile(
    r"\bdmarc\s*=\s*pass\b", re.IGNORECASE
)

_TRANSACTION_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:amount|total|charged?|spent|you used your card for)\s*[:\-]?\s*"
    r"£\s*([0-9,]+\.\d{2})",
    re.IGNORECASE,
)
_MERCHANT_RE: Final[re.Pattern[str]] = re.compile(
    r"\bat\s+([A-Z0-9][A-Z0-9\s&\.\-']{2,60}?)(?:\s+on|\s+for|\.)",
    re.IGNORECASE,
)
_DATE_ISO_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(20\d{2}-\d{2}-\d{2})\b"
)
_DATE_DMY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\s+(20\d{2})\b",
    re.IGNORECASE,
)
_APPROVAL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(approval|auth)\s*(?:code)?\s*[:#]?\s*([A-Z0-9]{5,10})\b",
    re.IGNORECASE,
)
_STATEMENT_SUBJECT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(your\s+statement\s+is\s+ready|statement\s+closing|"
    r"new\s+statement\s+available)\b",
    re.IGNORECASE,
)
_STATEMENT_BALANCE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:new\s+balance|total\s+owed|closing\s+balance|statement\s+balance)\s*"
    r"[:\-]?\s*£\s*([0-9,]+\.\d{2})",
    re.IGNORECASE,
)
_STATEMENT_CLOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:statement\s+closing\s+on|closing\s+date|statement\s+date|"
    r"closing\s+on)\s*[:\-]?\s*(\d{1,2}\s+\w+\s+20\d{2}|20\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)

_MONTHS_3: Final[dict[str, int]] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class EmailKind(StrEnum):
    TRANSACTION_NOTIFICATION = "transaction_notification"
    STATEMENT_CLOSING = "statement_closing"
    UNRECOGNISED = "unrecognised"


@dataclass(frozen=True, slots=True)
class TransactionNotification:
    """One per-charge notification email, parsed."""

    source_msg_id: str
    amount: Decimal
    merchant: str
    posted_date: date | None
    approval_code: str | None


@dataclass(frozen=True, slots=True)
class StatementClosing:
    """One statement-closing email, parsed."""

    source_msg_id: str
    statement_billed_amount: Decimal
    statement_close_date: date


def require_dmarc_pass(
    *, authentication_results_header: str | None
) -> None:
    """Raise :class:`SchemaViolationError` unless DMARC passed.

    Called once per email before any parsing work. The caller must fetch
    the ``Authentication-Results`` header explicitly — Graph's default
    ``$select`` does not include it.
    """
    if not authentication_results_header:
        raise SchemaViolationError(
            "Amex email rejected: no Authentication-Results header",
            source=SOURCE_ID,
        )
    if not _DMARC_PASS.search(authentication_results_header):
        raise SchemaViolationError(
            "Amex email rejected: DMARC not pass",
            source=SOURCE_ID,
            details={"header": authentication_results_header[:200]},
        )


def classify_email_kind(*, subject: str, body: str) -> EmailKind:
    """Route to the right parser based on subject + body heuristics."""
    if _STATEMENT_SUBJECT_RE.search(subject) or _STATEMENT_SUBJECT_RE.search(
        body[:500]
    ):
        return EmailKind.STATEMENT_CLOSING
    if (
        _TRANSACTION_AMOUNT_RE.search(body)
        and ("you used your card" in body.lower() or "amount" in body.lower())
    ):
        return EmailKind.TRANSACTION_NOTIFICATION
    return EmailKind.UNRECOGNISED


def parse_transaction_notification(
    *,
    source_msg_id: str,
    subject: str,
    body: str,
    received_date: date,
) -> TransactionNotification:
    """Extract the amount + merchant + date + approval code."""
    del subject  # signal only; amount + merchant live in the body

    amount_match = _TRANSACTION_AMOUNT_RE.search(body)
    if amount_match is None:
        raise DataQualityError(
            "Amex notification body has no amount",
            source=SOURCE_ID,
        )
    try:
        amount = Decimal(amount_match.group(1).replace(",", ""))
    except InvalidOperation as err:
        raise DataQualityError(
            f"Amex notification amount unparseable: {amount_match.group(1)!r}",
            source=SOURCE_ID,
            cause=err,
        ) from err

    merchant_match = _MERCHANT_RE.search(body)
    merchant = (
        merchant_match.group(1).strip().upper()
        if merchant_match
        else "UNKNOWN MERCHANT"
    )

    posted = _parse_date_from_body(body) or received_date
    approval_match = _APPROVAL_RE.search(body)
    approval = approval_match.group(2).upper() if approval_match else None

    return TransactionNotification(
        source_msg_id=source_msg_id,
        amount=amount,
        merchant=merchant,
        posted_date=posted,
        approval_code=approval,
    )


def parse_statement_closing(
    *,
    source_msg_id: str,
    subject: str,
    body: str,
    received_date: date,
) -> StatementClosing:
    """Extract the billed amount + close date."""
    balance_match = _STATEMENT_BALANCE_RE.search(body) or _TRANSACTION_AMOUNT_RE.search(body)
    if balance_match is None:
        raise DataQualityError(
            "Amex statement email has no balance amount",
            source=SOURCE_ID,
            details={"subject": subject},
        )
    try:
        amount = Decimal(balance_match.group(1).replace(",", ""))
    except InvalidOperation as err:
        raise DataQualityError(
            f"Amex statement balance unparseable: {balance_match.group(1)!r}",
            source=SOURCE_ID,
            cause=err,
        ) from err

    close_match = _STATEMENT_CLOSE_RE.search(body)
    close_date = _parse_date_from_body(close_match.group(1)) if close_match else None
    if close_date is None:
        close_date = received_date

    return StatementClosing(
        source_msg_id=source_msg_id,
        statement_billed_amount=amount,
        statement_close_date=close_date,
    )


def _parse_date_from_body(text: str) -> date | None:
    """Find the first parseable date in ``text``. ISO wins."""
    iso = _DATE_ISO_RE.search(text)
    if iso is not None:
        try:
            return datetime.strptime(iso.group(1), "%Y-%m-%d").date()  # noqa: DTZ007
        except ValueError:
            pass
    dmy = _DATE_DMY_RE.search(text)
    if dmy is not None:
        day = int(dmy.group(1))
        month = _MONTHS_3.get(dmy.group(2)[:3].lower())
        year = int(dmy.group(3))
        if month is not None:
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


__all__ = [
    "ALLOWED_SENDER_PREFIXES",
    "SOURCE_ID",
    "EmailKind",
    "StatementClosing",
    "TransactionNotification",
    "classify_email_kind",
    "parse_statement_closing",
    "parse_transaction_notification",
    "require_dmarc_pass",
]
