"""DB schema, PRAGMAs, migration idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from execution.shared import db as db_mod
from execution.shared.errors import ConfigError


def _fresh_conn() -> sqlite3.Connection:
    return db_mod.connect(":memory:")


class TestConnect:
    def test_wal_mode_enabled(self) -> None:
        # :memory: DBs always report "memory" for journal_mode; use tmpfile
        # instead to verify WAL actually activates.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = db_mod.connect(db)
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            assert mode.lower() == "wal"
            conn.close()

    def test_foreign_keys_enabled(self) -> None:
        conn = _fresh_conn()
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1

    def test_busy_timeout(self) -> None:
        conn = _fresh_conn()
        assert conn.execute("PRAGMA busy_timeout;").fetchone()[0] == 30_000

    def test_synchronous_normal(self) -> None:
        conn = _fresh_conn()
        # synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
        assert conn.execute("PRAGMA synchronous;").fetchone()[0] == 1


class TestMigrations:
    def test_initial_migration_creates_all_tables(self) -> None:
        conn = _fresh_conn()
        db_mod.apply_migrations(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "schema_migrations",
            "emails",
            "vendors",
            "invoices",
            "transactions",
            "reconciliation_rows",
            "reconciliation_links",
            "pending_link",
            "fx_rates",
            "fiscal_year_sheets",
            "runs",
            "reauth_required",
            "id_migrations",
            "watermarks",
            "vendor_category_hints",
        }
        missing = expected - tables
        assert not missing, f"missing tables: {missing}"

    def test_migrations_are_idempotent(self) -> None:
        conn = _fresh_conn()
        first = db_mod.apply_migrations(conn)
        assert first  # first call actually ran
        second = db_mod.apply_migrations(conn)
        assert second == []  # second call ran nothing

    def test_current_version_records_latest(self) -> None:
        conn = _fresh_conn()
        db_mod.apply_migrations(conn)
        assert db_mod.current_version(conn) == "011_add_email_manual_download_url"

    def test_tampered_migration_rejected(self, tmp_path: Path) -> None:
        # Build a migrations dir with a single file, apply it, then mutate
        # the file and ensure we refuse to re-apply.
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        mig = migrations / "001_tiny.sql"
        mig.write_text("CREATE TABLE foo (x INTEGER);")
        conn = db_mod.connect(":memory:")
        db_mod.apply_migrations(conn, migrations_dir=migrations)
        mig.write_text("CREATE TABLE foo (x INTEGER); DROP TABLE foo;")
        with pytest.raises(ConfigError, match="checksum"):
            db_mod.apply_migrations(conn, migrations_dir=migrations)


class TestIndexes:
    @pytest.fixture
    def conn(self) -> sqlite3.Connection:
        conn = _fresh_conn()
        db_mod.apply_migrations(conn)
        return conn

    def test_txn_composite_index_exists(self, conn: sqlite3.Connection) -> None:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_txn_date_amt" in indexes
        assert "idx_inv_vendor_date" in indexes
        assert "idx_recon_fy_state" in indexes
        assert "ux_invoice_vendor_number" in indexes


class TestForeignKeyEnforcement:
    def test_orphaned_invoice_rejected(self, tmp_db: sqlite3.Connection) -> None:
        # vendor_id FK is RESTRICT → inserting without a parent vendor fails
        with pytest.raises(sqlite3.IntegrityError):
            tmp_db.execute(
                """
                INSERT INTO invoices
                    (invoice_id, source_msg_id, vendor_id, vendor_name_raw,
                     invoice_number, invoice_date, currency, amount_gross,
                     reverse_charge, category, category_source, classifier_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?);
                """,
                (
                    "inv-1",
                    "msg-nonexistent",
                    "vendor-nonexistent",
                    "Acme",
                    "INV-001",
                    "2026-04-17",
                    "GBP",
                    "100.00",
                    "software",
                    "llm",
                    "v1",
                ),
            )


class TestTransactionContext:
    def test_rolls_back_on_exception(self) -> None:
        conn = _fresh_conn()
        db_mod.apply_migrations(conn)
        try:
            with db_mod.transaction(conn):
                conn.execute(
                    "INSERT INTO vendors (vendor_id, canonical_name) VALUES (?, ?)",
                    ("v1", "Acme"),
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        count = conn.execute(
            "SELECT COUNT(*) FROM vendors WHERE vendor_id='v1'"
        ).fetchone()[0]
        assert count == 0

    def test_commits_on_success(self) -> None:
        conn = _fresh_conn()
        db_mod.apply_migrations(conn)
        with db_mod.transaction(conn):
            conn.execute(
                "INSERT INTO vendors (vendor_id, canonical_name) VALUES (?, ?)",
                ("v2", "Beta"),
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM vendors WHERE vendor_id='v2'"
        ).fetchone()[0]
        assert count == 1
