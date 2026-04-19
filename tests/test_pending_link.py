"""Tests for execution.reconcile.pending_link."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from execution.reconcile.pending_link import (
    PendingOutcome,
    SettlementOutcome,
    flag_stale,
    record_pending,
    record_settlement,
)
from execution.shared import db as db_mod


def _seed_txn(
    conn: sqlite3.Connection,
    *,
    txn_id: str,
    account: str = "monzo",
    status: str = "pending",
) -> None:
    conn.execute(
        """
        INSERT INTO transactions (
            txn_id, account, txn_type, booking_date,
            description_raw, description_canonical, currency,
            amount, amount_gbp, status, source, hash_schema_version
        ) VALUES (
            ?, ?, 'purchase', '2026-04-10',
            'Coffee', 'COFFEE', 'GBP',
            '4.50', '4.50', ?, 'monzo_api', 1
        )
        """,
        (txn_id, account, status),
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db_mod.connect(":memory:")
    db_mod.apply_migrations(c)
    return c


# ---------------------------------------------------------------------------
# record_pending
# ---------------------------------------------------------------------------


class TestRecordPending:
    def test_records_new_authorisation(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-1")
        result = record_pending(
            conn,
            provider_auth_id="auth-123",
            account="monzo",
            pending_txn_id="p-1",
        )
        assert result.outcome is PendingOutcome.RECORDED
        assert result.ambiguous is False
        row = conn.execute(
            "SELECT * FROM pending_link WHERE provider_auth_id = 'auth-123'"
        ).fetchone()
        assert row["pending_txn_id"] == "p-1"
        assert row["settled_txn_id"] is None
        assert not row["ambiguous"]

    def test_reports_already_known_on_same_txn(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-1")
        record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-1"
        )
        second = record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-1"
        )
        assert second.outcome is PendingOutcome.ALREADY_KNOWN

    def test_flags_ambiguous_on_conflicting_pending(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-1")
        _seed_txn(conn, txn_id="p-2")
        record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-1"
        )
        second = record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-2"
        )
        assert second.outcome is PendingOutcome.AMBIGUOUS
        assert second.ambiguous is True
        row = conn.execute(
            "SELECT ambiguous FROM pending_link WHERE provider_auth_id = 'auth-1'"
        ).fetchone()
        assert row["ambiguous"]

    def test_empty_auth_id_rejected(self, conn: sqlite3.Connection):
        with pytest.raises(ValueError):
            record_pending(
                conn, provider_auth_id="", account="monzo", pending_txn_id="p-1"
            )


# ---------------------------------------------------------------------------
# record_settlement
# ---------------------------------------------------------------------------


class TestRecordSettlement:
    def test_merges_settlement_into_pending(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-1", status="pending")
        _seed_txn(conn, txn_id="s-1", status="pending")
        record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-1"
        )
        result = record_settlement(
            conn, provider_auth_id="auth-1", settled_txn_id="s-1"
        )
        assert result.outcome is SettlementOutcome.MERGED
        assert result.removed_pending_txn_id == "p-1"

        # Settled row flipped to 'settled'
        settled = conn.execute(
            "SELECT status FROM transactions WHERE txn_id = 's-1'"
        ).fetchone()
        assert settled["status"] == "settled"

        # Pending row soft-deleted
        pending = conn.execute(
            "SELECT deleted_at, deleted_reason FROM transactions WHERE txn_id = 'p-1'"
        ).fetchone()
        assert pending["deleted_at"] is not None
        assert pending["deleted_reason"] == "merged_into_settled"

    def test_idempotent_on_second_settlement(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-1", status="pending")
        _seed_txn(conn, txn_id="s-1", status="pending")
        record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-1"
        )
        record_settlement(
            conn, provider_auth_id="auth-1", settled_txn_id="s-1"
        )
        second = record_settlement(
            conn, provider_auth_id="auth-1", settled_txn_id="s-1"
        )
        assert second.outcome is SettlementOutcome.ALREADY_SETTLED

    def test_returns_no_pending_when_none_recorded(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="s-orphan", status="settled")
        result = record_settlement(
            conn, provider_auth_id="auth-ghost", settled_txn_id="s-orphan"
        )
        assert result.outcome is SettlementOutcome.NO_PENDING
        assert result.removed_pending_txn_id is None

    def test_when_same_txn_ids_skip_delete(self, conn: sqlite3.Connection):
        """Edge case — the pending row IS the row that settles.

        Rare, but possible when an adapter upserts the same row from
        'pending' to 'settled' in-place. We should flip the status and
        NOT soft-delete the only row.
        """
        _seed_txn(conn, txn_id="p-1", status="pending")
        record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-1"
        )
        record_settlement(
            conn, provider_auth_id="auth-1", settled_txn_id="p-1"
        )
        row = conn.execute(
            "SELECT status, deleted_at FROM transactions WHERE txn_id = 'p-1'"
        ).fetchone()
        assert row["status"] == "settled"
        assert row["deleted_at"] is None


# ---------------------------------------------------------------------------
# flag_stale
# ---------------------------------------------------------------------------


class TestFlagStale:
    def test_flags_pending_older_than_cutoff(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-old", status="pending")
        record_pending(
            conn,
            provider_auth_id="auth-old",
            account="monzo",
            pending_txn_id="p-old",
        )
        # Rewind first_seen to 20 days ago
        twenty_days_ago = date.today() - timedelta(days=20)
        conn.execute(
            "UPDATE pending_link SET first_seen = ?",
            (twenty_days_ago.isoformat() + "T00:00:00+00:00",),
        )
        conn.commit()

        tagged = flag_stale(conn, stale_days=14)
        assert tagged == 1
        row = conn.execute(
            "SELECT category FROM transactions WHERE txn_id = 'p-old'"
        ).fetchone()
        assert row["category"] == "pending_stale"

    def test_leaves_fresh_pending_alone(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-fresh", status="pending")
        record_pending(
            conn,
            provider_auth_id="auth-fresh",
            account="monzo",
            pending_txn_id="p-fresh",
        )
        tagged = flag_stale(conn, stale_days=14)
        assert tagged == 0
        row = conn.execute(
            "SELECT category FROM transactions WHERE txn_id = 'p-fresh'"
        ).fetchone()
        assert row["category"] is None

    def test_ignores_already_settled_pending_rows(self, conn: sqlite3.Connection):
        _seed_txn(conn, txn_id="p-1", status="pending")
        _seed_txn(conn, txn_id="s-1", status="pending")
        record_pending(
            conn, provider_auth_id="auth-1", account="monzo", pending_txn_id="p-1"
        )
        record_settlement(
            conn, provider_auth_id="auth-1", settled_txn_id="s-1"
        )
        # Even if the pending row is ancient, merging it out means it
        # shouldn't be re-tagged stale.
        conn.execute("UPDATE pending_link SET first_seen = '2020-01-01T00:00:00+00:00'")
        conn.commit()
        tagged = flag_stale(conn, stale_days=14)
        assert tagged == 0
