"""Tests for reconcile CLI commands."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from execution.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def mock_migrations() -> Iterator[None]:
    """Skip migrations in CLI tests - we create minimal schemas directly."""
    with patch("execution.shared.db.apply_migrations"):
        yield


class TestReconcileUpload:
    """Test the reconcile upload command."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create a test database."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Create minimal schema
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                completed_at TEXT,
                status TEXT NOT NULL,
                stats_json TEXT,
                cost_gbp TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions (
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

            CREATE TABLE IF NOT EXISTS reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                txn_id TEXT,
                fiscal_year TEXT,
                state TEXT,
                match_score TEXT,
                match_reason TEXT,
                override_history TEXT,
                updated_at TEXT,
                last_run_id TEXT
            );

            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT PRIMARY KEY,
                vendor_id TEXT,
                invoice_date TEXT,
                currency TEXT,
                amount_gross TEXT,
                amount_gross_gbp TEXT,
                deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS vendors (
                vendor_id TEXT PRIMARY KEY,
                canonical_name TEXT
            );

            CREATE TABLE IF NOT EXISTS emails (
                msg_id TEXT PRIMARY KEY,
                from_addr TEXT,
                received_at TEXT,
                processed_at TEXT,
                outcome TEXT
            );

            CREATE TABLE IF NOT EXISTS fx_rates (
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
        yield db_path
        conn.close()

    @pytest.fixture
    def sample_csv(self, tmp_path):
        """Create a sample CSV statement."""
        csv_path = tmp_path / "statement.csv"
        csv_path.write_text(
            "Date,Description,Amount,Balance\n"
            "15/11/2025,ANTHROPIC API,-49.99,1000.00\n"
            "16/11/2025,STARBUCKS LONDON,-5.50,994.50\n"
        )
        return csv_path

    def test_unknown_account_returns_error(self, test_db, sample_csv) -> None:
        """Unknown account should return error."""
        result = runner.invoke(
            app,
            ["reconcile", "upload", str(sample_csv), "--account", "invalid", "--db", str(test_db)],
        )

        assert result.exit_code == 1
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert "Unknown account" in output["message"]

    def test_file_not_found_returns_error(self, test_db, tmp_path) -> None:
        """Missing file should return error."""
        result = runner.invoke(
            app,
            ["reconcile", "upload", str(tmp_path / "nonexistent.csv"), "--account", "amex", "--db", str(test_db)],
        )

        assert result.exit_code == 1
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert "not found" in output["message"].lower()


class TestReconcileListTransactions:
    """Test the reconcile list-transactions command."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create a test database with sample transactions."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                completed_at TEXT,
                status TEXT NOT NULL,
                stats_json TEXT,
                cost_gbp TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions (
                txn_id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                txn_type TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                description_raw TEXT NOT NULL,
                description_canonical TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_gbp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'settled',
                deleted_at TEXT,
                needs_manual_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                txn_id TEXT,
                fiscal_year TEXT,
                state TEXT,
                match_score TEXT,
                match_reason TEXT,
                override_history TEXT,
                updated_at TEXT,
                last_run_id TEXT
            );

            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp
            ) VALUES
                ('txn1', 'amex', 'purchase', '2025-11-15', 'ANTHROPIC', 'ANTHROPIC', 'GBP', '-49.99', '-49.99'),
                ('txn2', 'wise', 'purchase', '2025-11-16', 'ZOOM', 'ZOOM', 'USD', '-15.00', '-12.00');
        """)
        conn.commit()
        yield db_path
        conn.close()

    def test_list_all_transactions(self, test_db) -> None:
        """Should list all transactions."""
        result = runner.invoke(
            app,
            ["reconcile", "list-transactions", "--db", str(test_db)],
        )

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["status"] == "success"
        assert output["count"] == 2

    def test_filter_by_account(self, test_db) -> None:
        """Should filter by account."""
        result = runner.invoke(
            app,
            ["reconcile", "list-transactions", "--account", "amex", "--db", str(test_db)],
        )

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["count"] == 1
        assert output["transactions"][0]["account"] == "amex"


class TestReconcileResolve:
    """Test the reconcile resolve command."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create a test database with a transaction."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                completed_at TEXT,
                status TEXT NOT NULL,
                stats_json TEXT,
                cost_gbp TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions (
                txn_id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                txn_type TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                description_raw TEXT NOT NULL,
                description_canonical TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_gbp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'settled',
                deleted_at TEXT,
                needs_manual_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                txn_id TEXT,
                fiscal_year TEXT,
                state TEXT,
                match_score TEXT,
                match_reason TEXT,
                override_history TEXT,
                updated_at TEXT,
                last_run_id TEXT
            );

            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp, needs_manual_download
            ) VALUES (
                'txn1', 'amex', 'purchase', '2025-11-15',
                'PERSONAL PURCHASE', 'PERSONAL PURCHASE',
                'GBP', '-25.00', '-25.00', 1
            );
        """)
        conn.commit()
        yield db_path
        conn.close()

    def test_resolve_to_personal(self, test_db) -> None:
        """Should resolve transaction to personal state."""
        result = runner.invoke(
            app,
            ["reconcile", "resolve", "txn1", "--state", "personal", "--db", str(test_db)],
        )

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["status"] == "success"
        assert output["txn_id"] == "txn1"
        assert output["state"] == "user_personal"

    def test_resolve_unknown_state_returns_error(self, test_db) -> None:
        """Unknown state should return error."""
        result = runner.invoke(
            app,
            ["reconcile", "resolve", "txn1", "--state", "invalid", "--db", str(test_db)],
        )

        assert result.exit_code == 1
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert "Unknown state" in output["message"]

    def test_resolve_missing_transaction_returns_error(self, test_db) -> None:
        """Missing transaction should return error."""
        result = runner.invoke(
            app,
            ["reconcile", "resolve", "nonexistent", "--state", "personal", "--db", str(test_db)],
        )

        assert result.exit_code == 1
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert "not found" in output["message"].lower()

    def test_resolve_verified_requires_invoice_id(self, test_db) -> None:
        """Verified state should require invoice-id."""
        result = runner.invoke(
            app,
            ["reconcile", "resolve", "txn1", "--state", "verified", "--db", str(test_db)],
        )

        assert result.exit_code == 1
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert "requires --invoice-id" in output["message"]


class TestReconcileBulkUpload:
    """Test the reconcile bulk-upload command."""

    def test_no_files_returns_error(self, tmp_path) -> None:
        """No files should return error."""
        result = runner.invoke(
            app,
            ["reconcile", "bulk-upload"],
        )

        # Typer should complain about missing argument
        assert result.exit_code != 0

    def test_missing_file_returns_error(self, tmp_path) -> None:
        """Missing file should return error."""
        db_path = tmp_path / "test.db"

        result = runner.invoke(
            app,
            ["reconcile", "bulk-upload", str(tmp_path / "nonexistent.pdf"), "--db", str(db_path)],
        )

        assert result.exit_code == 1
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert "not found" in output["message"].lower()
