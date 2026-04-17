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
