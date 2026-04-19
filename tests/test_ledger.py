"""Tests for execution.reconcile.ledger."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from execution.adapters.amex_csv import ACCOUNT, RawTransaction
from execution.reconcile.ledger import (
    category_hint_for,
    classify_txn_type,
    link_refunds,
    write_batch,
)
from execution.shared import db as db_mod
from execution.shared import fx as fx_mod

# ---------------------------------------------------------------------------
# classify_txn_type
# ---------------------------------------------------------------------------


class TestClassifyTxnType:
    @pytest.mark.parametrize(
        "desc",
        [
            "AMEX PAYMENT",
            "American Express",
            "CARD PMT TO AMEX",
            "DD AMEX",
            "CARD REPAYMENT",
            "WISE TRANSFER",
            "INTERNAL TRANSFER",
            "STRIPE PAYOUT",
        ],
    )
    def test_transfer_patterns(self, desc):
        assert (
            classify_txn_type(
                amount=Decimal("100.00"),
                canonical_description=desc,
                account="wise",
            )
            == "transfer"
        )

    def test_negative_sign_is_refund(self):
        assert (
            classify_txn_type(
                amount=Decimal("-25.00"),
                canonical_description="STARBUCKS COFFEE",
                account="amex",
            )
            == "refund"
        )

    def test_positive_non_amex_is_income(self):
        assert (
            classify_txn_type(
                amount=Decimal("500.00"),
                canonical_description="CLIENT PAYMENT",
                account="wise",
            )
            == "income"
        )

    def test_positive_amex_without_refund_is_purchase(self):
        """Amex shouldn't see positive credits in the normal flow."""
        assert (
            classify_txn_type(
                amount=Decimal("50.00"),
                canonical_description="MYSTERY CREDIT",
                account="amex",
            )
            == "purchase"
        )

    def test_default_is_purchase(self):
        assert (
            classify_txn_type(
                amount=Decimal("12.00"),
                canonical_description="RANDOM SUPPLIER",
                account="amex",
            )
            == "purchase"
        )

    def test_bank_fee_stays_purchase(self):
        """Fees sit in the ``purchase`` slot; the category dimension tags them."""
        assert (
            classify_txn_type(
                amount=Decimal("2.50"),
                canonical_description="FX FEE",
                account="wise",
            )
            == "purchase"
        )


# ---------------------------------------------------------------------------
# category_hint_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "desc",
    [
        "CONVERSION FEE",
        "FOREIGN TRANSACTION FEE",
        "fx fee",
        "WIRE FEE",
        "MEMBERSHIP FEE",
    ],
)
def test_category_hint_detects_bank_fees(desc):
    assert category_hint_for(desc) == "bank_fee"


def test_category_hint_none_for_regular_merchant():
    assert category_hint_for("STARBUCKS COFFEE") is None


# ---------------------------------------------------------------------------
# write_batch — integration with the ``transactions`` table
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db_mod.connect(":memory:")
    db_mod.apply_migrations(c)
    return c


def _amex_row(**overrides) -> RawTransaction:
    defaults = {
        "txn_id": "t-1",
        "account": ACCOUNT,
        "booking_date": date(2026, 4, 10),
        "description_raw": "Starbucks London",
        "description_canonical": "STARBUCKS",
        "currency": "GBP",
        "amount": Decimal("4.50"),
        "reference": "REF-001",
        "category_hint": "Dining",
    }
    defaults.update(overrides)
    return RawTransaction(**defaults)  # type: ignore[arg-type]


def test_write_batch_inserts_single_row(conn):
    stats = write_batch(conn, [_amex_row()])
    assert stats.inserted == 1
    assert stats.classified_purchase == 1
    row = conn.execute(
        "SELECT * FROM transactions WHERE txn_id = 't-1'"
    ).fetchone()
    assert row["account"] == "amex"
    assert row["txn_type"] == "purchase"
    assert row["amount"] == "4.50"
    assert row["amount_gbp"] == "4.50"
    assert row["fx_rate"] is None
    assert row["status"] == "settled"
    assert row["source"] == "amex_csv"


def test_write_batch_is_idempotent(conn):
    write_batch(conn, [_amex_row()])
    write_batch(conn, [_amex_row()])
    count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count == 1


def test_write_batch_classifies_transfer_and_refund(conn):
    stats = write_batch(
        conn,
        [
            _amex_row(txn_id="t-a", description_canonical="AMEX PAYMENT"),
            _amex_row(
                txn_id="t-b",
                amount=Decimal("-25.00"),
                description_canonical="STARBUCKS",
            ),
        ],
    )
    assert stats.classified_transfer == 1
    assert stats.classified_refund == 1


def test_write_batch_respects_category_hint(conn):
    write_batch(
        conn,
        [_amex_row(description_canonical="FX FEE", category_hint=None)],
    )
    row = conn.execute(
        "SELECT category FROM transactions WHERE txn_id = 't-1'"
    ).fetchone()
    assert row["category"] == "bank_fee"


def test_write_batch_handles_non_gbp_with_cached_fx(conn, monkeypatch):
    fx_mod.set_mock_rate(date(2026, 4, 10), "USD", "GBP", Decimal("0.80"))
    # Seed cache so get_rate doesn't try the live path.
    conn.execute(
        """
        INSERT INTO fx_rates (date, from_ccy, to_ccy, rate, source, fetched_at)
        VALUES ('2026-04-10', 'USD', 'GBP', '0.80', 'mock', '2026-04-10T10:00:00')
        """
    )
    conn.commit()
    write_batch(
        conn,
        [_amex_row(currency="USD", amount=Decimal("100.00"), txn_id="t-usd")],
    )
    row = conn.execute(
        "SELECT amount, amount_gbp, fx_rate FROM transactions WHERE txn_id = 't-usd'"
    ).fetchone()
    assert row["amount"] == "100.00"
    assert Decimal(row["amount_gbp"]) == Decimal("80.00")
    assert Decimal(row["fx_rate"]) == Decimal("0.80")
    fx_mod.clear_mock_rates()


def test_write_batch_defers_fx_on_cache_miss(conn):
    write_batch(
        conn,
        [_amex_row(currency="USD", amount=Decimal("100.00"), txn_id="t-usd")],
    )
    row = conn.execute(
        "SELECT amount_gbp, fx_rate FROM transactions WHERE txn_id = 't-usd'"
    ).fetchone()
    # Defer — amount_gbp equals native until the FX backfill runs.
    assert row["amount_gbp"] == "100.00"
    assert row["fx_rate"] is None


def test_write_batch_returns_stats_zero_on_empty_input(conn):
    stats = write_batch(conn, [])
    assert stats.inserted == 0


# ---------------------------------------------------------------------------
# link_refunds
# ---------------------------------------------------------------------------


def test_link_refunds_links_prior_purchase(conn):
    # Original purchase + later refund for the same canonical description
    write_batch(
        conn,
        [
            _amex_row(
                txn_id="orig-1",
                amount=Decimal("25.00"),
                booking_date=date(2026, 4, 1),
                description_canonical="STARBUCKS",
                category_hint=None,
            ),
            _amex_row(
                txn_id="refund-1",
                amount=Decimal("-25.00"),
                booking_date=date(2026, 4, 10),
                description_canonical="STARBUCKS",
                category_hint=None,
            ),
        ],
    )
    linked = link_refunds(conn)
    assert linked == 1
    row = conn.execute(
        "SELECT category FROM transactions WHERE txn_id = 'refund-1'"
    ).fetchone()
    assert row["category"] == "refund_matched"


def test_link_refunds_marks_orphan_when_no_match(conn):
    write_batch(
        conn,
        [
            _amex_row(
                txn_id="refund-only",
                amount=Decimal("-25.00"),
                booking_date=date(2026, 4, 10),
                description_canonical="UNKNOWN SUPPLIER",
            ),
        ],
    )
    linked = link_refunds(conn)
    assert linked == 0
    row = conn.execute(
        "SELECT category FROM transactions WHERE txn_id = 'refund-only'"
    ).fetchone()
    assert row["category"] == "orphan_refund"


def test_link_refunds_ignores_purchases_outside_lookback(conn):
    # Original purchase 200 days before the refund → outside the 180d window
    write_batch(
        conn,
        [
            _amex_row(
                txn_id="old",
                amount=Decimal("10.00"),
                booking_date=date(2025, 9, 20),
                description_canonical="STARBUCKS",
            ),
            _amex_row(
                txn_id="ref",
                amount=Decimal("-10.00"),
                booking_date=date(2026, 4, 10),
                description_canonical="STARBUCKS",
            ),
        ],
    )
    linked = link_refunds(conn)
    assert linked == 0
    row = conn.execute(
        "SELECT category FROM transactions WHERE txn_id = 'ref'"
    ).fetchone()
    assert row["category"] == "orphan_refund"
