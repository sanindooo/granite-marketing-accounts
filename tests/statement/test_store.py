"""Tests for statement store module."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from execution.shared.fx import clear_mock_rates, set_mock_rate
from execution.statement.parser import RawTransaction
from execution.statement.store import (
    StoreResult,
    canonicalize_description,
    compute_txn_id,
    store_transactions,
)


class TestCanonicalizeDescription:
    """Test description canonicalization for stable hashing."""

    def test_uppercase_and_trim(self) -> None:
        assert canonicalize_description("  hello world  ") == "HELLO WORLD"

    def test_collapse_whitespace(self) -> None:
        assert canonicalize_description("hello   world") == "HELLO WORLD"

    def test_strip_trailing_country(self) -> None:
        assert canonicalize_description("STARBUCKS GB") == "STARBUCKS"
        assert canonicalize_description("COFFEE SHOP GB 12345") == "COFFEE SHOP"

    def test_strip_trailing_reference(self) -> None:
        assert canonicalize_description("MERCHANT ABCD12345678") == "MERCHANT"
        assert canonicalize_description("SHOP REF12345EF") == "SHOP"

    def test_strip_uk_city_suffix(self) -> None:
        assert canonicalize_description("STARBUCKS LONDON") == "STARBUCKS"
        assert canonicalize_description("COFFEE MANCHESTER") == "COFFEE"
        assert canonicalize_description("SHOP BRISTOL CITY CENTRE") == "SHOP"

    def test_combined_cleaning(self) -> None:
        # Real-world Amex-style description
        raw = "  ANTHROPIC API HTTPSAN FRANCISCO CA 12345678  "
        # Should strip trailing city/ref but keep the core
        result = canonicalize_description(raw)
        assert "ANTHROPIC" in result


class TestComputeTxnId:
    """Test stable transaction ID computation."""

    def test_deterministic(self) -> None:
        txn_id1 = compute_txn_id(
            account="amex",
            booking_date=date(2025, 11, 15),
            canonical_description="MERCHANT",
            amount=Decimal("-50.00"),
            row_ordinal=0,
        )
        txn_id2 = compute_txn_id(
            account="amex",
            booking_date=date(2025, 11, 15),
            canonical_description="MERCHANT",
            amount=Decimal("-50.00"),
            row_ordinal=0,
        )
        assert txn_id1 == txn_id2

    def test_different_for_different_inputs(self) -> None:
        base = {
            "account": "amex",
            "booking_date": date(2025, 11, 15),
            "canonical_description": "MERCHANT",
            "amount": Decimal("-50.00"),
            "row_ordinal": 0,
        }
        txn_id_base = compute_txn_id(**base)

        # Different account
        txn_id_account = compute_txn_id(**{**base, "account": "wise"})
        assert txn_id_account != txn_id_base

        # Different date
        txn_id_date = compute_txn_id(**{**base, "booking_date": date(2025, 11, 16)})
        assert txn_id_date != txn_id_base

        # Different amount
        txn_id_amount = compute_txn_id(**{**base, "amount": Decimal("-51.00")})
        assert txn_id_amount != txn_id_base

        # Different ordinal (same-day disambiguation)
        txn_id_ordinal = compute_txn_id(**{**base, "row_ordinal": 1})
        assert txn_id_ordinal != txn_id_base

    def test_is_16_char_hex(self) -> None:
        txn_id = compute_txn_id(
            account="amex",
            booking_date=date(2025, 11, 15),
            canonical_description="TEST",
            amount=Decimal("-10.00"),
            row_ordinal=0,
        )
        assert len(txn_id) == 16
        assert all(c in "0123456789abcdef" for c in txn_id)


class TestStoreTransactions:
    """Test transaction storage with deduplication."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create a test database with the necessary tables."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Create minimal schema needed for tests
        conn.executescript("""
            CREATE TABLE transactions (
                txn_id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                txn_type TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                description_raw TEXT NOT NULL,
                description_canonical TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_gbp TEXT NOT NULL,
                fx_rate TEXT,
                status TEXT NOT NULL DEFAULT 'settled',
                source TEXT NOT NULL,
                hash_schema_version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT,
                needs_manual_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE fx_rates (
                date TEXT NOT NULL,
                from_ccy TEXT NOT NULL,
                to_ccy TEXT NOT NULL,
                rate TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'ecb',
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (date, from_ccy, to_ccy)
            );
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_store_new_transactions(self, test_db) -> None:
        transactions = [
            RawTransaction(
                date=date(2025, 11, 15),
                description="MERCHANT ONE",
                amount=Decimal("-50.00"),
                currency="GBP",
            ),
            RawTransaction(
                date=date(2025, 11, 16),
                description="MERCHANT TWO",
                amount=Decimal("-25.00"),
                currency="GBP",
            ),
        ]

        result = store_transactions(test_db, transactions, "amex")

        assert result.total_count == 2
        assert result.new_count == 2
        assert result.duplicate_count == 0

        # Verify in database
        rows = test_db.execute("SELECT * FROM transactions").fetchall()
        assert len(rows) == 2

    def test_deduplication_skips_duplicates(self, test_db) -> None:
        transactions = [
            RawTransaction(
                date=date(2025, 11, 15),
                description="MERCHANT",
                amount=Decimal("-50.00"),
                currency="GBP",
            ),
        ]

        # First upload
        result1 = store_transactions(test_db, transactions, "amex")
        assert result1.new_count == 1
        assert result1.duplicate_count == 0

        # Second upload of same transactions
        result2 = store_transactions(test_db, transactions, "amex")
        assert result2.new_count == 0
        assert result2.duplicate_count == 1

        # Still only one row in DB
        count = test_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert count == 1

    def test_overlapping_statements_deduplication(self, test_db) -> None:
        """Test AE5: overlapping statement uploads deduplicate correctly."""
        march_transactions = [
            RawTransaction(
                date=date(2025, 3, 28), description="TXN1", amount=Decimal("-10.00"), currency="GBP"
            ),
            RawTransaction(
                date=date(2025, 3, 29), description="TXN2", amount=Decimal("-20.00"), currency="GBP"
            ),
            RawTransaction(
                date=date(2025, 3, 30), description="TXN3", amount=Decimal("-30.00"), currency="GBP"
            ),
            RawTransaction(
                date=date(2025, 3, 31), description="TXN4", amount=Decimal("-40.00"), currency="GBP"
            ),
        ]
        april_transactions = [
            # These 2 overlap with March
            RawTransaction(
                date=date(2025, 3, 30), description="TXN3", amount=Decimal("-30.00"), currency="GBP"
            ),
            RawTransaction(
                date=date(2025, 3, 31), description="TXN4", amount=Decimal("-40.00"), currency="GBP"
            ),
            # These are new
            RawTransaction(
                date=date(2025, 4, 1), description="TXN5", amount=Decimal("-50.00"), currency="GBP"
            ),
            RawTransaction(
                date=date(2025, 4, 2), description="TXN6", amount=Decimal("-60.00"), currency="GBP"
            ),
        ]

        # Upload March
        result_march = store_transactions(test_db, march_transactions, "amex")
        assert result_march.new_count == 4

        # Upload April (should skip 2 duplicates)
        result_april = store_transactions(test_db, april_transactions, "amex")
        assert result_april.new_count == 2
        assert result_april.duplicate_count == 2

        # Total should be 6
        count = test_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert count == 6

    def test_same_day_different_transactions(self, test_db) -> None:
        """Two different purchases on same day should both be stored."""
        transactions = [
            RawTransaction(
                date=date(2025, 11, 15),
                description="COFFEE SHOP",
                amount=Decimal("-3.50"),
                currency="GBP",
            ),
            RawTransaction(
                date=date(2025, 11, 15),
                description="LUNCH PLACE",
                amount=Decimal("-12.00"),
                currency="GBP",
            ),
        ]

        result = store_transactions(test_db, transactions, "amex")
        assert result.new_count == 2

    def test_fx_conversion_for_usd(self, test_db) -> None:
        """Test USD transaction converts to GBP."""
        # Set mock FX rate
        set_mock_rate(date(2025, 11, 15), "USD", "GBP", Decimal("0.80"))

        transactions = [
            RawTransaction(
                date=date(2025, 11, 15),
                description="US MERCHANT",
                amount=Decimal("-50.00"),
                currency="USD",
            ),
        ]

        try:
            result = store_transactions(test_db, transactions, "wise")
            assert result.new_count == 1
            assert len(result.fx_errors) == 0

            # Check stored values
            row = test_db.execute("SELECT * FROM transactions").fetchone()
            assert row["currency"] == "USD"
            assert row["amount"] == "-50.00"
            assert row["amount_gbp"] == "-40.00"  # 50 * 0.80
            assert row["fx_rate"] == "0.800000"
        finally:
            clear_mock_rates()

    def test_transaction_type_from_amount(self, test_db) -> None:
        """Negative amount = purchase, positive = income."""
        transactions = [
            RawTransaction(
                date=date(2025, 11, 15),
                description="PURCHASE",
                amount=Decimal("-50.00"),
                currency="GBP",
            ),
            RawTransaction(
                date=date(2025, 11, 16),
                description="REFUND",
                amount=Decimal("25.00"),
                currency="GBP",
            ),
        ]

        store_transactions(test_db, transactions, "amex")

        rows = test_db.execute(
            "SELECT txn_type, amount FROM transactions ORDER BY booking_date"
        ).fetchall()
        assert rows[0]["txn_type"] == "purchase"
        assert rows[1]["txn_type"] == "income"


class TestStoreResult:
    """Test StoreResult dataclass."""

    def test_success_with_new(self) -> None:
        result = StoreResult(total_count=5, new_count=5, duplicate_count=0, fx_errors=[])
        assert result.success is True

    def test_success_with_duplicates_only(self) -> None:
        result = StoreResult(total_count=5, new_count=0, duplicate_count=5, fx_errors=[])
        assert result.success is True

    def test_no_success_with_nothing(self) -> None:
        result = StoreResult(total_count=0, new_count=0, duplicate_count=0, fx_errors=[])
        assert result.success is False
