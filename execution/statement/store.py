"""Transaction storage with deduplication for bank statement reconciliation.

Stores transactions extracted from bank statements with:
- Stable txn_id computed via SHA-256 of (account, date, description, amount)
- INSERT OR IGNORE deduplication for overlapping statement uploads
- FX conversion for non-GBP transactions using transaction date rates

The reconciliation state machine uses the existing UNMATCHED state for
transactions with no invoice match. The needs_manual_download column
flags emails requiring portal download.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from execution.shared.fx import get_rate_to_gbp
from execution.shared.money import to_money

if TYPE_CHECKING:
    from execution.statement.parser import RawTransaction

# Regex for canonicalizing descriptions - compiled at module level for performance
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_TRAILING_COUNTRY: Final[re.Pattern[str]] = re.compile(r"\s+GB\s*\d*$", re.IGNORECASE)
_TRAILING_REF: Final[re.Pattern[str]] = re.compile(r"\s+[A-Z0-9]{8,12}$")
_UK_CITY_SUFFIX: Final[re.Pattern[str]] = re.compile(
    r"\s+(LONDON|MANCHESTER|BRISTOL|EDINBURGH|GLASGOW|BIRMINGHAM|LEEDS|"
    r"LIVERPOOL|CARDIFF|BELFAST|OXFORD|CAMBRIDGE|BRIGHTON|READING|YORK)"
    r"(\s.*)?$",
    re.IGNORECASE,
)


@dataclass
class StoreResult:
    """Result of storing transactions from a statement."""

    total_count: int
    new_count: int
    duplicate_count: int
    fx_errors: list[str]

    @property
    def success(self) -> bool:
        return self.new_count > 0 or self.duplicate_count > 0


def store_transactions(
    conn: sqlite3.Connection,
    transactions: list[RawTransaction],
    account: str,
    *,
    source: str = "statement_upload",
) -> StoreResult:
    """Store transactions from a parsed statement with deduplication.

    Args:
        conn: Database connection
        transactions: List of parsed transactions from statement
        account: Account identifier (amex, wise, tide, monzo)
        source: Source identifier for provenance tracking

    Returns:
        StoreResult with counts of new vs duplicate transactions
    """
    new_count = 0
    duplicate_count = 0
    fx_errors: list[str] = []

    # Track row ordinals for same-day transactions to ensure unique txn_ids
    ordinal_by_date: dict[date, int] = {}

    for txn in transactions:
        # Get ordinal for this date (disambiguates identical transactions on same day)
        ordinal = ordinal_by_date.get(txn.date, 0)
        ordinal_by_date[txn.date] = ordinal + 1

        # Canonicalize description for stable hashing
        canonical_desc = canonicalize_description(txn.description)

        # Compute stable txn_id
        txn_id = compute_txn_id(
            account=account,
            booking_date=txn.date,
            canonical_description=canonical_desc,
            amount=txn.amount,
            row_ordinal=ordinal,
        )

        # Handle FX conversion for non-GBP transactions
        amount_gbp: Decimal
        fx_rate: Decimal | None = None

        if txn.currency.upper() == "GBP":
            amount_gbp = txn.amount
        else:
            rate, error = get_rate_to_gbp(conn, txn.currency, txn.date.isoformat())
            if rate is not None:
                amount_gbp = to_money(txn.amount * rate, "GBP")
                fx_rate = rate
            else:
                # Store original amount as GBP placeholder, flag for backfill
                amount_gbp = txn.amount
                fx_errors.append(f"{txn_id}: {error}")

        # Determine transaction type from amount sign
        txn_type = "purchase" if txn.amount < 0 else "income"

        # Try to insert (will be ignored if duplicate txn_id)
        result = conn.execute(
            """
            INSERT OR IGNORE INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp, fx_rate,
                status, source, hash_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'settled', ?, 1)
            """,
            (
                txn_id,
                account,
                txn_type,
                txn.date.isoformat(),
                txn.description,
                canonical_desc,
                txn.currency.upper(),
                format(txn.amount, "f"),
                format(amount_gbp, "f"),
                format(fx_rate, "f") if fx_rate else None,
                source,
            ),
        )

        if result.rowcount > 0:
            new_count += 1
        else:
            duplicate_count += 1

    conn.commit()

    return StoreResult(
        total_count=len(transactions),
        new_count=new_count,
        duplicate_count=duplicate_count,
        fx_errors=fx_errors,
    )


def canonicalize_description(raw: str) -> str:
    """Strip city/country/reference noise from a merchant description.

    Produces a stable canonical form for hashing. Changes to this function
    require incrementing hash_schema_version to re-hash historical transactions.
    """
    text = raw.upper().strip()
    text = _WHITESPACE.sub(" ", text)
    text = _TRAILING_COUNTRY.sub("", text)
    text = _TRAILING_REF.sub("", text)
    text = _UK_CITY_SUFFIX.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def compute_txn_id(
    *,
    account: str,
    booking_date: date,
    canonical_description: str,
    amount: Decimal,
    row_ordinal: int,
) -> str:
    """Compute stable transaction ID via SHA-256.

    The ordinal disambiguates identical transactions on the same day
    (e.g., two £3.50 coffees at the same merchant).
    """
    payload = (
        f"{account}\x00"
        f"{booking_date.isoformat()}\x00"
        f"{canonical_description}\x00"
        f"{format(amount, 'f')}\x00"
        f"{row_ordinal}"
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def get_unreconciled_transactions(
    conn: sqlite3.Connection,
    *,
    fiscal_year: str | None = None,
    account: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Get transactions that don't have a reconciliation row yet.

    Args:
        conn: Database connection
        fiscal_year: Optional FY filter (e.g., "FY-2025-26")
        account: Optional account filter (amex, wise, etc.)
        limit: Optional max results

    Returns:
        List of transaction dicts suitable for matching
    """
    from execution.shared.fiscal import fy_bounds

    params: list[str | int] = []
    where_clauses = [
        "t.deleted_at IS NULL",
        "t.status = 'settled'",
    ]

    # Only get transactions without a reconciliation row
    where_clauses.append("""
        NOT EXISTS (
            SELECT 1 FROM reconciliation_rows r
            WHERE r.txn_id = t.txn_id
        )
    """)

    if fiscal_year:
        start, end = fy_bounds(fiscal_year)
        where_clauses.append("DATE(t.booking_date) >= ? AND DATE(t.booking_date) <= ?")
        params.extend([start.isoformat(), end.isoformat()])

    if account:
        where_clauses.append("t.account = ?")
        params.append(account)

    where_sql = " AND ".join(where_clauses)
    # S608: where_clauses built from constant strings with parameterized values
    query = f"""
        SELECT
            t.txn_id,
            t.account,
            t.booking_date,
            t.description_raw,
            t.description_canonical,
            t.currency,
            t.amount,
            t.amount_gbp,
            t.needs_manual_download
        FROM transactions t
        WHERE {where_sql}
        ORDER BY t.booking_date ASC
    """  # noqa: S608

    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def mark_needs_manual_download(
    conn: sqlite3.Connection,
    txn_id: str,
    *,
    needs_download: bool = True,
) -> bool:
    """Set the needs_manual_download flag on a transaction.

    Returns True if the transaction was updated, False if not found.
    """
    result = conn.execute(
        """
        UPDATE transactions
        SET needs_manual_download = ?
        WHERE txn_id = ? AND deleted_at IS NULL
        """,
        (1 if needs_download else 0, txn_id),
    )
    conn.commit()
    return result.rowcount > 0


__all__ = [
    "StoreResult",
    "canonicalize_description",
    "compute_txn_id",
    "get_unreconciled_transactions",
    "mark_needs_manual_download",
    "store_transactions",
]
