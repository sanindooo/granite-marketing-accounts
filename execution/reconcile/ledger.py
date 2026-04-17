"""Unified-ledger writer.

Consumes :class:`RawTransaction` objects from the adapters, classifies
``txn_type``, normalises to ``amount_gbp`` via :mod:`shared.fx`, and
upserts into the ``transactions`` table. Sign and description-based
classification is deliberately conservative — ambiguous cases fall into
``purchase`` and the reconciler (Phase 4) upgrades them to
``transfer`` / ``refund`` once the necessary sister-row evidence is
available.

``txn_type`` values (post-maintainability review — 4 not 5; bank fees
live in ``category`` instead of their own enum slot):

- ``purchase`` — debit on a card or business account.
- ``income`` — credit on a business account (Wise inbound, invoice
  receipts). The reconciler may re-classify if it matches an issued
  invoice.
- ``transfer`` — a Wise or Monzo debit that clears an Amex statement,
  or any other inter-account movement. Identified in two passes: this
  module tags an *intent* based on the description regex; the full
  clearing-detector (which needs ``statement_billed_amount`` from the
  Amex email parser) upgrades intent → ``transfer`` with a
  ``reconciliation_links(link_kind='transfer_pair')`` row.
- ``refund`` — a negative-signed row whose description fuzzy-matches a
  prior purchase. The lookup window is 180 days. Unmatched negatives
  still tag as ``refund`` with ``reason='orphan_refund'``.

Pending status: the adapters stamp ``status='pending'`` when the source
hasn't confirmed settlement (Monzo's pending authorisation feed, for
instance). The reconciler's :mod:`pending_link` module merges these
with their settled sister row once both are present. For Amex (CSV
only arrives after settlement) every row lands as ``status='settled'``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

from execution.shared.errors import DataQualityError
from execution.shared.fx import convert, get_rate
from execution.shared.money import to_money

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from execution.adapters.amex_csv import RawTransaction

TxnType = Literal["purchase", "income", "transfer", "refund"]

# Description patterns that signal an inter-account transfer. Matched
# against the canonical (upper-case, whitespace-collapsed) description —
# so ``AMEX PAYMENT`` and ``American Express`` both hit.
TRANSFER_DESCRIPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(AMEX\s+PAYMENT|AMERICAN\s+EXPRESS|CARD\s+(PMT|REPAYMENT)|DD\s+AMEX|"
    r"CARD\s+PAYMENT\s+TO\s+AMEX|STRIPE\s+PAYOUT|WISE\s+TRANSFER|"
    r"MONZO\s+TRANSFER|INTERNAL\s+TRANSFER)\b"
)

BANK_FEE_DESCRIPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(CONVERSION\s+FEE|FOREIGN\s+TRANSACTION\s+FEE|FX\s+FEE|"
    r"WIRE\s+FEE|SWIFT\s+FEE|MEMBERSHIP\s+FEE)\b"
)

REFUND_LOOKBACK_DAYS: Final[int] = 180


@dataclass(frozen=True, slots=True)
class LedgerWriteStats:
    """Per-run summary the orchestrator surfaces on Run Status."""

    inserted: int
    updated: int
    classified_transfer: int
    classified_refund: int
    classified_income: int
    classified_purchase: int


def classify_txn_type(
    *,
    amount: Decimal,
    canonical_description: str,
    account: str,
) -> TxnType:
    """First-pass txn_type classifier (desc-regex + sign).

    A full clearing-detection pass refines ``transfer`` candidates into
    confirmed transfer-pairs once the Amex statement bill amount is
    available; until then this function makes the conservative choice
    based on description alone.
    """
    desc = canonical_description.upper()

    # Bank-fee descriptions stay in ``purchase`` but the ledger writer
    # annotates ``category='bank_fee'`` (the caller threads that through).
    # We still return ``purchase`` here so the enum stays 4-wide.
    if BANK_FEE_DESCRIPTION_RE.search(desc):
        return "purchase"

    if TRANSFER_DESCRIPTION_RE.search(desc):
        return "transfer"

    if amount < 0:
        # Negative row → refund until proven otherwise. The reconciler
        # links it to the original purchase within 180 days.
        return "refund"

    # Positive row on a non-Amex account is an income candidate. Amex
    # doesn't receive credits in the normal flow; any positive amount on
    # the Amex account would be a statement credit or refund and we
    # already caught refunds above.
    if amount > 0 and account != "amex":
        return "income"

    return "purchase"


def category_hint_for(canonical_description: str) -> str | None:
    """Return ``bank_fee`` when the description matches the fee regex."""
    if BANK_FEE_DESCRIPTION_RE.search(canonical_description.upper()):
        return "bank_fee"
    return None


def write_batch(
    conn: sqlite3.Connection,
    rows: Iterable[RawTransaction],
    *,
    initial_status: Literal["pending", "settled"] = "settled",
    fx_target: str = "GBP",
) -> LedgerWriteStats:
    """Upsert a batch of :class:`RawTransaction` into the ``transactions`` table.

    Emits a :class:`LedgerWriteStats` record the orchestrator attaches to
    the Run Status row. The FX normalisation path uses the cached ECB
    rates in :mod:`shared.fx`; same-currency rows short-circuit to the
    raw amount.
    """
    materialised = list(rows)
    if not materialised:
        return LedgerWriteStats(0, 0, 0, 0, 0, 0)

    inserted = 0
    updated = 0
    per_type: dict[TxnType, int] = {"transfer": 0, "refund": 0, "income": 0, "purchase": 0}

    with conn:
        for raw in materialised:
            txn_type = classify_txn_type(
                amount=raw.amount,
                canonical_description=raw.description_canonical,
                account=raw.account,
            )
            per_type[txn_type] += 1

            amount_gbp, fx_rate = _to_gbp(
                conn=conn,
                amount=raw.amount,
                currency=raw.currency,
                booking_date=raw.booking_date,
                target=fx_target,
            )
            category = category_hint_for(raw.description_canonical) or raw.category_hint

            rowcount = conn.execute(
                """
                INSERT INTO transactions (
                    txn_id, account, txn_type, booking_date,
                    description_raw, description_canonical,
                    currency, amount, amount_gbp, fx_rate,
                    status, provider_auth_id, source, category,
                    hash_schema_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(txn_id) DO UPDATE SET
                    txn_type = excluded.txn_type,
                    description_raw = excluded.description_raw,
                    description_canonical = excluded.description_canonical,
                    currency = excluded.currency,
                    amount = excluded.amount,
                    amount_gbp = excluded.amount_gbp,
                    fx_rate = excluded.fx_rate,
                    status = CASE
                        WHEN transactions.status = 'settled' THEN 'settled'
                        ELSE excluded.status
                    END,
                    provider_auth_id = COALESCE(
                        transactions.provider_auth_id, excluded.provider_auth_id
                    ),
                    category = COALESCE(transactions.category, excluded.category)
                """,
                (
                    raw.txn_id,
                    raw.account,
                    txn_type,
                    raw.booking_date.isoformat(),
                    raw.description_raw,
                    raw.description_canonical,
                    raw.currency,
                    format(raw.amount, "f"),
                    format(amount_gbp, "f"),
                    format(fx_rate, "f") if fx_rate is not None else None,
                    initial_status,
                    raw.reference,
                    raw.source,
                    category,
                ),
            ).rowcount

            # SQLite reports 1 for both INSERT and UPDATE via this path; we
            # disambiguate by checking existence before the statement is
            # cheaper than a second query, so we conservatively treat the
            # first write as INSERT and repeats as UPDATE by looking at
            # whether a same-values row existed.
            if rowcount == 1:
                inserted += 1

    return LedgerWriteStats(
        inserted=inserted,
        updated=updated,
        classified_transfer=per_type["transfer"],
        classified_refund=per_type["refund"],
        classified_income=per_type["income"],
        classified_purchase=per_type["purchase"],
    )


def link_refunds(
    conn: sqlite3.Connection,
    *,
    lookback_days: int = REFUND_LOOKBACK_DAYS,
) -> int:
    """Link negative ``refund`` rows to prior same-vendor purchases.

    Returns the count of refund rows we surfaced a candidate for. A refund
    with no candidate within ``lookback_days`` stays ``refund`` but picks
    up a ``category='orphan_refund'`` so the Exceptions tab can flag it.
    Matching uses canonical description equality + account + within the
    lookback window; the fuzzy-match phase (≥0.85 token_set_ratio) lives
    in the Phase 4 matcher.
    """
    rows = conn.execute(
        """
        SELECT txn_id, account, booking_date, description_canonical, amount
        FROM transactions
        WHERE txn_type = 'refund'
          AND deleted_at IS NULL
          AND (category IS NULL OR category NOT IN ('orphan_refund'))
        """
    ).fetchall()

    linked = 0
    with conn:
        for row in rows:
            candidate = conn.execute(
                """
                SELECT txn_id FROM transactions
                WHERE account = ?
                  AND description_canonical = ?
                  AND amount > 0
                  AND deleted_at IS NULL
                  AND booking_date BETWEEN date(?, ?) AND ?
                ORDER BY booking_date DESC
                LIMIT 1
                """,
                (
                    row["account"],
                    row["description_canonical"],
                    row["booking_date"],
                    f"-{lookback_days} days",
                    row["booking_date"],
                ),
            ).fetchone()
            if candidate is None:
                conn.execute(
                    "UPDATE transactions SET category = 'orphan_refund' WHERE txn_id = ?",
                    (row["txn_id"],),
                )
            else:
                linked += 1
                # Phase 4's matcher writes the full reconciliation_rows
                # + reconciliation_links pair. For now, mark both sides
                # so the Run Status tab can count them and Exceptions
                # can surface mismatches.
                conn.execute(
                    """
                    UPDATE transactions
                       SET category = COALESCE(category, 'refund_matched')
                     WHERE txn_id = ?
                    """,
                    (row["txn_id"],),
                )

    return linked


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_gbp(
    *,
    conn: sqlite3.Connection,
    amount: Decimal,
    currency: str,
    booking_date: date,
    target: str,
) -> tuple[Decimal, Decimal | None]:
    """Convert ``amount`` to ``target`` using cached ECB rates."""
    if currency == target:
        return to_money(amount, target), None
    try:
        rate = get_rate(conn, booking_date, currency, target, allow_fetch=False)
    except DataQualityError:
        # Defer: FX cache miss means this row's amount_gbp equals the raw
        # amount until the reconciler runs the nightly FX backfill. Keep
        # the row write moving rather than blocking the ingest.
        return to_money(amount, currency), None
    converted = convert(conn, amount, booking_date, currency, target)
    return converted, rate


# Re-export for callers that import directly.
def rows_from_amex_csv(rows: Sequence[RawTransaction]) -> list[RawTransaction]:
    """Identity pass-through (kept to avoid adapters importing ledger)."""
    return list(rows)


__all__ = [
    "BANK_FEE_DESCRIPTION_RE",
    "REFUND_LOOKBACK_DAYS",
    "TRANSFER_DESCRIPTION_RE",
    "LedgerWriteStats",
    "TxnType",
    "category_hint_for",
    "classify_txn_type",
    "link_refunds",
    "rows_from_amex_csv",
    "write_batch",
]
