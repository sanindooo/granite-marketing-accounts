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


def test_ingest_bank_subcommand_registered() -> None:
    """`granite ingest bank wise` should be exposed."""
    result = runner.invoke(app, ["ingest", "bank", "--help"])
    assert result.exit_code == 0
    assert "wise" in result.stdout


def test_ingest_bank_monzo_registered() -> None:
    result = runner.invoke(app, ["ingest", "bank", "monzo", "--help"])
    assert result.exit_code == 0


def test_ops_healthcheck_registered() -> None:
    result = runner.invoke(app, ["ops", "healthcheck", "--help"])
    assert result.exit_code == 0


def test_ops_healthcheck_emits_report(tmp_path, mock_secrets, monkeypatch) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])

    # Seed a fresh run so "no previous run" doesn't dominate the output.
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO runs (run_id, started_at, ended_at, status) "
        "VALUES ('r-seed', '2026-04-18T08:00:00+00:00', "
        "'2026-04-18T08:00:01+00:00', 'ok')"
    )
    conn.commit()
    conn.close()

    # Force monzo cache empty so the check doesn't touch Keychain.
    from execution.adapters import monzo as monzo_mod

    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)

    # Shutil.disk_usage on tmp_path gives real disk space; fake it for
    # deterministic output.
    import shutil as _shutil

    from execution.ops import healthcheck as hc_mod

    class _Usage:
        free = 10 * 1024 * 1024 * 1024  # 10 GB

    monkeypatch.setattr(
        hc_mod.shutil, "disk_usage", lambda _p: _Usage()
    )
    del _shutil  # placate unused-import when module load order shifts

    result = runner.invoke(
        app, ["ops", "healthcheck", "--db", str(db), "--state-dir", str(tmp_path)]
    )
    # Exit code may be 0 or 1 depending on whether Keychain entries
    # happen to exist; the contract we care about is the JSON payload.
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert "healthy" in doc
    assert "checks" in doc
    assert "warnings" in doc
    assert "errors" in doc


def test_reconcile_match_registered() -> None:
    result = runner.invoke(app, ["reconcile", "match", "--help"])
    assert result.exit_code == 0


def test_reconcile_run_registered() -> None:
    result = runner.invoke(app, ["reconcile", "run", "--help"])
    assert result.exit_code == 0


def test_reconcile_match_empty_db_returns_zero_stats(tmp_path) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])
    result = runner.invoke(app, ["reconcile", "match", "--db", str(db)])
    assert result.exit_code == 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "success"
    assert doc["invoices_scanned"] == 0
    assert doc["rows_written"] == 0
    assert doc["run_id"].startswith("match-")


def test_reconcile_run_isolates_adapter_failures(tmp_path) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])
    result = runner.invoke(
        app,
        [
            "reconcile",
            "run",
            "--db",
            str(db),
            "--skip-ingest",
            "--skip-sheet",
        ],
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "ok"
    assert doc["match"]["invoices_scanned"] == 0
    # Warnings record skip-ingest for audit.
    assert any("skipped" in w for w in doc["warnings"])
    # The runs table must record a completed record.
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, ended_at FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "ok"
    assert row["ended_at"] is not None


def test_ingest_bank_monzo_surfaces_reauth_required(tmp_path, monkeypatch) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])

    from execution.adapters import monzo as monzo_mod
    from execution.shared.errors import AuthExpiredError

    class _BrokenAuth:
        def access_token(self) -> str:
            raise AuthExpiredError("simulated monzo expiry", source="monzo")

    class _BrokenAdapter:
        def __init__(self, *, auth, http=None):
            del auth, http

        def fetch_since(self, _watermark, *, now=None):
            raise AuthExpiredError("simulated monzo expiry", source="monzo")

        def close(self) -> None:
            return None

        @property
        def next_watermark(self) -> str | None:
            return None

    monkeypatch.setattr(
        monzo_mod.MonzoAuth, "from_keychain", staticmethod(lambda: _BrokenAuth())
    )
    monkeypatch.setattr(monzo_mod, "MonzoAdapter", _BrokenAdapter)

    result = runner.invoke(app, ["ingest", "bank", "monzo", "--db", str(db)])
    assert result.exit_code != 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "error"
    assert doc["error_code"] == "needs_reauth"


def test_ops_reauth_monzo_walks_oauth_callback(tmp_path, monkeypatch) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])

    from datetime import UTC, datetime, timedelta

    from execution.adapters import monzo as monzo_mod
    from execution.adapters.monzo import CallbackResult, TokenCache

    captured_state: dict[str, str] = {}

    class _FakeAuth:
        def build_authorize_url(self, *, state: str) -> str:
            captured_state["state"] = state
            return f"https://auth.monzo.com/?state={state}"

        def exchange_code(self, *, code: str) -> TokenCache:
            assert code == "fake-code"
            now = datetime(2026, 4, 11, tzinfo=UTC)
            return TokenCache(
                access_token="a",
                refresh_token="r",
                access_expires_at=now + timedelta(hours=1),
                first_auth_at=now,
                last_refresh_at=now,
                user_id="user-fake",
            )

    monkeypatch.setattr(
        monzo_mod.MonzoAuth, "from_keychain", staticmethod(lambda: _FakeAuth())
    )
    monkeypatch.setattr(
        monzo_mod,
        "run_callback_server",
        lambda *, expected_state, **_kw: CallbackResult(
            code="fake-code", state=expected_state
        ),
    )
    # No-op webbrowser.open so the test is CI-safe.
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda _url: False)

    result = runner.invoke(app, ["ops", "reauth", "monzo", "--db", str(db)])
    assert result.exit_code == 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "success"
    assert doc["source"] == "monzo"
    assert doc["user_id"] == "user-fake"
    assert captured_state["state"]  # used a non-empty CSRF state


def test_ingest_bank_wise_surfaces_reauth_required(tmp_path, monkeypatch) -> None:
    db = tmp_path / "pipeline.db"
    runner.invoke(app, ["db", "migrate", "--db", str(db)])

    from execution.adapters import wise as wise_mod
    from execution.shared.errors import AuthExpiredError

    class _BrokenAuth:
        def authorization_header(self) -> str:
            return "Bearer fake"

        def sign_challenge(self, _c: str) -> str:
            raise AuthExpiredError("simulated", source="wise")

    class _BrokenAdapter:
        def __init__(self, *, auth, http=None):
            del auth, http

        def fetch_since(self, _watermark, *, now=None):
            raise AuthExpiredError("simulated wise expiry", source="wise")

        def close(self) -> None:
            return None

        @property
        def next_watermark(self) -> str | None:
            return None

    monkeypatch.setattr(
        wise_mod.WiseAuth, "from_keychain", staticmethod(lambda: _BrokenAuth())
    )
    monkeypatch.setattr(wise_mod, "WiseAdapter", _BrokenAdapter)

    result = runner.invoke(app, ["ingest", "bank", "wise", "--db", str(db)])
    assert result.exit_code != 0
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["status"] == "error"
    assert doc["error_code"] == "needs_reauth"

    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM reauth_required WHERE source = 'wise'"
    ).fetchone()
    assert row is not None and row["resolved_at"] is None


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
