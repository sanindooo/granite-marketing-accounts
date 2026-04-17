"""Pending → settled link manager.

Monzo's API exposes card authorisations the moment they hit the ledger
(``pending``), with a stable ``id`` / approval code that is later
reused when the charge settles. Wise does the same on card spend. This
module owns the small bit of state that bridges the two events so the
transactions table carries one row per real-world charge instead of
duplicate "pending" + "settled" pairs.

Contract:

- :func:`record_pending` is called when an adapter sees an
  authorisation with ``provider_auth_id``. We write a row into
  ``pending_link`` (primary key is the provider's auth id, not our
  synthetic hash), and stamp the transaction row as
  ``status='pending'``.
- :func:`record_settlement` is called when a settled row lands with
  the same ``provider_auth_id``. We flip the ``pending_link`` row's
  ``settled_txn_id`` + ``settled_at``, update the transaction's
  ``status`` to ``'settled'``, and delete the now-stale pending
  transaction row so Exceptions don't show it twice.
- :func:`flag_stale` surfaces pending rows older than
  :data:`DEFAULT_STALE_DAYS` (14) for user review. Beyond that
  threshold, the authorisation has almost certainly been dropped
  silently by the merchant and the row should not keep counting as
  pending.
- Collision detection: if a second "pending" arrives with the same
  provider_auth_id but a different transaction row, we mark
  ``ambiguous=1`` on the pending_link row. The Exceptions tab
  surfaces these — never silently merge two different transactions
  that happened to share an auth id (can happen with Monzo's auth
  reuse around reversals).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final

from execution.shared.clock import now_utc

DEFAULT_STALE_DAYS: Final[int] = 14


class PendingOutcome(StrEnum):
    RECORDED = "recorded"
    ALREADY_KNOWN = "already_known"
    AMBIGUOUS = "ambiguous"


class SettlementOutcome(StrEnum):
    MERGED = "merged"
    NO_PENDING = "no_pending"
    ALREADY_SETTLED = "already_settled"


@dataclass(frozen=True, slots=True)
class PendingRecord:
    """Result of a :func:`record_pending` call."""

    provider_auth_id: str
    outcome: PendingOutcome
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    """Result of a :func:`record_settlement` call."""

    provider_auth_id: str
    outcome: SettlementOutcome
    settled_txn_id: str | None
    removed_pending_txn_id: str | None


def record_pending(
    conn: sqlite3.Connection,
    *,
    provider_auth_id: str,
    account: str,
    pending_txn_id: str,
) -> PendingRecord:
    """Register a pending authorisation.

    Idempotent on ``provider_auth_id`` + ``pending_txn_id``. If the same
    auth id appears tied to a different ``pending_txn_id``, the row is
    flagged ``ambiguous=1``.
    """
    if not provider_auth_id:
        raise ValueError("provider_auth_id is required")
    now = now_utc().isoformat()
    with conn:
        existing = conn.execute(
            "SELECT pending_txn_id, settled_txn_id, ambiguous "
            "FROM pending_link WHERE provider_auth_id = ?",
            (provider_auth_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO pending_link
                    (provider_auth_id, account, pending_txn_id, first_seen)
                VALUES (?, ?, ?, ?)
                """,
                (provider_auth_id, account, pending_txn_id, now),
            )
            return PendingRecord(
                provider_auth_id=provider_auth_id,
                outcome=PendingOutcome.RECORDED,
                ambiguous=False,
            )
        if existing["pending_txn_id"] == pending_txn_id:
            return PendingRecord(
                provider_auth_id=provider_auth_id,
                outcome=PendingOutcome.ALREADY_KNOWN,
                ambiguous=bool(existing["ambiguous"]),
            )
        # Same auth_id, different pending row → ambiguous.
        conn.execute(
            "UPDATE pending_link SET ambiguous = 1 WHERE provider_auth_id = ?",
            (provider_auth_id,),
        )
        return PendingRecord(
            provider_auth_id=provider_auth_id,
            outcome=PendingOutcome.AMBIGUOUS,
            ambiguous=True,
        )


def record_settlement(
    conn: sqlite3.Connection,
    *,
    provider_auth_id: str,
    settled_txn_id: str,
) -> SettlementRecord:
    """Merge a settled transaction with its pending sister row.

    Flips ``pending_link.settled_txn_id`` / ``settled_at``, flips
    ``transactions.status`` of the pending row to ``settled``, and
    removes the now-stale pending transactions row by soft-delete.
    Returns ``NO_PENDING`` when no prior pending was ever recorded.
    """
    if not provider_auth_id:
        raise ValueError("provider_auth_id is required")
    now = now_utc().isoformat()
    with conn:
        row = conn.execute(
            "SELECT pending_txn_id, settled_txn_id FROM pending_link "
            "WHERE provider_auth_id = ?",
            (provider_auth_id,),
        ).fetchone()
        if row is None:
            return SettlementRecord(
                provider_auth_id=provider_auth_id,
                outcome=SettlementOutcome.NO_PENDING,
                settled_txn_id=None,
                removed_pending_txn_id=None,
            )
        if row["settled_txn_id"]:
            return SettlementRecord(
                provider_auth_id=provider_auth_id,
                outcome=SettlementOutcome.ALREADY_SETTLED,
                settled_txn_id=row["settled_txn_id"],
                removed_pending_txn_id=None,
            )
        conn.execute(
            """
            UPDATE pending_link
               SET settled_txn_id = ?, settled_at = ?
             WHERE provider_auth_id = ?
            """,
            (settled_txn_id, now, provider_auth_id),
        )
        conn.execute(
            """
            UPDATE transactions
               SET status = 'settled'
             WHERE txn_id = ?
            """,
            (settled_txn_id,),
        )
        removed_pending_txn_id: str | None = row["pending_txn_id"]
        if removed_pending_txn_id and removed_pending_txn_id != settled_txn_id:
            conn.execute(
                """
                UPDATE transactions
                   SET deleted_at = ?, deleted_reason = 'merged_into_settled'
                 WHERE txn_id = ?
                """,
                (now, removed_pending_txn_id),
            )
        return SettlementRecord(
            provider_auth_id=provider_auth_id,
            outcome=SettlementOutcome.MERGED,
            settled_txn_id=settled_txn_id,
            removed_pending_txn_id=removed_pending_txn_id,
        )


def flag_stale(
    conn: sqlite3.Connection,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    as_of: date | None = None,
) -> int:
    """Tag every unsettled pending older than ``stale_days`` as ``pending_stale``.

    Returns the count of rows touched. Tagging writes to
    ``transactions.category`` so the Exceptions tab can surface them.
    """
    cutoff = (as_of or now_utc().date()) - timedelta(days=stale_days)
    with conn:
        rows = conn.execute(
            """
            SELECT pending_txn_id FROM pending_link
            WHERE settled_txn_id IS NULL
              AND pending_txn_id IS NOT NULL
              AND first_seen < ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        count = 0
        for row in rows:
            result = conn.execute(
                """
                UPDATE transactions
                   SET category = 'pending_stale'
                 WHERE txn_id = ?
                   AND deleted_at IS NULL
                """,
                (row["pending_txn_id"],),
            )
            if result.rowcount:
                count += 1
        return count


def parse_first_seen(value: str) -> datetime:
    """Round-tripper for tests / Run Status rendering."""
    return datetime.fromisoformat(value)


__all__ = [
    "DEFAULT_STALE_DAYS",
    "PendingOutcome",
    "PendingRecord",
    "SettlementOutcome",
    "SettlementRecord",
    "flag_stale",
    "parse_first_seen",
    "record_pending",
    "record_settlement",
]
