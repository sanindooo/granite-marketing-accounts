"""End-to-end reconciliation: DB → matcher → reconciliation_rows.

The Phase 4 matcher (`reconcile.match`) scores one invoice↔transaction
pair at a time; this module is the glue between it and SQLite. It:

1. Loads every non-deleted invoice in a fiscal year (or every invoice
   overall when no FY is supplied — useful on a fresh run where the
   user has backdated data).
2. For each invoice, fetches candidate transactions within
   ``policy.date_window_days`` of the invoice date (account-agnostic —
   the matcher handles currency + FX internally).
3. Calls :func:`execution.reconcile.match.match_invoice`.
4. Upserts a row in ``reconciliation_rows`` — preserving the user
   state-machine guard from :func:`execution.reconcile.state.preserve_user_state`
   so the script never clobbers ``user_verified`` / ``user_personal``.
5. Writes a matching entry into ``reconciliation_links`` (``link_kind``
   = ``full`` when the matcher produced a concrete txn_id, else the
   link row is absent and the recon row carries ``txn_id IS NULL``).

This intentionally does NOT orchestrate the ingest commands — those
already live on the CLI under ``granite ingest …`` and the run_pipeline
wrapper below invokes them in-process.

Scope kept to a first pass:

- No split-payment / subset-sum integration (that's a separate
  composition using :mod:`execution.reconcile.split`).
- No transfer-pair tagging; the clearing detector writes those links
  directly.
- FY of the recon row = FY of the invoice_date (when matched, we keep
  the invoice-side FY; the plan's full rule uses the matched txn's
  booking date but that's a Phase 5 refinement).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

from execution.reconcile.match import (
    DEFAULT_POLICY as DEFAULT_MATCH_POLICY,
)
from execution.reconcile.match import (
    InvoiceCandidate,
    MatchDecision,
    MatchPolicy,
    MatchState,
    TransactionCandidate,
    match_invoice,
)
from execution.reconcile.state import (
    NULL_SENTINEL,
    RowState,
    Trigger,
    append_history,
    compute_row_id,
    preserve_user_state,
    transition,
)
from execution.shared.fiscal import fy_of

DEFAULT_CANDIDATE_WINDOW_DAYS: Final[int] = 21  # matcher works within ±14, fetch ±21 for slack


@dataclass(frozen=True, slots=True)
class MatchRunStats:
    """Summary of a matcher pass — attached to ``runs.stats_json``."""

    invoices_scanned: int
    auto_matched: int
    suggested: int
    unmatched: int
    rows_written: int
    rows_preserved: int  # user_* states kept intact
    fiscal_year: str | None


def run_matcher(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    fiscal_year: str | None = None,
    policy: MatchPolicy = DEFAULT_MATCH_POLICY,
    now: datetime | None = None,
) -> MatchRunStats:
    """Match every (non-deleted) invoice against candidate transactions.

    ``run_id`` is stamped onto every row we write, so the CLI can filter
    the runs' stats easily.
    ``fiscal_year``, when present, limits the scan to invoices whose
    date falls in that FY — the orchestrator uses it for incremental
    runs.
    """
    now = now or datetime.now(tz=UTC)
    invoices = _load_invoices(conn, fiscal_year)

    auto = suggested = unmatched = 0
    rows_written = rows_preserved = 0

    for inv in invoices:
        candidates = _load_candidates(conn, inv, window_days=policy.date_window_days)
        decision = match_invoice(
            inv,
            candidates,
            policy=policy,
            vendor_confirmed_count=_vendor_confirmed_count(conn, invoice_id=inv.invoice_id),
        )
        written, preserved = _upsert_reconciliation_row(
            conn,
            inv=inv,
            decision=decision,
            run_id=run_id,
            now=now,
        )
        rows_written += written
        rows_preserved += preserved
        if decision.state == MatchState.AUTO_MATCHED:
            auto += 1
        elif decision.state == MatchState.SUGGESTED:
            suggested += 1
        else:
            unmatched += 1

    return MatchRunStats(
        invoices_scanned=len(invoices),
        auto_matched=auto,
        suggested=suggested,
        unmatched=unmatched,
        rows_written=rows_written,
        rows_preserved=rows_preserved,
        fiscal_year=fiscal_year,
    )


# ---------------------------------------------------------------------------
# Candidate loaders
# ---------------------------------------------------------------------------


def _load_invoices(
    conn: sqlite3.Connection,
    fiscal_year: str | None,
) -> list[InvoiceCandidate]:
    """Return every non-deleted invoice, optionally scoped to one FY."""
    sql = """
        SELECT invoice_id, vendor_name_raw, invoice_date, currency,
               amount_gross, amount_gross_gbp
        FROM invoices
        WHERE deleted_at IS NULL
    """
    params: list[object] = []
    if fiscal_year:
        from execution.shared.fiscal import fy_bounds

        start, end = fy_bounds(fiscal_year)
        sql += " AND invoice_date BETWEEN ? AND ?"
        params.extend([start.isoformat(), end.isoformat()])
    rows = conn.execute(sql, params).fetchall()
    invoices: list[InvoiceCandidate] = []
    for row in rows:
        inv_date = _parse_iso_date(row["invoice_date"])
        invoices.append(
            InvoiceCandidate(
                invoice_id=row["invoice_id"],
                supplier_name=row["vendor_name_raw"] or "",
                invoice_date=inv_date,
                currency=row["currency"] or "GBP",
                amount_gross=_to_decimal(row["amount_gross"]),
                amount_gbp_converted=_to_optional_decimal(row["amount_gross_gbp"]),
            )
        )
    return invoices


def _load_candidates(
    conn: sqlite3.Connection,
    inv: InvoiceCandidate,
    *,
    window_days: int,
) -> list[TransactionCandidate]:
    """Transactions inside the invoice's date window, excluding transfers."""
    if inv.invoice_date is None:
        return []
    low = inv.invoice_date - timedelta(days=window_days)
    high = inv.invoice_date + timedelta(days=window_days)
    rows = conn.execute(
        """
        SELECT txn_id, description_canonical, booking_date,
               currency, amount, amount_gbp
        FROM transactions
        WHERE deleted_at IS NULL
          AND booking_date BETWEEN ? AND ?
          AND txn_type NOT IN ('transfer')
          AND status != 'reversed'
        """,
        (low.isoformat(), high.isoformat()),
    ).fetchall()
    candidates: list[TransactionCandidate] = []
    for row in rows:
        candidates.append(
            TransactionCandidate(
                txn_id=row["txn_id"],
                description_canonical=row["description_canonical"] or "",
                booking_date=_parse_iso_date(row["booking_date"]),
                currency=row["currency"] or "GBP",
                amount=abs(_to_decimal(row["amount"])),  # matcher works in absolute space
                amount_gbp=abs(_to_decimal(row["amount_gbp"])),
            )
        )
    return candidates


def _vendor_confirmed_count(conn: sqlite3.Connection, *, invoice_id: str) -> int:
    """How many prior user-confirmed matches exist for this vendor.

    ``vendor_category_hints`` stores the running count per vendor; a
    vendor with zero confirmations is "unproven" and auto-matches get
    demoted per the matcher's Midday-style cap.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(vch.confirmed_count), 0) AS total
        FROM invoices i
        LEFT JOIN vendor_category_hints vch ON vch.vendor_id = i.vendor_id
        WHERE i.invoice_id = ?
        """,
        (invoice_id,),
    ).fetchone()
    if row is None or row["total"] is None:
        return 0
    return int(row["total"])


# ---------------------------------------------------------------------------
# Recon-row writer — state machine guarded
# ---------------------------------------------------------------------------


def _upsert_reconciliation_row(
    conn: sqlite3.Connection,
    *,
    inv: InvoiceCandidate,
    decision: MatchDecision,
    run_id: str,
    now: datetime,
) -> tuple[int, int]:
    """Write / update the invoice's reconciliation_rows entry.

    Returns ``(rows_written, rows_preserved)`` — ``rows_preserved`` is 1
    when we kept a user_* state intact rather than overwriting it.
    """
    script_state = _decision_to_row_state(decision)
    fiscal_year = fy_of(inv.invoice_date) if inv.invoice_date else "FY-UNKNOWN"
    link_kind = "full"
    row_id = compute_row_id(
        fiscal_year=fiscal_year,
        invoice_id=inv.invoice_id,
        txn_id=decision.txn_id or NULL_SENTINEL,
        link_kind=link_kind,
    )

    existing = conn.execute(
        "SELECT state, override_history FROM reconciliation_rows WHERE row_id = ?",
        (row_id,),
    ).fetchone()

    preserved = 0
    if existing is None:
        current_state = RowState.NEW
        history = ""
    else:
        current_state = RowState(existing["state"])
        history = existing["override_history"] or ""

    target_state = preserve_user_state(
        current=current_state, script_proposed=script_state
    )

    # Only record a transition note when the state actually changes.
    if current_state != target_state:
        record = transition(
            current=current_state,
            proposed=target_state,
            trigger=Trigger.SCRIPT,
            at=now,
            note=decision.reason[:120],
        )
        history = append_history(history, record)
    else:
        if current_state != script_state and current_state != RowState.NEW:
            # User state preserved over a script proposal.
            preserved = 1

    score_str = format(decision.score, "f")
    with conn:
        conn.execute(
            """
            INSERT INTO reconciliation_rows (
                row_id, invoice_id, txn_id, fiscal_year, state,
                match_score, match_reason, override_history,
                updated_at, last_run_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(row_id) DO UPDATE SET
                txn_id = CASE
                    WHEN reconciliation_rows.state IN (
                        'user_verified', 'user_overridden',
                        'user_personal', 'user_ignore', 'voided'
                    ) THEN reconciliation_rows.txn_id
                    ELSE excluded.txn_id
                END,
                state = excluded.state,
                match_score = excluded.match_score,
                match_reason = excluded.match_reason,
                override_history = excluded.override_history,
                updated_at = excluded.updated_at,
                last_run_id = excluded.last_run_id
            """,
            (
                row_id,
                inv.invoice_id,
                decision.txn_id,
                fiscal_year,
                target_state.value,
                score_str,
                decision.reason[:512],
                history,
                now.isoformat(),
                run_id,
            ),
        )
        # reconciliation_links: write for matched rows only.
        if decision.txn_id and target_state in (
            RowState.AUTO_MATCHED,
            RowState.SUGGESTED,
            RowState.USER_VERIFIED,
            RowState.USER_OVERRIDDEN,
        ):
            allocated = inv.amount_gross
            conn.execute(
                """
                INSERT INTO reconciliation_links (
                    row_id, invoice_id, txn_id,
                    allocated_amount_gbp, link_kind
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(row_id, COALESCE(invoice_id, ''), COALESCE(txn_id, ''))
                DO UPDATE SET allocated_amount_gbp = excluded.allocated_amount_gbp,
                              link_kind = excluded.link_kind
                """,
                (
                    row_id,
                    inv.invoice_id,
                    decision.txn_id,
                    format(allocated, "f"),
                    link_kind,
                ),
            )

    return 1, preserved


def _decision_to_row_state(decision: MatchDecision) -> RowState:
    return {
        MatchState.AUTO_MATCHED: RowState.AUTO_MATCHED,
        MatchState.SUGGESTED: RowState.SUGGESTED,
        MatchState.UNMATCHED: RowState.UNMATCHED,
    }[decision.state]


# ---------------------------------------------------------------------------
# Decimal + date helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: str | None) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(value)


def _to_optional_decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)


def _parse_iso_date(value: str | None) -> date:
    if not value:
        raise ValueError("missing date")
    if "T" in value:
        return datetime.fromisoformat(value).date()
    return date.fromisoformat(value)


__all__ = [
    "DEFAULT_CANDIDATE_WINDOW_DAYS",
    "MatchRunStats",
    "run_matcher",
]
