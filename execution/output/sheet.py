"""Per-tab row writers for the per-FY Google Sheets workbook.

Phase 1B created the workbook shell + the OAuth + formula-injection
sanitiser. This module adds the row payloads: how many columns each
tab has, the canonical order, and how to render a domain row
(``ReconciliationRow``, unmatched invoice, unmatched transaction,
exception, Run Status event) into a list-of-cells the Sheets API can
batch-update.

Every value written to a cell flows through
:func:`execution.shared.sheet.sanitize_cell` — the single formula-
injection chokepoint. A grep in CI asserts no test bypasses it.

Row-upsert semantics:

- **Keyed tabs** (Reconciliation, Sales) use a stable first-column
  row_id. A row with a matching id overwrites its cell range in-place;
  new rows are appended.
- **Stateless tabs** (Unmatched, Exceptions, Run Status) rewrite the
  whole sheet range each run. Each run re-derives the body from the
  DB so consistency is maintained without tracking sheet state.

Idempotence: the second run with no new inputs produces a byte-identical
sheet, which is the integration-test scenario :scenario:`run-twice`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol

from execution.shared.sheet import sanitize_cell

if TYPE_CHECKING:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# Column schemas — one source of truth per tab
# ---------------------------------------------------------------------------


RECONCILIATION_COLUMNS: Final[tuple[str, ...]] = (
    "Row ID",
    "Fiscal Year",
    "State",
    "Score",
    "Invoice ID",
    "Invoice Number",
    "Invoice Date",
    "Supplier",
    "Category",
    "Currency",
    "Amount Gross",
    "Amount GBP",
    "Transaction ID",
    "Booking Date",
    "Account",
    "Description",
    "Match Reason",
    "Verified",
    "Override Match",
    "Personal?",
    "Ignore?",
    "Category Override",
    "Notes",
    "Drive Link",
)

UNMATCHED_INVOICE_COLUMNS: Final[tuple[str, ...]] = (
    "Invoice ID",
    "Supplier",
    "Invoice Number",
    "Invoice Date",
    "Currency",
    "Amount Gross",
    "Category",
    "Drive Link",
)

UNMATCHED_TXN_COLUMNS: Final[tuple[str, ...]] = (
    "Transaction ID",
    "Booking Date",
    "Account",
    "Description",
    "Currency",
    "Amount",
    "Amount GBP",
    "txn_type",
    "Category",
)

EXCEPTIONS_COLUMNS: Final[tuple[str, ...]] = (
    "Kind",
    "Subject",
    "Amount",
    "Detected At",
    "Detail",
    "Action Needed",
)

SALES_COLUMNS: Final[tuple[str, ...]] = (
    "Row ID",
    "Date",
    "Counterparty",
    "Currency",
    "Amount",
    "Amount GBP",
    "Matched Invoice",
    "Notes",
)

RUN_STATUS_COLUMNS: Final[tuple[str, ...]] = (
    "Run ID",
    "Started At",
    "Ended At",
    "Status",
    "Adapters",
    "Warnings",
    "Errors",
    "Spent (GBP)",
)


# ---------------------------------------------------------------------------
# Domain → cell-row adapters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconciliationRow:
    """Everything the Reconciliation tab row needs."""

    row_id: str
    fiscal_year: str
    state: str
    score: Decimal
    invoice_id: str | None
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    category: str | None
    currency: str | None
    amount_gross: Decimal | None
    amount_gbp: Decimal | None
    txn_id: str | None
    booking_date: date | None
    account: str | None
    description: str | None
    match_reason: str
    verified: bool
    override_match: str | None
    personal_flag: bool
    ignore_flag: bool
    category_override: str | None
    notes: str
    drive_link: str | None


@dataclass(frozen=True, slots=True)
class UnmatchedInvoice:
    invoice_id: str
    supplier_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    currency: str | None
    amount_gross: Decimal | None
    category: str | None
    drive_link: str | None


@dataclass(frozen=True, slots=True)
class UnmatchedTransaction:
    txn_id: str
    booking_date: date
    account: str
    description: str
    currency: str
    amount: Decimal
    amount_gbp: Decimal
    txn_type: str
    category: str | None


@dataclass(frozen=True, slots=True)
class ExceptionRow:
    kind: str
    subject: str
    amount: Decimal | None
    detected_at: datetime
    detail: str
    action_needed: str


@dataclass(frozen=True, slots=True)
class SalesRow:
    row_id: str
    date: date
    counterparty: str
    currency: str
    amount: Decimal
    amount_gbp: Decimal
    matched_invoice: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class RunStatusRow:
    run_id: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    adapters: dict[str, str]  # adapter → "ok" | "reauth_required" | ...
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    spent_gbp: Decimal


# ---------------------------------------------------------------------------
# Cell-row renderers
# ---------------------------------------------------------------------------


def reconciliation_cells(row: ReconciliationRow) -> list[str]:
    return [
        sanitize_cell(row.row_id),
        sanitize_cell(row.fiscal_year),
        sanitize_cell(row.state),
        sanitize_cell(f"{row.score:.4f}"),
        sanitize_cell(row.invoice_id),
        sanitize_cell(row.invoice_number),
        sanitize_cell(_date_or_blank(row.invoice_date)),
        sanitize_cell(row.supplier_name),
        sanitize_cell(row.category),
        sanitize_cell(row.currency),
        sanitize_cell(_decimal_or_blank(row.amount_gross)),
        sanitize_cell(_decimal_or_blank(row.amount_gbp)),
        sanitize_cell(row.txn_id),
        sanitize_cell(_date_or_blank(row.booking_date)),
        sanitize_cell(row.account),
        sanitize_cell(row.description),
        sanitize_cell(row.match_reason),
        sanitize_cell("TRUE" if row.verified else "FALSE"),
        sanitize_cell(row.override_match),
        sanitize_cell("TRUE" if row.personal_flag else "FALSE"),
        sanitize_cell("TRUE" if row.ignore_flag else "FALSE"),
        sanitize_cell(row.category_override),
        sanitize_cell(row.notes),
        sanitize_cell(row.drive_link),
    ]


def unmatched_invoice_cells(row: UnmatchedInvoice) -> list[str]:
    return [
        sanitize_cell(row.invoice_id),
        sanitize_cell(row.supplier_name),
        sanitize_cell(row.invoice_number),
        sanitize_cell(_date_or_blank(row.invoice_date)),
        sanitize_cell(row.currency),
        sanitize_cell(_decimal_or_blank(row.amount_gross)),
        sanitize_cell(row.category),
        sanitize_cell(row.drive_link),
    ]


def unmatched_txn_cells(row: UnmatchedTransaction) -> list[str]:
    return [
        sanitize_cell(row.txn_id),
        sanitize_cell(row.booking_date.isoformat()),
        sanitize_cell(row.account),
        sanitize_cell(row.description),
        sanitize_cell(row.currency),
        sanitize_cell(format(row.amount, "f")),
        sanitize_cell(format(row.amount_gbp, "f")),
        sanitize_cell(row.txn_type),
        sanitize_cell(row.category),
    ]


def exception_cells(row: ExceptionRow) -> list[str]:
    return [
        sanitize_cell(row.kind),
        sanitize_cell(row.subject),
        sanitize_cell(_decimal_or_blank(row.amount)),
        sanitize_cell(row.detected_at.isoformat()),
        sanitize_cell(row.detail),
        sanitize_cell(row.action_needed),
    ]


def sales_cells(row: SalesRow) -> list[str]:
    return [
        sanitize_cell(row.row_id),
        sanitize_cell(row.date.isoformat()),
        sanitize_cell(row.counterparty),
        sanitize_cell(row.currency),
        sanitize_cell(format(row.amount, "f")),
        sanitize_cell(format(row.amount_gbp, "f")),
        sanitize_cell(row.matched_invoice),
        sanitize_cell(row.notes),
    ]


def run_status_cells(row: RunStatusRow) -> list[str]:
    return [
        sanitize_cell(row.run_id),
        sanitize_cell(row.started_at.isoformat()),
        sanitize_cell(row.ended_at.isoformat() if row.ended_at else ""),
        sanitize_cell(row.status),
        sanitize_cell(
            ", ".join(f"{k}={v}" for k, v in sorted(row.adapters.items()))
        ),
        sanitize_cell("; ".join(row.warnings)),
        sanitize_cell("; ".join(row.errors)),
        sanitize_cell(f"{row.spent_gbp:.4f}"),
    ]


# ---------------------------------------------------------------------------
# Abstraction over the sheets transport — keeps tests hermetic
# ---------------------------------------------------------------------------


class SheetSink(Protocol):
    """Narrow interface a renderer uses; mocked in tests."""

    def write_rectangle(
        self,
        *,
        spreadsheet_id: str,
        tab: str,
        header: tuple[str, ...],
        rows: list[list[str]],
    ) -> None: ...


@dataclass
class InMemorySheetSink:
    """Test seam: records every write so assertions can replay."""

    writes: list[dict[str, object]]

    def __init__(self) -> None:
        self.writes = []

    def write_rectangle(
        self,
        *,
        spreadsheet_id: str,
        tab: str,
        header: tuple[str, ...],
        rows: list[list[str]],
    ) -> None:
        self.writes.append(
            {
                "spreadsheet_id": spreadsheet_id,
                "tab": tab,
                "header": tuple(header),
                "rows": [list(r) for r in rows],
            }
        )


def write_reconciliation_tab(
    sink: SheetSink,
    *,
    spreadsheet_id: str,
    rows: list[ReconciliationRow],
) -> None:
    sink.write_rectangle(
        spreadsheet_id=spreadsheet_id,
        tab="Reconciliation",
        header=RECONCILIATION_COLUMNS,
        rows=[reconciliation_cells(r) for r in rows],
    )


def write_unmatched_invoices_tab(
    sink: SheetSink,
    *,
    spreadsheet_id: str,
    rows: list[UnmatchedInvoice],
) -> None:
    sink.write_rectangle(
        spreadsheet_id=spreadsheet_id,
        tab="Unmatched",
        header=UNMATCHED_INVOICE_COLUMNS,
        rows=[unmatched_invoice_cells(r) for r in rows],
    )


def write_unmatched_txns_tab(
    sink: SheetSink,
    *,
    spreadsheet_id: str,
    rows: list[UnmatchedTransaction],
) -> None:
    sink.write_rectangle(
        spreadsheet_id=spreadsheet_id,
        tab="Unmatched",
        header=UNMATCHED_TXN_COLUMNS,
        rows=[unmatched_txn_cells(r) for r in rows],
    )


def write_exceptions_tab(
    sink: SheetSink,
    *,
    spreadsheet_id: str,
    rows: list[ExceptionRow],
) -> None:
    sink.write_rectangle(
        spreadsheet_id=spreadsheet_id,
        tab="Exceptions",
        header=EXCEPTIONS_COLUMNS,
        rows=[exception_cells(r) for r in rows],
    )


def write_sales_tab(
    sink: SheetSink,
    *,
    spreadsheet_id: str,
    rows: list[SalesRow],
) -> None:
    sink.write_rectangle(
        spreadsheet_id=spreadsheet_id,
        tab="Sales",
        header=SALES_COLUMNS,
        rows=[sales_cells(r) for r in rows],
    )


def write_run_status_tab(
    sink: SheetSink,
    *,
    spreadsheet_id: str,
    rows: list[RunStatusRow],
) -> None:
    sink.write_rectangle(
        spreadsheet_id=spreadsheet_id,
        tab="Run Status",
        header=RUN_STATUS_COLUMNS,
        rows=[run_status_cells(r) for r in rows],
    )


# ---------------------------------------------------------------------------
# Internal formatting helpers
# ---------------------------------------------------------------------------


def _decimal_or_blank(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _date_or_blank(value: date | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


__all__ = [
    "EXCEPTIONS_COLUMNS",
    "RECONCILIATION_COLUMNS",
    "RUN_STATUS_COLUMNS",
    "SALES_COLUMNS",
    "UNMATCHED_INVOICE_COLUMNS",
    "UNMATCHED_TXN_COLUMNS",
    "ExceptionRow",
    "InMemorySheetSink",
    "ReconciliationRow",
    "RunStatusRow",
    "SalesRow",
    "SheetSink",
    "UnmatchedInvoice",
    "UnmatchedTransaction",
    "exception_cells",
    "reconciliation_cells",
    "run_status_cells",
    "sales_cells",
    "unmatched_invoice_cells",
    "unmatched_txn_cells",
    "write_exceptions_tab",
    "write_reconciliation_tab",
    "write_run_status_tab",
    "write_sales_tab",
    "write_unmatched_invoices_tab",
    "write_unmatched_txns_tab",
]
