"""Smoke tests for the Typer CLI shell."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from execution.cli import app

runner = CliRunner()


def test_granite_help_shows_all_sections() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    stdout = result.stdout
    for cmd in ("db", "ops", "ingest", "reconcile", "output"):
        assert cmd in stdout


def test_db_migrate_in_memory() -> None:
    # :memory: DB — migrations run every time because there's nothing persisted
    result = runner.invoke(app, ["db", "migrate", "--db", ":memory:"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "success"
    assert "001_init.sql" in doc["migrations_applied"]


def test_db_status_after_migrate(tmp_path) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])
    result = runner.invoke(app, ["db", "status", "--db", str(db)])
    assert result.exit_code == 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "success"
    assert doc["schema_version"] == "001_init"
    assert doc["pragmas"]["foreign_keys"] == 1


def test_ops_health_reports_fiscal_year(tmp_path, mock_secrets) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])
    result = runner.invoke(app, ["ops", "health", "--db", str(db)])
    assert result.exit_code == 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "success"
    assert doc["checks"]["fiscal_year"].startswith("FY-")
    assert doc["checks"]["foreign_keys"] is True


def test_ingest_email_subcommand_registered() -> None:
    """`granite ingest email ms365` should be exposed."""
    result = runner.invoke(app, ["ingest", "email", "--help"])
    assert result.exit_code == 0
    assert "ms365" in result.stdout


def test_ops_reauth_rejects_unknown_source() -> None:
    result = runner.invoke(app, ["ops", "reauth", "gmail"])
    # Unknown source path emits an error JSON and non-zero exit.
    assert result.exit_code != 0
    last_line = result.stdout.strip().splitlines()[-1]
    doc = json.loads(last_line)
    assert doc["status"] == "error"
    assert "ms365" in doc["message"]


def test_ingest_ms365_surfaces_reauth_required(tmp_path, monkeypatch) -> None:
    """When the adapter raises AuthExpiredError, the CLI records a row + exits non-zero."""
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])

    # Stub Ms365Auth.from_keychain to return a fake that fails to refresh.
    from execution.adapters import ms365 as ms365_mod
    from execution.shared.errors import AuthExpiredError

    class _BrokenAuth:
        def access_token(self):
            raise AuthExpiredError(
                "simulated expiry", source="ms365"
            )

    def fake_from_keychain():
        return _BrokenAuth()

    monkeypatch.setattr(ms365_mod.Ms365Auth, "from_keychain", staticmethod(fake_from_keychain))

    # Also stub out the httpx.Client the adapter would build, just in case.
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: None)

    result = runner.invoke(app, ["ingest", "email", "ms365", "--db", str(db)])
    assert result.exit_code != 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "error"
    assert doc["error_code"] == "needs_reauth"

    # reauth_required row must be there
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM reauth_required WHERE source = 'ms365'"
    ).fetchone()
    assert row is not None
    assert row["resolved_at"] is None
    assert row["retry_count"] >= 1
