"""Tests for execution.reconcile.run — matcher to reconciliation_rows writer."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from execution.reconcile.run import run_matcher
from execution.reconcile.state import RowState
from execution.shared import db as db_mod


@pytest.fixture
def conn():
    c = db_mod.connect(":memory:")
    db_mod.apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_email(conn, msg_id: str = "m1"):
    conn.execute(
        """
        INSERT INTO emails (msg_id, source_adapter, received_at, from_addr, subject, outcome)
        VALUES (?, 'ms365', '2026-04-01T00:00:00+00:00', 'a@b.com', 'sub', 'invoice')
        """,
        (msg_id,),
    )


def _seed_vendor(conn, vendor_id: str = "stripe"):
    conn.execute(
        """
        INSERT INTO vendors (vendor_id, canonical_name)
        VALUES (?, ?)
        ON CONFLICT(vendor_id) DO NOTHING
        """,
        (vendor_id, vendor_id.capitalize()),
    )


def _seed_invoice(
    conn,
    *,
    invoice_id: str = "inv-1",
    vendor_id: str = "stripe",
    amount_gross: str = "100.00",
    invoice_date: str = "2026-04-10",
    invoice_number: str = "INV-1",
    supplier: str = "Stripe UK",
):
    _seed_email(conn, msg_id=f"msg-{invoice_id}")
    _seed_vendor(conn, vendor_id=vendor_id)
    conn.execute(
        """
        INSERT INTO invoices (
            invoice_id, source_msg_id, vendor_id, vendor_name_raw,
            invoice_number, invoice_date, currency, amount_gross,
            amount_gross_gbp, category, category_source,
            classifier_version
        )
        VALUES (?, ?, ?, ?, ?, ?, 'GBP', ?, ?, 'saas', 'rule', 'v1')
        """,
        (
            invoice_id,
            f"msg-{invoice_id}",
            vendor_id,
            supplier,
            invoice_number,
            invoice_date,
            amount_gross,
            amount_gross,
        ),
    )


def _seed_txn(
    conn,
    *,
    txn_id: str = "t-1",
    amount: str = "-100.00",
    amount_gbp: str = "100.00",
    booking_date: str = "2026-04-10",
    description: str = "STRIPE UK",
    txn_type: str = "purchase",
    status: str = "settled",
):
    conn.execute(
        """
        INSERT INTO transactions (
            txn_id, account, txn_type, booking_date,
            description_raw, description_canonical, currency,
            amount, amount_gbp, status, source
        )
        VALUES (?, 'amex', ?, ?, ?, ?, 'GBP', ?, ?, ?, 'csv')
        """,
        (
            txn_id,
            txn_type,
            booking_date,
            description,
            description,
            amount,
            amount_gbp,
            status,
        ),
    )


def test_auto_match_writes_reconciliation_row(conn):
    _seed_invoice(conn)
    _seed_txn(conn)

    # Seed vendor confirmations so the auto-match isn't demoted for
    # being unproven.
    conn.execute(
        """
        INSERT INTO vendor_category_hints (vendor_id, category, confirmed_count, last_confirmed_at)
        VALUES ('stripe', 'saas', 5, '2026-04-01T00:00:00+00:00')
        """
    )

    stats = run_matcher(conn, run_id="run-1")
    assert stats.invoices_scanned == 1
    assert stats.auto_matched == 1
    assert stats.unmatched == 0
    assert stats.rows_written == 1

    row = conn.execute(
        "SELECT state, txn_id, match_score FROM reconciliation_rows"
    ).fetchone()
    assert row["state"] == RowState.AUTO_MATCHED.value
    assert row["txn_id"] == "t-1"
    assert Decimal(row["match_score"]) >= Decimal("0.93")

    link = conn.execute(
        "SELECT link_kind, allocated_amount_gbp FROM reconciliation_links"
    ).fetchone()
    assert link["link_kind"] == "full"
    assert Decimal(link["allocated_amount_gbp"]) == Decimal("100.00")


def test_unmatched_invoice_writes_unmatched_row(conn):
    _seed_invoice(conn, supplier="Obscure Supplier Ltd")
    # No transactions seeded.

    stats = run_matcher(conn, run_id="run-1")
    assert stats.invoices_scanned == 1
    assert stats.unmatched == 1
    row = conn.execute("SELECT state, txn_id FROM reconciliation_rows").fetchone()
    assert row["state"] == RowState.UNMATCHED.value
    assert row["txn_id"] is None


def test_user_state_preserved_over_script(conn):
    _seed_invoice(conn)
    _seed_txn(conn)
    conn.execute(
        """
        INSERT INTO vendor_category_hints (vendor_id, category, confirmed_count, last_confirmed_at)
        VALUES ('stripe', 'saas', 5, '2026-04-01T00:00:00+00:00')
        """
    )
    # First pass — auto_matched.
    run_matcher(conn, run_id="run-1")
    row_before = conn.execute(
        "SELECT row_id, state FROM reconciliation_rows"
    ).fetchone()
    assert row_before["state"] == RowState.AUTO_MATCHED.value

    # Simulate the user verifying the row in the sheet.
    conn.execute(
        "UPDATE reconciliation_rows SET state = 'user_verified' WHERE row_id = ?",
        (row_before["row_id"],),
    )

    # Re-run — the state machine should preserve user_verified.
    stats = run_matcher(conn, run_id="run-2")
    assert stats.rows_preserved == 1 or stats.rows_written >= 1
    row_after = conn.execute(
        "SELECT state, txn_id FROM reconciliation_rows WHERE row_id = ?",
        (row_before["row_id"],),
    ).fetchone()
    assert row_after["state"] == "user_verified"
    # txn_id also preserved (the guard in the UPSERT).
    assert row_after["txn_id"] == "t-1"


def test_transfer_transactions_are_skipped_as_candidates(conn):
    _seed_invoice(conn)
    _seed_txn(conn, txn_id="transfer-1", txn_type="transfer")

    stats = run_matcher(conn, run_id="run-1")
    assert stats.unmatched == 1
    row = conn.execute("SELECT txn_id FROM reconciliation_rows").fetchone()
    assert row["txn_id"] is None


def test_reversed_transactions_are_skipped_as_candidates(conn):
    _seed_invoice(conn)
    _seed_txn(conn, txn_id="rev-1", status="reversed")

    stats = run_matcher(conn, run_id="run-1")
    assert stats.unmatched == 1
    row = conn.execute("SELECT txn_id FROM reconciliation_rows").fetchone()
    assert row["txn_id"] is None


def test_fiscal_year_scope_limits_invoices(conn):
    _seed_invoice(conn, invoice_id="inv-2026", invoice_date="2026-04-10", invoice_number="A")
    _seed_invoice(conn, invoice_id="inv-2025", invoice_date="2025-06-01", invoice_number="B")

    stats = run_matcher(conn, run_id="run-1", fiscal_year="FY-2026-27")
    assert stats.invoices_scanned == 1
    rows = conn.execute("SELECT invoice_id FROM reconciliation_rows").fetchall()
    assert [r["invoice_id"] for r in rows] == ["inv-2026"]


def test_rerun_updates_recon_row_in_place(conn):
    _seed_invoice(conn)
    _seed_txn(conn, txn_id="t-1")
    conn.execute(
        """
        INSERT INTO vendor_category_hints (vendor_id, category, confirmed_count, last_confirmed_at)
        VALUES ('stripe', 'saas', 5, '2026-04-01T00:00:00+00:00')
        """
    )
    run_matcher(conn, run_id="run-1")
    count_1 = conn.execute(
        "SELECT COUNT(*) AS c FROM reconciliation_rows"
    ).fetchone()["c"]
    run_matcher(conn, run_id="run-2")
    count_2 = conn.execute(
        "SELECT COUNT(*) AS c FROM reconciliation_rows"
    ).fetchone()["c"]
    assert count_1 == count_2 == 1
    row = conn.execute(
        "SELECT last_run_id FROM reconciliation_rows"
    ).fetchone()
    assert row["last_run_id"] == "run-2"


def test_empty_db_returns_zero_stats(conn):
    stats = run_matcher(conn, run_id="run-1")
    assert stats.invoices_scanned == 0
    assert stats.auto_matched == 0
    assert stats.unmatched == 0
    assert stats.rows_written == 0


def test_now_override_used_for_updated_at(conn):
    _seed_invoice(conn)
    fixed = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    run_matcher(conn, run_id="run-1", now=fixed)
    row = conn.execute("SELECT updated_at FROM reconciliation_rows").fetchone()
    assert row["updated_at"] == fixed.isoformat()


def test_unproven_vendor_auto_cap_demotes_to_suggested(conn):
    _seed_invoice(conn)
    _seed_txn(conn)
    # Intentionally DO NOT seed vendor_category_hints — the vendor is unproven.
    stats = run_matcher(conn, run_id="run-1")
    assert stats.suggested == 1
    assert stats.auto_matched == 0
    row = conn.execute("SELECT state FROM reconciliation_rows").fetchone()
    assert row["state"] == RowState.SUGGESTED.value
