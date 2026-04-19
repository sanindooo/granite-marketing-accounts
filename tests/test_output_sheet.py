"""Tests for execution.output.sheet."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from execution.output.sheet import (
    EXCEPTIONS_COLUMNS,
    RECONCILIATION_COLUMNS,
    RUN_STATUS_COLUMNS,
    SALES_COLUMNS,
    UNMATCHED_INVOICE_COLUMNS,
    UNMATCHED_TXN_COLUMNS,
    ExceptionRow,
    InMemorySheetSink,
    ReconciliationRow,
    RunStatusRow,
    SalesRow,
    UnmatchedInvoice,
    UnmatchedTransaction,
    exception_cells,
    reconciliation_cells,
    run_status_cells,
    sales_cells,
    unmatched_invoice_cells,
    unmatched_txn_cells,
    write_exceptions_tab,
    write_reconciliation_tab,
    write_run_status_tab,
    write_sales_tab,
    write_unmatched_invoices_tab,
    write_unmatched_txns_tab,
)

NOW = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Column schemas — invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        RECONCILIATION_COLUMNS,
        UNMATCHED_INVOICE_COLUMNS,
        UNMATCHED_TXN_COLUMNS,
        EXCEPTIONS_COLUMNS,
        SALES_COLUMNS,
        RUN_STATUS_COLUMNS,
    ],
)
def test_each_schema_is_tuple_of_unique_strings(schema):
    assert isinstance(schema, tuple)
    assert len(schema) == len(set(schema))
    assert all(isinstance(c, str) and c for c in schema)


# ---------------------------------------------------------------------------
# reconciliation_cells — shape + sanitiser + cell count
# ---------------------------------------------------------------------------


def _recon_row(**overrides):
    defaults = {
        "row_id": "r-1",
        "fiscal_year": "FY-2026-27",
        "state": "auto_matched",
        "score": Decimal("0.9500"),
        "invoice_id": "inv-1",
        "invoice_number": "INV-0412",
        "invoice_date": date(2026, 4, 1),
        "supplier_name": "Atlassian",
        "category": "software_saas",
        "currency": "GBP",
        "amount_gross": Decimal("480.00"),
        "amount_gbp": Decimal("480.00"),
        "txn_id": "t-1",
        "booking_date": date(2026, 4, 1),
        "account": "amex",
        "description": "ATLASSIAN",
        "match_reason": "short-circuit",
        "verified": False,
        "override_match": None,
        "personal_flag": False,
        "ignore_flag": False,
        "category_override": None,
        "notes": "",
        "drive_link": "https://drive.example/x",
    }
    defaults.update(overrides)
    return ReconciliationRow(**defaults)


class TestReconciliationCells:
    def test_cell_count_matches_schema(self):
        cells = reconciliation_cells(_recon_row())
        assert len(cells) == len(RECONCILIATION_COLUMNS)

    def test_booleans_render_as_upper_case_literals(self):
        cells = reconciliation_cells(_recon_row(verified=True, personal_flag=True))
        header_to_cell = dict(zip(RECONCILIATION_COLUMNS, cells, strict=True))
        assert header_to_cell["Verified"] == "TRUE"
        assert header_to_cell["Personal?"] == "TRUE"
        assert header_to_cell["Ignore?"] == "FALSE"

    def test_none_amount_renders_blank(self):
        cells = reconciliation_cells(_recon_row(amount_gross=None, amount_gbp=None))
        mapping = dict(zip(RECONCILIATION_COLUMNS, cells, strict=True))
        assert mapping["Amount Gross"] == ""
        assert mapping["Amount GBP"] == ""

    def test_formula_injection_in_supplier_is_sanitised(self):
        cells = reconciliation_cells(
            _recon_row(supplier_name="=HYPERLINK('http://evil', 'click')")
        )
        mapping = dict(zip(RECONCILIATION_COLUMNS, cells, strict=True))
        assert mapping["Supplier"].startswith("'=")


# ---------------------------------------------------------------------------
# unmatched renderers
# ---------------------------------------------------------------------------


def test_unmatched_invoice_cells_shape():
    row = UnmatchedInvoice(
        invoice_id="i-1",
        supplier_name="Stripe",
        invoice_number="INV-1",
        invoice_date=date(2026, 4, 1),
        currency="GBP",
        amount_gross=Decimal("79.00"),
        category="software_saas",
        drive_link="https://drive.example/i1",
    )
    cells = unmatched_invoice_cells(row)
    assert len(cells) == len(UNMATCHED_INVOICE_COLUMNS)
    assert cells[0] == "i-1"


def test_unmatched_txn_cells_shape():
    row = UnmatchedTransaction(
        txn_id="t-1",
        booking_date=date(2026, 4, 1),
        account="amex",
        description="ATLASSIAN",
        currency="GBP",
        amount=Decimal("480.00"),
        amount_gbp=Decimal("480.00"),
        txn_type="purchase",
        category=None,
    )
    cells = unmatched_txn_cells(row)
    assert len(cells) == len(UNMATCHED_TXN_COLUMNS)
    assert cells[1] == "2026-04-01"


# ---------------------------------------------------------------------------
# ExceptionRow + SalesRow + RunStatusRow
# ---------------------------------------------------------------------------


def test_exception_row_renders_all_fields():
    row = ExceptionRow(
        kind="needs_manual_download",
        subject="Zoom billing",
        amount=Decimal("149.90"),
        detected_at=NOW,
        detail="Portal login required",
        action_needed="Download PDF manually",
    )
    cells = exception_cells(row)
    assert cells[0] == "needs_manual_download"
    assert cells[3] == NOW.isoformat()


def test_sales_row_renders_all_fields():
    row = SalesRow(
        row_id="s-1",
        date=date(2026, 4, 5),
        counterparty="Client Ltd",
        currency="GBP",
        amount=Decimal("2500.00"),
        amount_gbp=Decimal("2500.00"),
        matched_invoice=None,
        notes="manual check",
    )
    cells = sales_cells(row)
    assert cells[2] == "Client Ltd"
    assert cells[4] == "2500.00"


def test_run_status_row_flattens_adapters_stably():
    row = RunStatusRow(
        run_id="run-1",
        started_at=NOW,
        ended_at=NOW,
        status="ok",
        adapters={"ms365": "ok", "amex_csv": "ok", "wise": "reauth_required"},
        warnings=("monzo 60d to cliff",),
        errors=(),
        spent_gbp=Decimal("0.0400"),
    )
    cells = run_status_cells(row)
    assert "amex_csv=ok" in cells[4]
    assert "wise=reauth_required" in cells[4]
    assert cells[7] == "0.0400"


# ---------------------------------------------------------------------------
# Sink integration — writes land with the expected shape
# ---------------------------------------------------------------------------


class TestSinkWrites:
    def test_reconciliation_tab_writes_header_and_rows(self):
        sink = InMemorySheetSink()
        write_reconciliation_tab(
            sink, spreadsheet_id="ss-1", rows=[_recon_row()]
        )
        assert len(sink.writes) == 1
        call = sink.writes[0]
        assert call["spreadsheet_id"] == "ss-1"
        assert call["tab"] == "Reconciliation"
        assert call["header"] == RECONCILIATION_COLUMNS
        rows = call["rows"]
        assert len(rows) == 1
        assert len(rows[0]) == len(RECONCILIATION_COLUMNS)

    def test_empty_rowset_still_writes_header(self):
        sink = InMemorySheetSink()
        write_unmatched_invoices_tab(sink, spreadsheet_id="ss-1", rows=[])
        assert sink.writes[0]["rows"] == []

    def test_unmatched_txn_tab_name(self):
        sink = InMemorySheetSink()
        write_unmatched_txns_tab(sink, spreadsheet_id="ss-1", rows=[])
        assert sink.writes[0]["tab"] == "Unmatched"

    def test_exceptions_tab_writes(self):
        sink = InMemorySheetSink()
        write_exceptions_tab(
            sink,
            spreadsheet_id="ss-1",
            rows=[
                ExceptionRow(
                    kind="orphan_refund",
                    subject="Starbucks refund",
                    amount=Decimal("-3.50"),
                    detected_at=NOW,
                    detail="no prior purchase",
                    action_needed="verify",
                )
            ],
        )
        assert sink.writes[0]["tab"] == "Exceptions"
        assert sink.writes[0]["rows"][0][0] == "orphan_refund"

    def test_sales_tab_writes(self):
        sink = InMemorySheetSink()
        write_sales_tab(
            sink,
            spreadsheet_id="ss-1",
            rows=[
                SalesRow(
                    row_id="s-1",
                    date=date(2026, 4, 5),
                    counterparty="Client Ltd",
                    currency="GBP",
                    amount=Decimal("2500.00"),
                    amount_gbp=Decimal("2500.00"),
                    matched_invoice=None,
                    notes="",
                )
            ],
        )
        assert sink.writes[0]["tab"] == "Sales"

    def test_run_status_tab_writes(self):
        sink = InMemorySheetSink()
        write_run_status_tab(
            sink,
            spreadsheet_id="ss-1",
            rows=[
                RunStatusRow(
                    run_id="run-1",
                    started_at=NOW,
                    ended_at=None,
                    status="in_progress",
                    adapters={"ms365": "ok"},
                    warnings=(),
                    errors=(),
                    spent_gbp=Decimal("0"),
                )
            ],
        )
        assert sink.writes[0]["tab"] == "Run Status"


# ---------------------------------------------------------------------------
# Full-round idempotence (run twice → identical writes)
# ---------------------------------------------------------------------------


def test_two_identical_runs_produce_identical_writes():
    sink_a = InMemorySheetSink()
    sink_b = InMemorySheetSink()
    rows = [_recon_row()]
    write_reconciliation_tab(sink_a, spreadsheet_id="ss-1", rows=rows)
    write_reconciliation_tab(sink_b, spreadsheet_id="ss-1", rows=rows)
    assert sink_a.writes == sink_b.writes
