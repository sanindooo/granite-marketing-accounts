"""Wise→Amex statement-clearing detector.

When the user's Wise or Monzo business account pays an Amex statement,
the clearing debit should **not** count as an expense — it's an
inter-account transfer. Identifying the pair is tricky because the
debit's description varies ("AMEX PAYMENT" / "AMERICAN EXPRESS" / "DD
AMEX"), the amount may drift by pennies across re-postings, and both
sides of the pair live in different adapters.

This module's contract:

- Input: one :class:`StatementClosing` (parsed by
  :mod:`execution.adapters.amex_email`) and a collection of candidate
  Wise/Monzo rows (``txn_type='transfer'`` as tagged by
  :mod:`reconcile.ledger`, dated within a configurable window after
  the statement close).
- Output: a :class:`ClearingMatch` if exactly one candidate satisfies
  the amount tolerance; a :class:`ClearingAmbiguous` when multiple
  candidates match or none match within tolerance.
- Side effect (when a matching store is passed): the matched debit's
  ``status`` stays ``settled`` and its ``category`` is annotated with
  ``'amex_clearing'``. Unconfirmed cases get ``'transfer_unconfirmed'``
  so the Exceptions tab can surface them.

Primary path only. Multi-debit subset-sum and multi-currency clearing
are deferred to Phase 6; both require more real-world data to tune
than we have at this stage.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

from execution.shared.errors import DataQualityError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from execution.adapters.amex_email import StatementClosing

DEFAULT_TOLERANCE: Final[Decimal] = Decimal("0.50")
DEFAULT_WINDOW_DAYS_MIN: Final[int] = 1
DEFAULT_WINDOW_DAYS_MAX: Final[int] = 35


@dataclass(frozen=True, slots=True)
class CandidateDebit:
    """A Wise/Monzo transfer-tagged row eligible for clearing pair match."""

    txn_id: str
    account: str
    booking_date: date
    amount: Decimal  # positive absolute value for a debit


@dataclass(frozen=True, slots=True)
class ClearingMatch:
    """A confirmed Wise→Amex clearing pair."""

    statement_msg_id: str
    debit_txn_id: str
    debit_account: str
    statement_close_date: date
    statement_billed_amount: Decimal
    debit_amount: Decimal
    delta: Decimal


@dataclass(frozen=True, slots=True)
class ClearingAmbiguous:
    """Zero or multiple candidates; surfaced in Exceptions."""

    statement_msg_id: str
    statement_billed_amount: Decimal
    reason: Literal["no_candidate", "multiple_candidates"]
    candidates: tuple[str, ...]


def match_clearing(
    statement: StatementClosing,
    candidates: Sequence[CandidateDebit],
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE,
    window_days_min: int = DEFAULT_WINDOW_DAYS_MIN,
    window_days_max: int = DEFAULT_WINDOW_DAYS_MAX,
) -> ClearingMatch | ClearingAmbiguous:
    """Pair the statement with exactly one Wise/Monzo debit, or surface ambiguity."""
    if tolerance < 0:
        raise DataQualityError(
            "clearing tolerance must be non-negative", source="clearing"
        )

    earliest = statement.statement_close_date + timedelta(days=window_days_min)
    latest = statement.statement_close_date + timedelta(days=window_days_max)

    eligible = [
        c
        for c in candidates
        if earliest <= c.booking_date <= latest
        and abs(c.amount - statement.statement_billed_amount) <= tolerance
    ]

    if len(eligible) == 1:
        debit = eligible[0]
        return ClearingMatch(
            statement_msg_id=statement.source_msg_id,
            debit_txn_id=debit.txn_id,
            debit_account=debit.account,
            statement_close_date=statement.statement_close_date,
            statement_billed_amount=statement.statement_billed_amount,
            debit_amount=debit.amount,
            delta=(debit.amount - statement.statement_billed_amount),
        )

    return ClearingAmbiguous(
        statement_msg_id=statement.source_msg_id,
        statement_billed_amount=statement.statement_billed_amount,
        reason="no_candidate" if not eligible else "multiple_candidates",
        candidates=tuple(c.txn_id for c in eligible),
    )


def apply_clearing_result(
    conn: sqlite3.Connection,
    result: ClearingMatch | ClearingAmbiguous,
) -> None:
    """Annotate the ``transactions`` table with the clearing decision."""
    with conn:
        if isinstance(result, ClearingMatch):
            conn.execute(
                """
                UPDATE transactions
                   SET category = COALESCE(category, 'amex_clearing')
                 WHERE txn_id = ?
                """,
                (result.debit_txn_id,),
            )
            return
        for txn_id in result.candidates:
            conn.execute(
                """
                UPDATE transactions
                   SET category = 'transfer_unconfirmed'
                 WHERE txn_id = ?
                """,
                (txn_id,),
            )


def fetch_candidates(
    conn: sqlite3.Connection,
    *,
    accounts: tuple[str, ...] = ("wise", "monzo"),
    window_days_max: int = DEFAULT_WINDOW_DAYS_MAX,
    statement_close_date: date,
) -> list[CandidateDebit]:
    """Pull candidate transfer-tagged debits from the ``transactions`` table."""
    earliest = statement_close_date + timedelta(days=DEFAULT_WINDOW_DAYS_MIN)
    latest = statement_close_date + timedelta(days=window_days_max)
    placeholders = ",".join("?" for _ in accounts)
    # placeholders is built exclusively from the literal ``?`` character
    # per account; no user input reaches the string. The account values
    # themselves flow through the parameter binding.
    query = (
        "SELECT txn_id, account, booking_date, amount "  # noqa: S608
        "FROM transactions "
        "WHERE txn_type = 'transfer' "
        f"AND account IN ({placeholders}) "
        "AND deleted_at IS NULL "
        "AND booking_date BETWEEN ? AND ?"
    )
    rows = conn.execute(
        query,
        (*accounts, earliest.isoformat(), latest.isoformat()),
    ).fetchall()
    return [
        CandidateDebit(
            txn_id=row["txn_id"],
            account=row["account"],
            booking_date=date.fromisoformat(row["booking_date"]),
            amount=abs(Decimal(row["amount"])),
        )
        for row in rows
    ]


__all__ = [
    "DEFAULT_TOLERANCE",
    "DEFAULT_WINDOW_DAYS_MAX",
    "DEFAULT_WINDOW_DAYS_MIN",
    "CandidateDebit",
    "ClearingAmbiguous",
    "ClearingMatch",
    "apply_clearing_result",
    "fetch_candidates",
    "match_clearing",
]
