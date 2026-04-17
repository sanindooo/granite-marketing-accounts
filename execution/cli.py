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
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from execution.shared import db as db_mod
from execution.shared.errors import PipelineError, emit_error, emit_success
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

app.add_typer(db_app)
app.add_typer(ops_app)
app.add_typer(ingest_app)
app.add_typer(reconcile_app)
app.add_typer(output_app)


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


# ---------------------------------------------------------------------------
# ops subcommands (minimal Phase 1A surface)
# ---------------------------------------------------------------------------

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
# Placeholders for later phases
# ---------------------------------------------------------------------------

@ingest_app.callback()
def ingest_callback() -> None:
    """Stub. Email + bank adapters land in Phases 2 and 3."""


@reconcile_app.callback()
def reconcile_callback() -> None:
    """Stub. Matching engine lands in Phase 4."""


@output_app.callback()
def output_callback() -> None:
    """Output namespace; Phase 4 mounts the full sheet/sales commands."""


if __name__ == "__main__":
    app()
