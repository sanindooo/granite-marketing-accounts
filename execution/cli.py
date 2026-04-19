"""`granite` CLI — the single entrypoint for every operational command.

Directives reference ``granite <subcommand>`` so the file tree can refactor
without touching documentation. Phase 1A ships the shell + the subcommands
that exercise the foundation (``granite db migrate``, ``granite db status``,
``granite ops health``). Later phases mount ``ingest``, ``reconcile``, and
``output`` sub-apps.

Every leaf command prints a single JSON document via ``emit_success`` or
``emit_error``, per the agent-native output standard in ``CLAUDE.md``.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:  # pragma: no cover
    from execution.output.sheet import (
        ReconciliationRow,
        UnmatchedInvoice,
        UnmatchedTransaction,
    )

import typer

from execution.shared import db as db_mod
from execution.shared.errors import (
    ConfigError,
    PipelineError,
    emit_error,
    emit_progress,
    emit_success,
)
from execution.shared.fiscal import london_today_fy
from execution.shared.secrets import ensure_backend, is_mock

app = typer.Typer(
    name="granite",
    help="Granite Marketing accounting pipeline — invoice ingestion + reconciliation.",
    no_args_is_help=True,
    add_completion=False,
)

db_app = typer.Typer(name="db", help="Database bootstrap + inspection.", no_args_is_help=True)
ops_app = typer.Typer(name="ops", help="Operational commands (health, reauth, backup).", no_args_is_help=True)
ingest_app = typer.Typer(name="ingest", help="Email + bank ingestion (Phase 2+).", no_args_is_help=True)
reconcile_app = typer.Typer(name="reconcile", help="Matching engine (Phase 4).", no_args_is_help=True)
output_app = typer.Typer(name="output", help="Sheet + sales output (Phase 4).", no_args_is_help=True)
vendors_app = typer.Typer(name="vendors", help="List and search known vendors.", no_args_is_help=True)

app.add_typer(db_app)
app.add_typer(ops_app)
app.add_typer(ingest_app)
app.add_typer(reconcile_app)
app.add_typer(output_app)
app.add_typer(vendors_app)


# ---------------------------------------------------------------------------
# db subcommands
# ---------------------------------------------------------------------------

@db_app.command("migrate")
def db_migrate(
    db_path: Annotated[
        Path | None,
        typer.Option("--db", help="Override DB path (default: .state/pipeline.db)."),
    ] = None,
) -> None:
    """Apply pending migrations to the pipeline DB."""
    try:
        conn = db_mod.connect(db_path)
        ran = db_mod.apply_migrations(conn)
        version = db_mod.current_version(conn)
        emit_success(
            {
                "db_path": str(db_path or db_mod.default_db_path()),
                "migrations_applied": ran,
                "current_version": version,
            }
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


@db_app.command("status")
def db_status(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Report current schema version + PRAGMAs."""
    try:
        conn = db_mod.connect(db_path)
        version = db_mod.current_version(conn)
        pragmas = {
            name: conn.execute(f"PRAGMA {name};").fetchone()[0]
            for name in ("journal_mode", "synchronous", "foreign_keys", "cache_size")
        }
        emit_success(
            {
                "db_path": str(db_path or db_mod.default_db_path()),
                "schema_version": version,
                "pragmas": pragmas,
            }
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


@db_app.command("backfill-fx")
def db_backfill_fx(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be updated without making changes."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-process invoices that previously had FX errors."),
    ] = False,
) -> None:
    """Backfill amount_gross_gbp for existing invoices using their invoice dates."""
    from execution.shared.fx import get_rate_to_gbp
    from execution.shared.money import to_money

    try:
        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)

        # Find invoices that need FX conversion
        where_clause = """
            amount_gross IS NOT NULL
            AND currency IS NOT NULL
            AND invoice_date IS NOT NULL
            AND deleted_at IS NULL
        """
        if force:
            where_clause += " AND (amount_gross_gbp IS NULL OR fx_error IS NOT NULL)"
        else:
            where_clause += " AND amount_gross_gbp IS NULL"

        # where_clause is built from constants, not user input
        invoices = conn.execute(
            f"""
            SELECT invoice_id, currency, amount_gross, invoice_date, fx_error
            FROM invoices
            WHERE {where_clause}
            ORDER BY invoice_date
            """,  # noqa: S608
        ).fetchall()

        total = len(invoices)
        if total == 0:
            emit_success({"message": "No invoices need FX backfill", "processed": 0, "errors": 0})
            return

        if dry_run:
            emit_success({
                "message": f"Dry run: would process {total} invoices",
                "invoices": [
                    {
                        "invoice_id": row["invoice_id"],
                        "currency": row["currency"],
                        "amount_gross": row["amount_gross"],
                        "invoice_date": row["invoice_date"],
                        "previous_error": row["fx_error"],
                    }
                    for row in invoices[:20]  # Limit preview
                ],
                "total": total,
                "showing": min(20, total),
            })
            return

        processed = 0
        errors = 0
        results: list[dict[str, str | None]] = []

        for row in invoices:
            invoice_id = row["invoice_id"]
            currency = row["currency"]
            amount_gross = row["amount_gross"]
            invoice_date = row["invoice_date"]

            rate, err = get_rate_to_gbp(conn, currency, invoice_date)

            if rate is not None:
                try:
                    gross = Decimal(amount_gross)
                    converted = to_money(gross * rate, "GBP")
                    conn.execute(
                        """
                        UPDATE invoices
                        SET amount_gross_gbp = ?, fx_rate_used = ?, fx_error = NULL
                        WHERE invoice_id = ?
                        """,
                        (str(converted), str(rate), invoice_id),
                    )
                    processed += 1
                    results.append({
                        "invoice_id": invoice_id,
                        "status": "converted",
                        "amount_gross_gbp": str(converted),
                        "rate": str(rate),
                    })
                except Exception as e:
                    errors += 1
                    conn.execute(
                        "UPDATE invoices SET fx_error = ? WHERE invoice_id = ?",
                        (f"conversion failed: {e}", invoice_id),
                    )
                    results.append({
                        "invoice_id": invoice_id,
                        "status": "error",
                        "error": f"conversion failed: {e}",
                    })
            else:
                errors += 1
                conn.execute(
                    "UPDATE invoices SET fx_error = ? WHERE invoice_id = ?",
                    (err, invoice_id),
                )
                results.append({
                    "invoice_id": invoice_id,
                    "status": "error",
                    "error": err,
                })

            # Progress output every 50 invoices
            if (processed + errors) % 50 == 0:
                sys.stderr.write(f"\rProcessed {processed + errors}/{total}...")
                sys.stderr.flush()

        if total > 0:
            sys.stderr.write(f"\rProcessed {processed + errors}/{total}   \n")
            sys.stderr.flush()

        emit_success({
            "message": f"Backfill complete: {processed} converted, {errors} errors",
            "processed": processed,
            "errors": errors,
            "total": total,
            "results": results[:50] if len(results) <= 50 else [*results[:25], {"...": f"{len(results) - 50} more"}, *results[-25:]],
        })
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# ops subcommands (minimal Phase 1A surface)
# ---------------------------------------------------------------------------

@ops_app.command("healthcheck")
def ops_healthcheck(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help=(
                "Directory that holds pipeline.db; defaults to the same "
                "directory as the DB path."
            ),
        ),
    ] = None,
) -> None:
    """Pre-run healthcheck — JSON payload + non-zero exit on failure."""
    try:
        import json as _json

        from execution.ops.healthcheck import run_healthcheck

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)
        default_state = db_mod.default_db_path().parent
        resolved_state = state_dir or (
            db_path.parent if db_path is not None else default_state
        )
        report = run_healthcheck(conn, state_dir=resolved_state)
        payload = {
            "checks": report.checks,
            "warnings": list(report.warnings),
            "errors": list(report.errors),
            "healthy": report.healthy,
        }
        status = "success" if report.healthy else "error"
        sys.stdout.write(_json.dumps({"status": status, **payload}, default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()
        if not report.healthy:
            raise typer.Exit(code=1)
    except PipelineError as err:
        emit_error(err)
    except typer.Exit:
        raise
    except Exception as err:
        emit_error(err)


@ops_app.command("health")
def ops_health(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Phase 1A health probe: Keychain backend ok, DB openable, FY computed."""
    checks: dict[str, str | bool | int | None] = {}
    warnings: list[str] = []
    errors: list[str] = []

    # Keychain backend
    try:
        if is_mock():
            checks["keyring_backend"] = "mock"
        else:
            backend = ensure_backend()
            checks["keyring_backend"] = type(backend).__name__
    except PipelineError as err:
        errors.append(f"keyring: {err.user_message}")
        checks["keyring_backend"] = None

    # Database
    try:
        conn = db_mod.connect(db_path)
        checks["schema_version"] = db_mod.current_version(conn)
        checks["foreign_keys"] = bool(conn.execute("PRAGMA foreign_keys;").fetchone()[0])
        if checks["schema_version"] is None:
            warnings.append("no migrations applied — run `granite db migrate`")
    except sqlite3.Error as err:
        errors.append(f"database: {err}")

    # Fiscal year
    try:
        checks["fiscal_year"] = london_today_fy()
    except Exception as err:
        errors.append(f"fiscal_year: {err}")

    payload = {
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "healthy": not errors,
    }
    if errors:
        # Still emit a structured document, but exit non-zero.
        import json

        sys.stdout.write(json.dumps({"status": "error", **payload}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise typer.Exit(code=1)
    emit_success(payload)


@ops_app.command("smoke-claude")
def ops_smoke_claude(
    budget_gbp: Annotated[
        str,
        typer.Option("--budget", help="Per-run budget ceiling in GBP."),
    ] = "0.05",
) -> None:
    """Send one cheap Haiku 4.5 ping to prove the Claude wiring works."""
    try:
        from execution.shared.claude_client import ClaudeClient

        client = ClaudeClient(budget_gbp=Decimal(budget_gbp))
        call = client.smoke()
        emit_success(
            {
                "model": call.model,
                "input_tokens": call.usage.input_tokens,
                "output_tokens": call.usage.output_tokens,
                "cost_gbp": format(call.cost_gbp, "f"),
                "budget": client.budget.stats(),
            }
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


@ops_app.command("setup-sheets")
def ops_setup_sheets() -> None:
    """Run the Google OAuth flow and cache a refresh-capable token."""
    try:
        from execution.shared import sheet as sheet_mod

        sheet_mod.load_credentials()
        emit_success(
            {
                "token_path": str(sheet_mod.token_path()),
                "scopes": list(sheet_mod.SCOPES),
            }
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# output subcommands (Phase 1B + Phase 4)
# ---------------------------------------------------------------------------


@output_app.command("create-fy")
def output_create_fy(
    fiscal_year: Annotated[str, typer.Argument(help="FY label, e.g. FY-2026-27.")],
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Create the Drive folder + Sheets workbook for ``fiscal_year``."""
    try:
        from execution.shared import sheet as sheet_mod

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)
        clients = sheet_mod.GoogleClients.connect()
        fy_sheet = sheet_mod.create_fy_workbook(clients, conn, fiscal_year)
        emit_success(
            {
                "fiscal_year": fy_sheet.fiscal_year,
                "spreadsheet_id": fy_sheet.spreadsheet_id,
                "drive_folder_id": fy_sheet.drive_folder_id,
                "web_view_link": fy_sheet.web_view_link,
            }
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# ingest email subcommands (Phase 2)
# ---------------------------------------------------------------------------


ingest_email_app = typer.Typer(
    name="email", help="Email-adapter ingestion.", no_args_is_help=True
)
ingest_app.add_typer(ingest_email_app)


@ingest_email_app.command("ms365")
def ingest_email_ms365(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    initial: Annotated[
        bool,
        typer.Option(
            "--initial",
            help="Ignore the saved watermark and start a fresh delta sync.",
        ),
    ] = False,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Clear all synced emails and watermark, then do a full inbox search (90 days back).",
        ),
    ] = False,
    sender: Annotated[
        str | None,
        typer.Option(
            "--sender",
            help="Search for emails from a specific sender (e.g., 'uber', 'anthropic').",
        ),
    ] = None,
    date_from: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Only fetch emails received on or after this date (YYYY-MM-DD).",
        ),
    ] = None,
    date_to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="Only fetch emails received on or before this date (YYYY-MM-DD).",
        ),
    ] = None,
    backfill_from: Annotated[
        str | None,
        typer.Option(
            "--backfill-from",
            help="Backfill historical emails from this date (YYYY-MM-DD), then set up delta sync for future runs.",
        ),
    ] = None,
    rescan: Annotated[
        bool,
        typer.Option(
            "--rescan",
            help="Re-scan emails even if already in database. Updates existing records and clears processed status for re-processing.",
        ),
    ] = False,
) -> None:
    """Fetch new MS Graph inbox messages into the ``emails`` table.

    Classification + extraction + filing run in a later stage; this command
    only handles the ingest → email-row side so each concern stays
    separately runnable and observable.

    Use --sender to search for emails from a specific company (e.g., --sender uber).
    Use --from and --to to limit the date range.
    Use --backfill-from to capture historical emails and set up incremental sync.
    Use --rescan to re-fetch emails that are already in the database.
    """
    try:
        from datetime import datetime, timedelta

        from execution.adapters.ms365 import SOURCE_ID, Ms365Adapter, Ms365Auth

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)

        # Reset mode: clear everything and start fresh
        if reset:
            emit_progress("reset", 0, 0, "Clearing synced emails and watermark")
            with conn:
                # Delete in correct order due to foreign key constraints
                conn.execute("DELETE FROM invoices")  # References emails, must go first
                conn.execute("DELETE FROM emails WHERE source_adapter = 'ms365'")
                conn.execute("DELETE FROM watermarks WHERE source = ?", (SOURCE_ID,))
            # Set date_from to 90 days back for full resync
            reset_from = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            emit_progress("reset", 0, 0, f"Will search emails from {reset_from}")
            date_from = reset_from

        run_id = _begin_run(conn, kind="email", operation="ingest_email")

        auth = Ms365Auth.from_keychain()
        adapter = Ms365Adapter(auth=auth)
        try:
            batches = 0
            emails = 0
            skipped = 0
            backfill_emails = 0

            # Backfill mode: search historical + set up delta sync
            if backfill_from:
                # Phase 1: Backfill historical emails via search
                rescan_msg = " (rescan mode)" if rescan else ""
                emit_progress("backfill_search", 0, 0, f"Searching emails from {backfill_from}{rescan_msg}")
                scanned = 0
                updated = 0
                for batch in adapter.search_inbox(date_from=backfill_from):
                    batches += 1
                    with conn:
                        for email in batch:
                            scanned += 1
                            existing = conn.execute(
                                "SELECT 1 FROM emails WHERE msg_id = ?",
                                (email.msg_id,),
                            ).fetchone()
                            if existing:
                                if rescan:
                                    # Clear processed status so it gets re-processed
                                    conn.execute(
                                        """UPDATE emails SET
                                            processed_at = NULL, outcome = NULL,
                                            classifier_version = NULL, error_code = NULL
                                        WHERE msg_id = ?""",
                                        (email.msg_id,),
                                    )
                                    updated += 1
                                else:
                                    skipped += 1
                                continue
                            _upsert_email(conn, email.as_email_row())
                            backfill_emails += 1
                    if rescan:
                        emit_progress("backfill_search", backfill_emails + updated, scanned, f"Scanned {scanned}: {backfill_emails} new, {updated} refreshed")
                    else:
                        emit_progress("backfill_search", backfill_emails, scanned, f"Scanned {scanned} emails: {backfill_emails} new, {skipped} already synced")
                    _update_run_stats(conn, run_id=run_id, stats={"emails": backfill_emails, "skipped": skipped, "updated": updated, "scanned": scanned, "phase": "backfill"})
                emails += backfill_emails + updated

                # Phase 2: Run delta sync to establish watermark for future runs
                # We just need the watermark - skip DB checks since we already captured emails
                emit_progress("backfill_delta", 0, 0, "Setting up incremental sync (this may take a while for large inboxes)")
                delta_pages = 0
                for batch in adapter.fetch_since(None):
                    delta_pages += 1
                    # Skip processing - we already have these emails from the search
                    # Just iterate to get the watermark at the end
                    emit_progress("backfill_delta", delta_pages, 0, f"Setting up sync... (page {delta_pages})")
                _save_watermark(
                    conn, SOURCE_ID, watermark=adapter.next_watermark, emit_count=emails
                )

            # Search mode: use filters but don't set up delta sync
            elif sender or date_from or date_to:
                filter_desc = sender or date_from or "filtered"
                rescan_msg = " (rescan mode)" if rescan else ""
                emit_progress("search", 0, 0, f"Searching emails: {filter_desc}{rescan_msg}")
                scanned = 0
                updated = 0
                for batch in adapter.search_inbox(
                    sender=sender, date_from=date_from, date_to=date_to
                ):
                    batches += 1
                    with conn:
                        for email in batch:
                            scanned += 1
                            existing = conn.execute(
                                "SELECT 1 FROM emails WHERE msg_id = ?",
                                (email.msg_id,),
                            ).fetchone()
                            if existing:
                                if rescan:
                                    # Clear processed status so it gets re-processed
                                    conn.execute(
                                        """UPDATE emails SET
                                            processed_at = NULL, outcome = NULL,
                                            classifier_version = NULL, error_code = NULL
                                        WHERE msg_id = ?""",
                                        (email.msg_id,),
                                    )
                                    updated += 1
                                else:
                                    skipped += 1
                                continue
                            _upsert_email(conn, email.as_email_row())
                            emails += 1
                    _update_run_stats(conn, run_id=run_id, stats={"emails": emails, "skipped": skipped, "updated": updated, "scanned": scanned, "phase": "search"})
                    if rescan:
                        emit_progress("search", emails + updated, scanned, f"Scanned {scanned}: {emails} new, {updated} reset for re-processing")
                    else:
                        emit_progress("search", emails, scanned, f"Scanned {scanned} emails: {emails} new, {skipped} already synced")

            # Standard delta sync
            else:
                watermark = None if initial else _load_watermark(conn, SOURCE_ID)

                # First-run: do a full inbox scan, not delta sync
                # Delta sync only returns emails that arrive AFTER initialization,
                # so existing emails would be missed
                if watermark is None:
                    emit_progress("sync", 0, 0, "First run - scanning recent emails")
                    scanned = 0
                    for batch in adapter.search_inbox(max_pages=50):
                        batches += 1
                        with conn:
                            for email in batch:
                                scanned += 1
                                existing = conn.execute(
                                    "SELECT 1 FROM emails WHERE msg_id = ?",
                                    (email.msg_id,),
                                ).fetchone()
                                if existing:
                                    skipped += 1
                                    continue
                                _upsert_email(conn, email.as_email_row())
                                emails += 1
                        emit_progress("sync", emails, scanned, f"Scanned {scanned} emails: {emails} new, {skipped} already synced")
                        _update_run_stats(conn, run_id=run_id, stats={"emails": emails, "skipped": skipped, "scanned": scanned, "phase": "scan"})
                    # Now set up delta sync for future runs - just get the watermark
                    emit_progress("sync", emails, scanned, "Setting up incremental sync...")
                    delta_pages = 0
                    for batch in adapter.fetch_since(None):
                        delta_pages += 1
                        emit_progress("sync", emails, scanned, f"Setting up sync... (page {delta_pages})")
                    _save_watermark(
                        conn, SOURCE_ID, watermark=adapter.next_watermark, emit_count=emails
                    )
                else:
                    # Incremental sync with existing watermark
                    emit_progress("sync", 0, 0, "Fetching new emails")
                    for batch in adapter.fetch_since(watermark):
                        batches += 1
                        emails += len(batch)
                        with conn:
                            for email in batch:
                                _upsert_email(conn, email.as_email_row())
                        emit_progress("sync", emails, 0, f"Synced {emails} emails")
                        _update_run_stats(conn, run_id=run_id, stats={"emails": emails, "phase": "incremental"})
                    _save_watermark(
                        conn, SOURCE_ID, watermark=adapter.next_watermark, emit_count=emails
                    )

            _clear_reauth(conn, SOURCE_ID)
            _complete_run(
                conn,
                run_id=run_id,
                status="ok",
                stats={"emails": emails, "batches": batches, "skipped": skipped},
            )
        except Exception:
            _complete_run(conn, run_id=run_id, status="failed", stats={})
            raise
        finally:
            adapter.close()

        result = {
            "run_id": run_id,
            "source": SOURCE_ID,
            "batches": batches,
            "emails": emails,
        }
        if backfill_from:
            result["backfill_mode"] = True
            result["backfill_from"] = backfill_from
            result["backfill_emails"] = backfill_emails
            result["skipped_duplicates"] = skipped
            result["watermark_saved"] = adapter.next_watermark is not None
        elif sender or date_from or date_to:
            result["search_mode"] = True
            result["skipped_duplicates"] = skipped
            if sender:
                result["sender_filter"] = sender
            if date_from:
                result["date_from"] = date_from
            if date_to:
                result["date_to"] = date_to
        else:
            result["next_watermark_saved"] = adapter.next_watermark is not None
            result["initial"] = initial

        emit_success(result)
    except PipelineError as err:
        if err.error_code == "needs_reauth":
            conn = db_mod.connect(db_path)
            db_mod.apply_migrations(conn)
            _record_reauth(conn, err.source, message=str(err))
        emit_error(err)
    except Exception as err:
        emit_error(err)


@ingest_email_app.command("body")
def ingest_email_body(
    msg_id: Annotated[str, typer.Argument(help="The message ID to fetch body for.")],
) -> None:
    """Fetch and return the email body for a given message ID."""
    try:
        from execution.adapters.ms365 import Ms365Adapter, Ms365Auth

        auth = Ms365Auth.from_keychain()
        adapter = Ms365Adapter(auth=auth)
        try:
            body_html, body_text = adapter.fetch_message_body_both(msg_id)
            emit_success({
                "msg_id": msg_id,
                "body_html": body_html,
                "body_text": body_text,
            })
        finally:
            adapter.close()
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


@ingest_email_app.command("pending")
def ingest_email_pending(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max items to return.")] = 50,
) -> None:
    """List emails that need attention (manual download, errors, no attachment)."""
    try:
        from execution.shared import db as db_mod

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)
        rows = conn.execute(
            """
            SELECT msg_id, from_addr, subject, received_at, outcome
            FROM emails
            WHERE outcome IN ('needs_manual_download', 'error', 'no_attachment')
              AND dismissed_at IS NULL
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        emit_success({
            "count": len(rows),
            "items": [
                {
                    "msg_id": r[0],
                    "from_addr": r[1],
                    "subject": r[2],
                    "received_at": r[3],
                    "outcome": r[4],
                }
                for r in rows
            ],
        })
    except Exception as err:
        emit_error(err)


@ingest_email_app.command("dismiss")
def ingest_email_dismiss(
    msg_id: Annotated[str, typer.Argument(help="The message ID to dismiss.")],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Reason: not_invoice, resolved, or duplicate."),
    ] = "resolved",
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Dismiss an email from the needs-attention queue."""
    try:
        from execution.shared import db as db_mod
        from execution.shared.clock import now_utc

        valid_reasons = ("not_invoice", "resolved", "duplicate")
        if reason not in valid_reasons:
            emit_error(ValueError(f"Invalid reason. Must be one of: {valid_reasons}"))
            return

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)

        row = conn.execute(
            "SELECT from_addr, subject FROM emails WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if not row:
            emit_error(ValueError(f"Email not found: {msg_id}"))
            return

        from_addr, subject = row
        domain = from_addr.split("@")[1] if "@" in from_addr else ""
        now = now_utc()

        conn.execute(
            "UPDATE emails SET dismissed_at = ?, dismissed_reason = ? WHERE msg_id = ?",
            (now, reason, msg_id),
        )
        conn.execute(
            """
            INSERT INTO email_feedback (msg_id, feedback_type, feedback_value, from_addr, subject, sender_domain, created_at)
            VALUES (?, 'dismiss', ?, ?, ?, ?, ?)
            """,
            (msg_id, reason, from_addr, subject, domain, now),
        )
        conn.commit()

        emit_success({
            "msg_id": msg_id,
            "reason": reason,
            "domain": domain,
        })
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# ingest invoice subcommands (Phase 2)
# ---------------------------------------------------------------------------


ingest_invoice_app = typer.Typer(
    name="invoice", help="Invoice classification + extraction + filing.", no_args_is_help=True
)
ingest_app.add_typer(ingest_invoice_app)


@ingest_invoice_app.command("process")
def ingest_invoice_process(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    budget: Annotated[
        str,
        typer.Option("--budget", help="Per-run budget ceiling in GBP (default: 2.00)."),
    ] = "2.00",
    backfill: Annotated[
        bool,
        typer.Option(
            "--backfill",
            help="Backfill mode: £20 budget, 1h cache TTL.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Process at most N emails."),
    ] = None,
    tmp_root: Annotated[
        Path | None,
        typer.Option("--tmp", help="Override temp directory (default: .tmp)."),
    ] = None,
    workers: Annotated[
        int,
        typer.Option("--workers", help="Number of concurrent workers (1-20)."),
    ] = 5,
    model: Annotated[
        str,
        typer.Option("--model", help="LLM provider: claude or openai."),
    ] = "openai",
) -> None:
    """Classify, extract, and file pending emails as invoices.

    Processes all emails with ``processed_at IS NULL``. Each email is:
    1. Classified via GPT-4o-mini or Claude Haiku (invoice | receipt | statement | neither).
    2. If invoice/receipt: PDF extracted, data extracted via LLM.
    3. Filed to Google Drive and written to the ``invoices`` table.

    Use ``--backfill`` for initial bulk processing (higher budget, longer cache).
    Use ``--workers N`` to process N emails in parallel (default: 5).
    Use ``--model claude`` for Claude Haiku (more accurate, higher cost).
    """
    try:
        from execution.adapters.ms365 import Ms365Adapter, Ms365Auth
        from execution.invoice.processor import (
            BACKFILL_BUDGET_GBP,
            process_pending_emails,
        )
        from execution.shared.budget import SharedBudget
        from execution.shared.claude_client import HAIKU, ClaudeClient
        from execution.shared.prompts import (
            CLASSIFIER_WEIGHTS,
            EXTRACTOR_WEIGHTS,
            load_prompt,
        )
        from execution.shared.sheet import GoogleClients

        # Validate model parameter
        if model not in ("claude", "openai"):
            emit_error(f"Invalid model: {model}. Must be 'claude' or 'openai'.")
            raise typer.Exit(1)

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)
        run_id = _begin_run(conn, kind="invoice", operation="ingest_invoice")

        # Setup budget and LLM client with thread-safe SharedBudget
        budget_gbp = BACKFILL_BUDGET_GBP if backfill else Decimal(budget)
        shared_budget = SharedBudget(ceiling_gbp=budget_gbp)

        if model == "openai":
            from execution.shared.openai_client import OpenAIClient
            llm_client = OpenAIClient(budget=shared_budget)
            model_id = "gpt-4o-mini"
        else:
            ttl = "1h" if backfill else "5m"
            llm_client = ClaudeClient(shared_budget=shared_budget, ttl=ttl)
            model_id = HAIKU

        # Load prompts (model_id used for token estimation)
        classifier_prompt = load_prompt("classifier", model_id=model_id, weights=CLASSIFIER_WEIGHTS)
        extractor_prompt = load_prompt("extractor", model_id=model_id, weights=EXTRACTOR_WEIGHTS)

        # Setup adapters
        auth = Ms365Auth.from_keychain()
        adapter = Ms365Adapter(auth=auth)
        google = GoogleClients.connect()

        # Resolve tmp directory
        resolved_tmp = tmp_root or Path(".tmp")
        resolved_tmp.mkdir(parents=True, exist_ok=True)

        def progress_callback(current: int, total: int, detail: str) -> None:
            emit_progress("process", current, total, detail)
            # Update stats incrementally so interrupted runs show partial progress
            _update_run_stats(conn, run_id=run_id, stats={"processed": current, "total": total, "detail": detail})

        emit_progress("process", 0, 0, f"Starting invoice processing with {workers} worker(s)")
        try:
            stats = process_pending_emails(
                conn,
                adapter=adapter,
                llm_client=llm_client,
                google=google,
                classifier_prompt=classifier_prompt,
                extractor_prompt=extractor_prompt,
                tmp_root=resolved_tmp,
                limit=limit,
                on_progress=progress_callback,
                workers=workers,
                db_path=db_path,
            )
            _complete_run(
                conn,
                run_id=run_id,
                status="ok",
                stats={
                    "processed": stats.processed,
                    "filed": stats.filed,
                    "errors": stats.errors,
                    "cost_gbp": str(stats.cost_gbp),
                },
            )
            emit_success(
                {
                    "run_id": run_id,
                    "processed": stats.processed,
                    "classified_invoice": stats.classified_invoice,
                    "classified_receipt": stats.classified_receipt,
                    "classified_statement": stats.classified_statement,
                    "classified_neither": stats.classified_neither,
                    "filed": stats.filed,
                    "duplicates": stats.duplicates,
                    "needs_manual_download": stats.needs_manual_download,
                    "errors": stats.errors,
                    "cost_gbp": format(stats.cost_gbp, ".4f"),
                    "budget_gbp": format(budget_gbp, ".2f"),
                    "backfill": backfill,
                }
            )
        except Exception:
            _complete_run(conn, run_id=run_id, status="failed", stats={})
            raise
        finally:
            adapter.close()

    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


@ingest_invoice_app.command("upload-pdf")
def ingest_invoice_upload_pdf(
    msg_id: Annotated[str, typer.Argument(help="The message ID to attach the PDF to.")],
    pdf_path: Annotated[Path, typer.Argument(help="Path to the PDF file.")],
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    tmp_root: Annotated[
        Path | None,
        typer.Option("--tmp", help="Override temp directory (default: .tmp)."),
    ] = None,
) -> None:
    """Upload a manually downloaded PDF for a flagged invoice.

    Use this when the invoice PDF couldn't be auto-fetched (login-gated vendors,
    expired URLs). The PDF will be extracted and filed to Google Drive.
    """
    try:
        from datetime import date as date_type

        from execution.invoice.category import resolve_category
        from execution.invoice.extractor import ExtractorInput, extract_invoice
        from execution.invoice.filer import FilerInput, file_invoice
        from execution.shared.claude_client import HAIKU, ClaudeClient
        from execution.shared.clock import now_utc
        from execution.shared.prompts import EXTRACTOR_WEIGHTS, load_prompt
        from execution.shared.sheet import GoogleClients

        if not pdf_path.exists():
            emit_error(FileNotFoundError(f"PDF not found: {pdf_path}"))
            return

        if pdf_path.suffix.lower() != ".pdf":
            emit_error(ValueError("File must be a PDF"))
            return

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)

        email = conn.execute(
            "SELECT from_addr, subject, received_at, outcome FROM emails WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if not email:
            emit_error(ValueError(f"Email not found: {msg_id}"))
            return

        from_addr, subject, received_at, outcome = email
        if outcome not in ("needs_manual_download", "error", "no_attachment"):
            emit_error(ValueError(f"Email doesn't need manual upload (outcome: {outcome})"))
            return

        sender_domain = from_addr.split("@")[1] if "@" in from_addr else None
        pdf_bytes = pdf_path.read_bytes()

        # Extract text from PDF
        import io

        import pdfplumber

        text_parts: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
        source_text = "\n\n".join(text_parts) if text_parts else ""

        # Parse received date
        received_date = date_type.fromisoformat(received_at[:10])

        claude = ClaudeClient(budget_gbp=Decimal("1.00"), ttl="5m")
        extractor_prompt = load_prompt("extractor", model_id=HAIKU, weights=EXTRACTOR_WEIGHTS)
        google = GoogleClients.connect()

        resolved_tmp = tmp_root or Path(".tmp")
        resolved_tmp.mkdir(parents=True, exist_ok=True)

        # Build extractor input
        extractor_input = ExtractorInput(
            subject=subject,
            sender=from_addr,
            source_text=source_text,
            email_received_date=received_date,
        )

        # Run extraction
        extraction_outcome = extract_invoice(claude, extractor_prompt, extractor_input)
        extraction = extraction_outcome.result

        # Resolve category
        category_decision = resolve_category(
            vendor_name=extraction.supplier_name,
            sender_domain=sender_domain,
        )

        filer_input = FilerInput(
            source_msg_id=msg_id,
            attachment_index=0,
            pdf_bytes=pdf_bytes,
            extraction=extraction,
            extractor_version=extractor_prompt.version,
            invoice_number_confidence=extraction.field_confidence.invoice_number,
            category=category_decision.category,
            sender_domain=sender_domain,
            tmp_root=resolved_tmp,
        )

        filed = file_invoice(google, conn, filer_input)

        conn.execute(
            "UPDATE emails SET processed_at = ?, outcome = ? WHERE msg_id = ?",
            (now_utc(), "invoice", msg_id),
        )
        conn.commit()

        emit_success({
            "msg_id": msg_id,
            "invoice_id": filed.invoice_id,
            "outcome": filed.outcome.value,
            "drive_file_id": filed.drive_file_id,
            "vendor_id": filed.vendor_id,
        })

    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# ingest bank subcommands (Phase 3)
# ---------------------------------------------------------------------------


ingest_bank_app = typer.Typer(
    name="bank", help="Bank-adapter ingestion.", no_args_is_help=True
)
ingest_app.add_typer(ingest_bank_app)


@ingest_bank_app.command("monzo")
def ingest_bank_monzo(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    initial: Annotated[
        bool,
        typer.Option(
            "--initial",
            help="Ignore the saved watermark and pull the full sliding window.",
        ),
    ] = False,
) -> None:
    """Pull Monzo transactions from every open account into ``transactions``."""
    try:
        from typing import cast

        from execution.adapters.monzo import SOURCE_ID, MonzoAdapter, MonzoAuth
        from execution.reconcile.ledger import RawTransactionLike, write_batch

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)

        watermark = None if initial else _load_watermark(conn, SOURCE_ID)
        auth = MonzoAuth.from_keychain()
        adapter = MonzoAdapter(auth=auth)
        try:
            batches = 0
            transactions = 0
            for batch in adapter.fetch_since(watermark):
                batches += 1
                transactions += len(batch)
                stats = write_batch(
                    conn, cast("list[RawTransactionLike]", batch)
                )
                del stats
            _save_watermark(
                conn,
                SOURCE_ID,
                watermark=adapter.next_watermark,
                emit_count=transactions,
            )
            _clear_reauth(conn, SOURCE_ID)
        finally:
            adapter.close()

        emit_success(
            {
                "source": SOURCE_ID,
                "batches": batches,
                "transactions": transactions,
                "next_watermark_saved": adapter.next_watermark is not None,
                "initial": initial,
            }
        )
    except PipelineError as err:
        if err.error_code == "needs_reauth":
            conn = db_mod.connect(db_path)
            db_mod.apply_migrations(conn)
            _record_reauth(conn, err.source, message=str(err))
        emit_error(err)
    except Exception as err:
        emit_error(err)


@ingest_bank_app.command("wise")
def ingest_bank_wise(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    initial: Annotated[
        bool,
        typer.Option(
            "--initial",
            help="Ignore the saved watermark and pull the full sliding window.",
        ),
    ] = False,
) -> None:
    """Pull Wise statements across every profile + balance into ``transactions``."""
    try:
        from typing import cast

        from execution.adapters.wise import SOURCE_ID, WiseAdapter, WiseAuth
        from execution.reconcile.ledger import RawTransactionLike, write_batch

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)

        watermark = None if initial else _load_watermark(conn, SOURCE_ID)
        auth = WiseAuth.from_keychain()
        adapter = WiseAdapter(auth=auth)
        try:
            batches = 0
            transactions = 0
            for batch in adapter.fetch_since(watermark):
                batches += 1
                transactions += len(batch)
                # Frozen+slots dataclasses don't match Protocols via mypy
                # structural subtyping across modules; runtime isinstance
                # confirms the shape (see ledger.RawTransactionLike).
                stats = write_batch(
                    conn, cast("list[RawTransactionLike]", batch)
                )
                del stats
            _save_watermark(
                conn,
                SOURCE_ID,
                watermark=adapter.next_watermark,
                emit_count=transactions,
            )
            _clear_reauth(conn, SOURCE_ID)
        finally:
            adapter.close()

        emit_success(
            {
                "source": SOURCE_ID,
                "batches": batches,
                "transactions": transactions,
                "next_watermark_saved": adapter.next_watermark is not None,
                "initial": initial,
            }
        )
    except PipelineError as err:
        if err.error_code == "needs_reauth":
            conn = db_mod.connect(db_path)
            db_mod.apply_migrations(conn)
            _record_reauth(conn, err.source, message=str(err))
        emit_error(err)
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# ops reauth subcommands (Phase 2)
# ---------------------------------------------------------------------------


@ops_app.command("reauth")
def ops_reauth(
    source: Annotated[str, typer.Argument(help="Adapter to re-authorise, e.g. 'ms365'.")],
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Run the interactive device-code re-auth for ``source``."""
    try:
        if source == "ms365":
            from execution.adapters.ms365 import SOURCE_ID as MS365_SOURCE
            from execution.adapters.ms365 import Ms365Auth

            auth = Ms365Auth.from_keychain()
            flow = auth.initiate_device_flow()
            message = flow.get("message") or (
                f"Visit {flow.get('verification_uri')} and enter code "
                f"{flow.get('user_code')}"
            )
            # Stderr so stdout stays single-JSON-doc per the agent contract.
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
            auth.complete_device_flow()

            conn = db_mod.connect(db_path)
            db_mod.apply_migrations(conn)
            _clear_reauth(conn, MS365_SOURCE)
            emit_success(
                {"source": MS365_SOURCE, "reauth": "ok", "user_code": flow.get("user_code")}
            )
            return
        if source == "monzo":
            import contextlib
            import webbrowser

            from execution.adapters.monzo import (
                SOURCE_ID as MONZO_SOURCE,
            )
            from execution.adapters.monzo import (
                MonzoAuth,
                new_state_token,
                run_callback_server,
            )

            monzo_auth = MonzoAuth.from_keychain()
            state = new_state_token()
            url = monzo_auth.build_authorize_url(state=state)
            sys.stderr.write(
                "Opening browser for Monzo authorisation. If the browser "
                "does not open automatically, visit:\n\n"
                f"  {url}\n\n"
                "After approving in the browser AND confirming in the "
                "Monzo mobile app, the callback will complete here.\n"
            )
            sys.stderr.flush()
            with contextlib.suppress(webbrowser.Error):
                webbrowser.open(url)
            callback = run_callback_server(expected_state=state)
            cache = monzo_auth.exchange_code(code=callback.code)

            conn = db_mod.connect(db_path)
            db_mod.apply_migrations(conn)
            _clear_reauth(conn, MONZO_SOURCE)
            emit_success(
                {
                    "source": MONZO_SOURCE,
                    "reauth": "ok",
                    "user_id": cache.user_id,
                    "access_expires_at": cache.access_expires_at.isoformat(),
                }
            )
            return
        raise ConfigError(
            f"unknown reauth source {source!r}; supported: 'ms365', 'monzo'",
            source="cli",
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# Small helpers reused by the ingest commands above
# ---------------------------------------------------------------------------


def _load_watermark(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT last_watermark FROM watermarks WHERE source = ?", (source,)
    ).fetchone()
    if row is None:
        return None
    return str(row["last_watermark"]) if row["last_watermark"] else None


def _save_watermark(
    conn: sqlite3.Connection,
    source: str,
    *,
    watermark: str | None,
    emit_count: int,
) -> None:
    from execution.shared.clock import now_utc

    with conn:
        conn.execute(
            """
            INSERT INTO watermarks
                (source, last_watermark, last_success_at, last_emit_count,
                 expected_cadence_hours)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_watermark = COALESCE(excluded.last_watermark, watermarks.last_watermark),
                last_success_at = excluded.last_success_at,
                last_emit_count = excluded.last_emit_count
            """,
            (source, watermark, now_utc().isoformat(), emit_count, 24),
        )


def _clear_reauth(conn: sqlite3.Connection, source: str) -> None:
    from execution.shared.clock import now_utc

    with conn:
        conn.execute(
            "UPDATE reauth_required SET resolved_at = ? WHERE source = ? AND resolved_at IS NULL",
            (now_utc().isoformat(), source),
        )


def _record_reauth(conn: sqlite3.Connection, source: str, *, message: str) -> None:
    from execution.shared.clock import now_utc

    with conn:
        conn.execute(
            """
            INSERT INTO reauth_required
                (source, detected_at, last_retry_at, retry_count, last_error)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_retry_at = excluded.last_retry_at,
                retry_count = reauth_required.retry_count + 1,
                last_error = excluded.last_error
            """,
            (source, now_utc().isoformat(), now_utc().isoformat(), message),
        )


def _upsert_email(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO emails
            (msg_id, source_adapter, message_id_header, received_at,
             from_addr, subject, outcome)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        ON CONFLICT(msg_id) DO NOTHING
        """,
        (
            row["msg_id"],
            row["source_adapter"],
            row.get("message_id_header"),
            row["received_at"],
            row["from_addr"],
            row["subject"],
        ),
    )


# ---------------------------------------------------------------------------
# Placeholders for later phases
# ---------------------------------------------------------------------------


@reconcile_app.callback()
def reconcile_callback() -> None:
    """Reconciliation engine — matcher, ledger post-processing, end-to-end run."""


@reconcile_app.command("match")
def reconcile_match(
    fiscal_year: Annotated[
        str | None,
        typer.Option("--fy", help="Restrict to a single FY (e.g. FY-2026-27)."),
    ] = None,
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Run the matcher over every invoice and upsert reconciliation_rows."""
    try:
        from execution.reconcile.run import run_matcher

        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)
        run_id = _begin_run(conn, kind="match", operation="reconcile")
        try:
            stats = run_matcher(conn, run_id=run_id, fiscal_year=fiscal_year)
            _complete_run(conn, run_id=run_id, status="ok", stats=_stats_dict(stats))
        except PipelineError:
            _complete_run(conn, run_id=run_id, status="failed", stats={})
            raise
        emit_success(
            {
                "run_id": run_id,
                "fiscal_year": fiscal_year,
                "invoices_scanned": stats.invoices_scanned,
                "auto_matched": stats.auto_matched,
                "suggested": stats.suggested,
                "unmatched": stats.unmatched,
                "rows_written": stats.rows_written,
                "rows_preserved": stats.rows_preserved,
            }
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


@reconcile_app.command("run")
def reconcile_run(
    fiscal_year: Annotated[
        str | None,
        typer.Option("--fy", help="Restrict matcher + sheet writes to a single FY."),
    ] = None,
    skip_ingest: Annotated[
        bool, typer.Option("--skip-ingest", help="Skip adapter pulls.")
    ] = False,
    skip_sheet: Annotated[
        bool, typer.Option("--skip-sheet", help="Skip the Google Sheets write step.")
    ] = False,
    adapters: Annotated[
        str,
        typer.Option(
            "--adapters",
            help=(
                "Comma-separated list of adapters to run. "
                "Supported: ms365,amex_csv,wise,monzo. Default: all."
            ),
        ),
    ] = "ms365,amex_csv,wise,monzo",
    amex_drop: Annotated[
        Path | None,
        typer.Option(
            "--amex-drop",
            help="Override the Amex CSV drop folder (default: ~/Downloads/Amex).",
        ),
    ] = None,
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """End-to-end: ingest → ledger post-processing → match → (optional) sheet.

    Adapters run in isolated try/except blocks so one failure doesn't
    block the others; the JSON summary lists each adapter's outcome
    plus a ``partial`` status when some adapters didn't complete.
    """
    try:
        conn = db_mod.connect(db_path)
        db_mod.apply_migrations(conn)
        run_id = _begin_run(conn, kind="pipeline", operation="reconcile")
        outcomes: dict[str, str] = {}
        warnings: list[str] = []
        requested = {a.strip() for a in adapters.split(",") if a.strip()}

        total_phases = 4 if not skip_sheet else 3
        phase = 0

        if not skip_ingest:
            emit_progress("reconcile", phase, total_phases, "Ingesting transactions")
            if "ms365" in requested:
                outcomes["ms365"] = _safe_run_adapter(conn, "ms365")
            if "amex_csv" in requested:
                outcomes["amex_csv"] = _safe_run_amex_csv(conn, drop=amex_drop)
            if "wise" in requested:
                outcomes["wise"] = _safe_run_adapter(conn, "wise")
            if "monzo" in requested:
                outcomes["monzo"] = _safe_run_adapter(conn, "monzo")
        else:
            warnings.append("ingest skipped via --skip-ingest")
        phase += 1

        # Ledger post-processing — refund linking.
        emit_progress("reconcile", phase, total_phases, "Linking refunds")
        try:
            from execution.reconcile.ledger import link_refunds

            refunds_linked = link_refunds(conn)
        except PipelineError as err:
            warnings.append(f"link_refunds: {err.user_message}")
            refunds_linked = 0
        phase += 1

        # Matcher.
        emit_progress("reconcile", phase, total_phases, "Matching invoices to transactions")
        from execution.reconcile.run import run_matcher

        match_stats = run_matcher(conn, run_id=run_id, fiscal_year=fiscal_year)
        phase += 1

        # Sheet writer (optional).
        sheet_outcome = "skipped"
        if not skip_sheet:
            emit_progress("reconcile", phase, total_phases, "Writing to Google Sheet")
            sheet_outcome = _safe_write_sheet(
                conn,
                fiscal_year=fiscal_year or london_today_fy(),
                run_id=run_id,
            )

        status = "ok"
        if any(v.startswith("error:") for v in outcomes.values()):
            status = "partial"
        _complete_run(
            conn,
            run_id=run_id,
            status=status,
            stats={
                "adapters": outcomes,
                "refunds_linked": refunds_linked,
                "match": _stats_dict(match_stats),
                "sheet": sheet_outcome,
            },
        )
        emit_success(
            {
                "run_id": run_id,
                "status": status,
                "fiscal_year": fiscal_year or london_today_fy(),
                "adapters": outcomes,
                "refunds_linked": refunds_linked,
                "match": {
                    "invoices_scanned": match_stats.invoices_scanned,
                    "auto_matched": match_stats.auto_matched,
                    "suggested": match_stats.suggested,
                    "unmatched": match_stats.unmatched,
                    "rows_written": match_stats.rows_written,
                    "rows_preserved": match_stats.rows_preserved,
                },
                "sheet": sheet_outcome,
                "warnings": warnings,
            }
        )
    except PipelineError as err:
        emit_error(err)
    except Exception as err:
        emit_error(err)


# ---------------------------------------------------------------------------
# Orchestrator helpers
# ---------------------------------------------------------------------------


def _stats_dict(stats: object) -> dict[str, object]:
    """asdict over a frozen+slotted dataclass; slots-dataclasses have no ``__dict__``."""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(stats) and not isinstance(stats, type):
        return asdict(stats)
    return {}


def _cleanup_stale_runs(conn: sqlite3.Connection, *, operation: str) -> int:
    """Mark runs stuck in 'running' for >1 hour as 'interrupted'.

    Returns the number of runs cleaned up.
    """
    from execution.shared.clock import now_utc

    one_hour_ago = (now_utc() - timedelta(hours=1)).isoformat()
    with conn:
        cursor = conn.execute(
            """
            UPDATE runs
               SET status = 'interrupted',
                   ended_at = datetime('now'),
                   completed_at = datetime('now')
             WHERE operation = ?
               AND status = 'running'
               AND started_at < ?
            """,
            (operation, one_hour_ago),
        )
        return cursor.rowcount


def _begin_run(conn: sqlite3.Connection, *, kind: str, operation: str) -> str:
    """Insert a ``runs`` record and return the generated ``run_id``.

    Also cleans up any stale 'running' records for this operation that are
    more than 1 hour old, marking them as 'interrupted'.
    """
    import secrets as _rnd

    from execution.shared.clock import now_utc

    # Cleanup stale runs first
    cleaned = _cleanup_stale_runs(conn, operation=operation)
    if cleaned > 0:
        emit_progress("cleanup", cleaned, cleaned, f"Cleaned up {cleaned} stale run(s)")

    run_id = f"{kind}-{now_utc().strftime('%Y%m%dT%H%M%S')}-{_rnd.token_hex(4)}"
    with conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, operation, started_at, status, stats_json, cost_gbp)
            VALUES (?, ?, ?, 'running', '{}', '0.00')
            """,
            (run_id, operation, now_utc().isoformat()),
        )
    return run_id


def _update_run_stats(conn: sqlite3.Connection, *, run_id: str, stats: dict[str, object]) -> None:
    """Update stats_json for a running job (for incremental progress tracking)."""
    import json as _json

    with conn:
        conn.execute(
            """
            UPDATE runs SET stats_json = ? WHERE run_id = ?
            """,
            (_json.dumps(stats, default=str), run_id),
        )


def _complete_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    stats: dict[str, object],
) -> None:
    import json as _json

    from execution.shared.clock import now_utc

    completed_at = now_utc().isoformat()
    with conn:
        conn.execute(
            """
            UPDATE runs
               SET ended_at = ?, completed_at = ?, status = ?, stats_json = ?
             WHERE run_id = ?
            """,
            (completed_at, completed_at, status, _json.dumps(stats, default=str), run_id),
        )


def _safe_run_adapter(conn: sqlite3.Connection, adapter_name: str) -> str:
    """Invoke an adapter's ingest in-process, capturing PipelineError.

    Returns the outcome string (``"ok"`` | ``"ok:N"`` | ``"error:<code>"``)
    for the Run Status tab + JSON output.
    """
    from typing import cast

    from execution.reconcile.ledger import RawTransactionLike, write_batch

    try:
        if adapter_name == "ms365":
            from execution.adapters.ms365 import (
                SOURCE_ID as MS_SOURCE,
            )
            from execution.adapters.ms365 import (
                Ms365Adapter,
                Ms365Auth,
            )

            auth = Ms365Auth.from_keychain()
            adapter = Ms365Adapter(auth=auth)
            try:
                watermark = _load_watermark(conn, MS_SOURCE)
                total = 0
                for batch in adapter.fetch_since(watermark):
                    total += len(batch)
                    with conn:
                        for email in batch:
                            _upsert_email(conn, email.as_email_row())
                _save_watermark(
                    conn, MS_SOURCE, watermark=adapter.next_watermark, emit_count=total
                )
                _clear_reauth(conn, MS_SOURCE)
            finally:
                adapter.close()
            return f"ok:{total}"

        if adapter_name == "wise":
            from execution.adapters.wise import (
                SOURCE_ID as WS_SOURCE,
            )
            from execution.adapters.wise import (
                WiseAdapter,
                WiseAuth,
            )

            w_auth = WiseAuth.from_keychain()
            w_adapter = WiseAdapter(auth=w_auth)
            try:
                watermark = _load_watermark(conn, WS_SOURCE)
                total = 0
                for w_batch in w_adapter.fetch_since(watermark):
                    total += len(w_batch)
                    write_batch(conn, cast("list[RawTransactionLike]", w_batch))
                _save_watermark(
                    conn, WS_SOURCE, watermark=w_adapter.next_watermark, emit_count=total
                )
                _clear_reauth(conn, WS_SOURCE)
            finally:
                w_adapter.close()
            return f"ok:{total}"

        if adapter_name == "monzo":
            from execution.adapters.monzo import (
                SOURCE_ID as MZ_SOURCE,
            )
            from execution.adapters.monzo import (
                MonzoAdapter,
                MonzoAuth,
            )

            m_auth = MonzoAuth.from_keychain()
            m_adapter = MonzoAdapter(auth=m_auth)
            try:
                watermark = _load_watermark(conn, MZ_SOURCE)
                total = 0
                for m_batch in m_adapter.fetch_since(watermark):
                    total += len(m_batch)
                    write_batch(conn, cast("list[RawTransactionLike]", m_batch))
                _save_watermark(
                    conn, MZ_SOURCE, watermark=m_adapter.next_watermark, emit_count=total
                )
                _clear_reauth(conn, MZ_SOURCE)
            finally:
                m_adapter.close()
            return f"ok:{total}"
        raise ConfigError(f"unknown adapter {adapter_name!r}", source="cli")
    except PipelineError as err:
        if err.error_code == "needs_reauth":
            _record_reauth(conn, err.source, message=str(err))
        return f"error:{err.error_code}"
    except Exception as err:
        # Adapters run in isolation so one failure doesn't block the others.
        return f"error:{type(err).__name__}"


def _safe_run_amex_csv(conn: sqlite3.Connection, *, drop: Path | None) -> str:
    """Run the Amex CSV adapter over the drop folder."""
    from typing import cast

    from execution.adapters.amex_csv import (
        SOURCE_ID as AMEX_SOURCE,
    )
    from execution.adapters.amex_csv import (
        discover_csv_files,
        fetch_from_file,
    )
    from execution.reconcile.ledger import RawTransactionLike, write_batch

    drop_root = drop or Path.home() / "Downloads" / "Amex"
    if not drop_root.exists():
        return "error:missing_drop_folder"
    try:
        files = discover_csv_files(drop_root)
        total = 0
        for csv_file in files:
            for batch in fetch_from_file(csv_file, drop_root=drop_root):
                total += len(batch)
                write_batch(conn, cast("list[RawTransactionLike]", batch))
        _save_watermark(conn, AMEX_SOURCE, watermark=None, emit_count=total)
        return f"ok:{total}"
    except PipelineError as err:
        return f"error:{err.error_code}"
    except Exception as err:
        return f"error:{type(err).__name__}"


def _safe_write_sheet(
    conn: sqlite3.Connection,
    *,
    fiscal_year: str,
    run_id: str,
) -> str:
    """Materialise Reconciliation + Unmatched + Exceptions tabs for a FY."""
    try:
        from execution.output.sheet import (
            ReconciliationRow,
            UnmatchedInvoice,
            UnmatchedTransaction,
            write_reconciliation_tab,
            write_unmatched_invoices_tab,
            write_unmatched_txns_tab,
        )
        from execution.shared import sheet as sheet_mod

        clients = sheet_mod.GoogleClients.connect()
        fy_sheet = sheet_mod.create_fy_workbook(clients, conn, fiscal_year)
        sink = _GspreadSheetSink(clients.gspread)

        recon_rows = _materialise_recon_rows(conn, fiscal_year=fiscal_year)
        write_reconciliation_tab(
            sink, spreadsheet_id=fy_sheet.spreadsheet_id, rows=recon_rows
        )

        unmatched_inv = _materialise_unmatched_invoices(conn, fiscal_year=fiscal_year)
        write_unmatched_invoices_tab(
            sink, spreadsheet_id=fy_sheet.spreadsheet_id, rows=unmatched_inv
        )

        unmatched_tx = _materialise_unmatched_transactions(conn, fiscal_year=fiscal_year)
        write_unmatched_txns_tab(
            sink, spreadsheet_id=fy_sheet.spreadsheet_id, rows=unmatched_tx
        )
        del run_id, ReconciliationRow, UnmatchedInvoice, UnmatchedTransaction
        return f"ok:{fy_sheet.spreadsheet_id}"
    except PipelineError as err:
        return f"error:{err.error_code}"
    except Exception as err:
        return f"error:{type(err).__name__}"


def _materialise_recon_rows(
    conn: sqlite3.Connection, *, fiscal_year: str
) -> list[ReconciliationRow]:
    from execution.output.sheet import ReconciliationRow

    rows = conn.execute(
        """
        SELECT r.row_id, r.fiscal_year, r.state, r.match_score, r.match_reason,
               r.user_note,
               i.invoice_id, i.invoice_number, i.invoice_date,
               i.vendor_name_raw, i.category, i.currency,
               i.amount_gross, i.amount_gross_gbp, i.drive_web_view_link,
               t.txn_id, t.booking_date, t.account, t.description_raw
        FROM reconciliation_rows r
        LEFT JOIN invoices i ON i.invoice_id = r.invoice_id
        LEFT JOIN transactions t ON t.txn_id = r.txn_id
        WHERE r.fiscal_year = ?
        ORDER BY r.updated_at DESC
        """,
        (fiscal_year,),
    ).fetchall()
    out: list[ReconciliationRow] = []
    for row in rows:
        out.append(
            ReconciliationRow(
                row_id=row["row_id"],
                fiscal_year=row["fiscal_year"],
                state=row["state"],
                score=Decimal(row["match_score"] or "0"),
                invoice_id=row["invoice_id"],
                invoice_number=row["invoice_number"],
                invoice_date=_parse_iso_or_none(row["invoice_date"]),
                supplier_name=row["vendor_name_raw"],
                category=row["category"],
                currency=row["currency"],
                amount_gross=_decimal_or_none(row["amount_gross"]),
                amount_gbp=_decimal_or_none(row["amount_gross_gbp"]),
                txn_id=row["txn_id"],
                booking_date=_parse_iso_or_none(row["booking_date"]),
                account=row["account"],
                description=row["description_raw"],
                match_reason=row["match_reason"] or "",
                verified=row["state"] == "user_verified",
                override_match=None,
                personal_flag=row["state"] == "user_personal",
                ignore_flag=row["state"] == "user_ignore",
                category_override=None,
                notes=row["user_note"] or "",
                drive_link=row["drive_web_view_link"],
            )
        )
    return out


def _materialise_unmatched_invoices(
    conn: sqlite3.Connection, *, fiscal_year: str
) -> list[UnmatchedInvoice]:
    from execution.output.sheet import UnmatchedInvoice
    from execution.shared.fiscal import fy_bounds

    start, end = fy_bounds(fiscal_year)
    rows = conn.execute(
        """
        SELECT i.invoice_id, i.vendor_name_raw, i.invoice_number,
               i.invoice_date, i.currency, i.amount_gross, i.category,
               i.drive_web_view_link
        FROM invoices i
        LEFT JOIN reconciliation_rows r ON r.invoice_id = i.invoice_id
        WHERE i.deleted_at IS NULL
          AND i.invoice_date BETWEEN ? AND ?
          AND (r.state IS NULL OR r.state = 'unmatched')
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        UnmatchedInvoice(
            invoice_id=row["invoice_id"],
            supplier_name=row["vendor_name_raw"],
            invoice_number=row["invoice_number"],
            invoice_date=_parse_iso_or_none(row["invoice_date"]),
            currency=row["currency"],
            amount_gross=_decimal_or_none(row["amount_gross"]),
            category=row["category"],
            drive_link=row["drive_web_view_link"],
        )
        for row in rows
    ]


def _materialise_unmatched_transactions(
    conn: sqlite3.Connection, *, fiscal_year: str
) -> list[UnmatchedTransaction]:
    from execution.output.sheet import UnmatchedTransaction
    from execution.shared.fiscal import fy_bounds

    start, end = fy_bounds(fiscal_year)
    rows = conn.execute(
        """
        SELECT t.txn_id, t.booking_date, t.account, t.description_raw,
               t.currency, t.amount, t.amount_gbp, t.txn_type, t.category
        FROM transactions t
        LEFT JOIN reconciliation_rows r ON r.txn_id = t.txn_id
        WHERE t.deleted_at IS NULL
          AND t.booking_date BETWEEN ? AND ?
          AND t.txn_type != 'transfer'
          AND r.row_id IS NULL
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    from datetime import date as _date

    return [
        UnmatchedTransaction(
            txn_id=row["txn_id"],
            booking_date=_date.fromisoformat(row["booking_date"]),
            account=row["account"],
            description=row["description_raw"],
            currency=row["currency"],
            amount=Decimal(row["amount"]),
            amount_gbp=Decimal(row["amount_gbp"]),
            txn_type=row["txn_type"],
            category=row["category"],
        )
        for row in rows
    ]


class _GspreadSheetSink:
    """Adapter from :class:`SheetSink` protocol onto a live gspread client."""

    def __init__(self, gspread_client: object) -> None:
        self._gspread = gspread_client

    def write_rectangle(
        self,
        *,
        spreadsheet_id: str,
        tab: str,
        header: tuple[str, ...],
        rows: list[list[str]],
    ) -> None:
        ss = self._gspread.open_by_key(spreadsheet_id)  # type: ignore[attr-defined]
        try:
            worksheet = ss.worksheet(tab)
        except Exception:
            worksheet = ss.add_worksheet(title=tab, rows=max(100, len(rows) + 1), cols=max(10, len(header)))
        values = [list(header), *rows]
        worksheet.clear()
        if values:
            worksheet.update(range_name="A1", values=values)


def _parse_iso_or_none(value: str | None) -> date | None:
    if not value:
        return None
    from datetime import date as _date
    from datetime import datetime as _datetime

    try:
        if "T" in value:
            return _datetime.fromisoformat(value).date()
        return _date.fromisoformat(value)
    except ValueError:
        return None


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)


@output_app.callback()
def output_callback() -> None:
    """Output namespace; Phase 4 mounts the full sheet/sales commands."""


# ---------------------------------------------------------------------------
# vendors subcommands
# ---------------------------------------------------------------------------


@vendors_app.command("list")
def vendors_list(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    search: Annotated[
        str | None,
        typer.Option("--search", "-s", help="Filter by name or domain (case-insensitive)."),
    ] = None,
) -> None:
    """List all known vendors from processed invoices."""
    import json as _json

    conn = db_mod.connect(db_path)
    db_mod.apply_migrations(conn)

    query = """
        SELECT
            v.vendor_id,
            v.canonical_name,
            v.domain,
            v.default_category,
            COUNT(i.invoice_id) as invoice_count,
            SUM(CASE WHEN i.currency = 'GBP' THEN i.amount_gross ELSE 0 END) as total_gbp,
            MAX(i.invoice_date) as last_invoice
        FROM vendors v
        LEFT JOIN invoices i ON v.vendor_id = i.vendor_id
    """
    params: list[str] = []
    if search:
        query += " WHERE v.canonical_name LIKE ? ESCAPE '\\' OR v.domain LIKE ? ESCAPE '\\'"
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = [f"%{escaped}%", f"%{escaped}%"]
    query += " GROUP BY v.vendor_id ORDER BY invoice_count DESC, v.canonical_name"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    vendors = []
    for row in rows:
        vendors.append({
            "vendor_id": row[0],
            "name": row[1],
            "domain": row[2],
            "category": row[3],
            "invoice_count": row[4],
            "total_gbp": format(Decimal(row[5] or 0), ".2f"),
            "last_invoice": row[6],
        })

    payload = {"status": "success", "count": len(vendors), "vendors": vendors}
    sys.stdout.write(_json.dumps(payload, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# runs subcommands (run management for agent-native parity)
# ---------------------------------------------------------------------------


runs_app = typer.Typer(name="runs", help="Manage pipeline runs.", no_args_is_help=True)
app.add_typer(runs_app)


@runs_app.command("list")
def runs_list(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    running: Annotated[
        bool,
        typer.Option("--running", "-r", help="Show only currently running jobs."),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of runs to show."),
    ] = 10,
) -> None:
    """List recent pipeline runs."""
    import json as _json

    conn = db_mod.connect(db_path)
    db_mod.apply_migrations(conn)

    if running:
        rows = conn.execute(
            """
            SELECT run_id, operation, started_at, completed_at, status, stats_json
            FROM runs
            WHERE status = 'running'
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT run_id, operation, started_at, completed_at, status, stats_json
            FROM runs
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    conn.close()

    runs = []
    for row in rows:
        stats = None
        if row[5]:
            try:
                stats = _json.loads(row[5])
            except _json.JSONDecodeError:
                stats = None
        runs.append({
            "run_id": row[0],
            "operation": row[1],
            "started_at": row[2],
            "completed_at": row[3],
            "status": row[4],
            "stats": stats,
        })

    emit_success({"count": len(runs), "runs": runs})


@runs_app.command("cancel")
def runs_cancel(
    operation: Annotated[
        str,
        typer.Argument(help="Operation to cancel (ingest_email, ingest_invoice, reconcile)."),
    ],
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Cancel running jobs for an operation."""
    valid_ops = {"ingest_email", "ingest_invoice", "reconcile"}
    if operation not in valid_ops:
        emit_error(ValueError(f"Invalid operation: {operation}. Must be one of: {', '.join(valid_ops)}"))
        return

    conn = db_mod.connect(db_path)
    db_mod.apply_migrations(conn)

    from execution.shared.clock import now_utc

    with conn:
        cursor = conn.execute(
            """
            UPDATE runs
            SET status = 'cancelled', completed_at = ?
            WHERE operation = ? AND status = 'running'
            """,
            (now_utc().isoformat(), operation),
        )
        cancelled = cursor.rowcount

    conn.close()
    emit_success({"cancelled": cancelled, "operation": operation})


@runs_app.command("status")
def runs_status(
    run_id: Annotated[str, typer.Argument(help="The run ID to check.")],
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Get detailed status of a specific run."""
    import json as _json

    conn = db_mod.connect(db_path)
    db_mod.apply_migrations(conn)

    row = conn.execute(
        """
        SELECT run_id, operation, started_at, completed_at, status, stats_json, cost_gbp
        FROM runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    conn.close()

    if not row:
        emit_error(ValueError(f"Run not found: {run_id}"))
        return

    stats = None
    if row[5]:
        try:
            stats = _json.loads(row[5])
        except _json.JSONDecodeError:
            stats = None

    emit_success({
        "run_id": row[0],
        "operation": row[1],
        "started_at": row[2],
        "completed_at": row[3],
        "status": row[4],
        "stats": stats,
        "cost_gbp": row[6],
    })


@runs_app.command("cleanup")
def runs_cleanup(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Clean up stale runs (running >1 hour) across all operations."""
    conn = db_mod.connect(db_path)
    db_mod.apply_migrations(conn)

    from execution.shared.clock import now_utc

    one_hour_ago = (now_utc() - timedelta(hours=1)).isoformat()

    with conn:
        cursor = conn.execute(
            """
            UPDATE runs
            SET status = 'interrupted', completed_at = datetime('now')
            WHERE status = 'running' AND started_at < ?
            """,
            (one_hour_ago,),
        )
        cleaned = cursor.rowcount

    conn.close()
    emit_success({"cleaned": cleaned})


if __name__ == "__main__":
    app()
