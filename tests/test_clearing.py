"""Tests for execution.reconcile.clearing."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from execution.adapters.amex_email import StatementClosing
from execution.reconcile.clearing import (
    CandidateDebit,
    ClearingAmbiguous,
    ClearingMatch,
    apply_clearing_result,
    fetch_candidates,
    match_clearing,
)
from execution.shared import db as db_mod
from execution.shared.errors import DataQualityError


def _stmt(**overrides) -> StatementClosing:
    defaults = {
        "source_msg_id": "stmt-1",
        "statement_billed_amount": Decimal("247.43"),
        "statement_close_date": date(2026, 3, 25),
    }
    defaults.update(overrides)
    return StatementClosing(**defaults)  # type: ignore[arg-type]


def _debit(**overrides) -> CandidateDebit:
    defaults = {
        "txn_id": "t-1",
        "account": "wise",
        "booking_date": date(2026, 3, 28),
        "amount": Decimal("247.43"),
    }
    defaults.update(overrides)
    return CandidateDebit(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# match_clearing — single-candidate, tolerance, window
# ---------------------------------------------------------------------------


class TestMatchClearing:
    def test_exact_amount_unambiguous_match(self):
        result = match_clearing(_stmt(), [_debit()])
        assert isinstance(result, ClearingMatch)
        assert result.debit_txn_id == "t-1"
        assert result.delta == Decimal("0")

    def test_within_tolerance_still_matches(self):
        result = match_clearing(
            _stmt(),
            [_debit(amount=Decimal("247.93"))],
        )
        assert isinstance(result, ClearingMatch)
        assert result.delta == Decimal("0.50")

    def test_beyond_tolerance_no_match(self):
        result = match_clearing(
            _stmt(),
            [_debit(amount=Decimal("250.00"))],
        )
        assert isinstance(result, ClearingAmbiguous)
        assert result.reason == "no_candidate"

    def test_outside_window_no_match(self):
        # Debit 2 days *before* close — outside [close+1, close+35].
        result = match_clearing(
            _stmt(),
            [_debit(booking_date=date(2026, 3, 23))],
        )
        assert isinstance(result, ClearingAmbiguous)
        assert result.reason == "no_candidate"

    def test_far_outside_window_no_match(self):
        # 40 days after — outside the 35d cap.
        result = match_clearing(
            _stmt(),
            [_debit(booking_date=date(2026, 5, 4))],
        )
        assert isinstance(result, ClearingAmbiguous)

    def test_multiple_candidates_surfaces_ambiguous(self):
        result = match_clearing(
            _stmt(),
            [
                _debit(txn_id="t-1", booking_date=date(2026, 3, 28)),
                _debit(txn_id="t-2", booking_date=date(2026, 4, 3)),
            ],
        )
        assert isinstance(result, ClearingAmbiguous)
        assert result.reason == "multiple_candidates"
        assert set(result.candidates) == {"t-1", "t-2"}

    def test_tolerance_must_be_non_negative(self):
        with pytest.raises(DataQualityError):
            match_clearing(
                _stmt(), [_debit()], tolerance=Decimal("-0.01")
            )

    def test_monzo_candidate_eligible(self):
        result = match_clearing(
            _stmt(),
            [_debit(account="monzo", txn_id="monzo-1")],
        )
        assert isinstance(result, ClearingMatch)
        assert result.debit_account == "monzo"


# ---------------------------------------------------------------------------
# apply_clearing_result — DB side effects
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db_mod.connect(":memory:")
    db_mod.apply_migrations(c)
    # Seed the transactions table with a single candidate row.
    c.execute(
        """
        INSERT INTO transactions (
            txn_id, account, txn_type, booking_date,
            description_raw, description_canonical, currency,
            amount, amount_gbp, status, source, hash_schema_version
        ) VALUES (
            't-1', 'wise', 'transfer', '2026-03-28',
            'AMEX PAYMENT', 'AMEX PAYMENT', 'GBP',
            '247.43', '247.43', 'settled', 'wise_api', 1
        )
        """
    )
    c.commit()
    return c


def test_apply_match_tags_amex_clearing(conn: sqlite3.Connection):
    result = ClearingMatch(
        statement_msg_id="stmt-1",
        debit_txn_id="t-1",
        debit_account="wise",
        statement_close_date=date(2026, 3, 25),
        statement_billed_amount=Decimal("247.43"),
        debit_amount=Decimal("247.43"),
        delta=Decimal("0"),
    )
    apply_clearing_result(conn, result)
    row = conn.execute(
        "SELECT category FROM transactions WHERE txn_id = 't-1'"
    ).fetchone()
    assert row["category"] == "amex_clearing"


def test_apply_ambiguous_tags_unconfirmed(conn: sqlite3.Connection):
    # Add a second candidate so the ambiguous branch has two txn_ids to tag.
    conn.execute(
        """
        INSERT INTO transactions (
            txn_id, account, txn_type, booking_date,
            description_raw, description_canonical, currency,
            amount, amount_gbp, status, source, hash_schema_version
        ) VALUES (
            't-2', 'monzo', 'transfer', '2026-04-02',
            'AMEX PAYMENT', 'AMEX PAYMENT', 'GBP',
            '247.43', '247.43', 'settled', 'monzo_api', 1
        )
        """
    )
    conn.commit()
    result = ClearingAmbiguous(
        statement_msg_id="stmt-1",
        statement_billed_amount=Decimal("247.43"),
        reason="multiple_candidates",
        candidates=("t-1", "t-2"),
    )
    apply_clearing_result(conn, result)
    rows = conn.execute(
        "SELECT txn_id, category FROM transactions ORDER BY txn_id"
    ).fetchall()
    assert {r["txn_id"]: r["category"] for r in rows} == {
        "t-1": "transfer_unconfirmed",
        "t-2": "transfer_unconfirmed",
    }


# ---------------------------------------------------------------------------
# fetch_candidates — SQL filter works against the ``transactions`` table
# ---------------------------------------------------------------------------


def test_fetch_candidates_filters_by_window(conn: sqlite3.Connection):
    # Add a row outside the window to make sure it's filtered.
    conn.execute(
        """
        INSERT INTO transactions (
            txn_id, account, txn_type, booking_date,
            description_raw, description_canonical, currency,
            amount, amount_gbp, status, source, hash_schema_version
        ) VALUES (
            't-3', 'wise', 'transfer', '2026-06-10',
            'AMEX PAYMENT', 'AMEX PAYMENT', 'GBP',
            '300.00', '300.00', 'settled', 'wise_api', 1
        )
        """
    )
    conn.commit()
    candidates = fetch_candidates(
        conn,
        statement_close_date=date(2026, 3, 25),
    )
    assert [c.txn_id for c in candidates] == ["t-1"]


def test_fetch_candidates_skips_non_transfer(conn: sqlite3.Connection):
    conn.execute(
        """
        INSERT INTO transactions (
            txn_id, account, txn_type, booking_date,
            description_raw, description_canonical, currency,
            amount, amount_gbp, status, source, hash_schema_version
        ) VALUES (
            't-4', 'wise', 'purchase', '2026-03-28',
            'STARBUCKS', 'STARBUCKS', 'GBP',
            '4.50', '4.50', 'settled', 'wise_api', 1
        )
        """
    )
    conn.commit()
    candidates = fetch_candidates(
        conn, statement_close_date=date(2026, 3, 25)
    )
    # Only the transfer row survives
    assert [c.txn_id for c in candidates] == ["t-1"]
