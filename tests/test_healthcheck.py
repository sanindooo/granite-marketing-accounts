"""Tests for execution.ops.healthcheck."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from execution.adapters import monzo as monzo_mod
from execution.ops import healthcheck as hc_mod
from execution.ops.healthcheck import (
    ERROR_FREE_BYTES,
    ERROR_LAST_RUN_AGE,
    WARN_FREE_BYTES,
    WARN_LAST_RUN_AGE,
    HealthReport,
    run_healthcheck,
)
from execution.shared import db as db_mod


@pytest.fixture
def conn():
    c = db_mod.connect(":memory:")
    db_mod.apply_migrations(c)
    try:
        yield c
    finally:
        c.close()


def _seed_run(conn, *, started_at: str, status: str = "ok"):
    conn.execute(
        """
        INSERT INTO runs (run_id, started_at, ended_at, status)
        VALUES (?, ?, ?, ?)
        """,
        (f"r-{started_at}", started_at, started_at, status),
    )


def _fake_disk_usage(free_bytes: int):
    usage = mock.Mock()
    usage.free = free_bytes
    return mock.patch.object(
        hc_mod.shutil,
        "disk_usage",
        return_value=usage,
    )


class TestRunHealthcheck:
    def test_missing_secrets_is_a_warning_not_error(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        _seed_run(conn, started_at=now.isoformat())
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert isinstance(report, HealthReport)
        assert report.healthy is True
        assert any("missing" in w for w in report.warnings)

    def test_stale_last_run_raises_error(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        stale = now - (ERROR_LAST_RUN_AGE + timedelta(hours=1))
        _seed_run(conn, started_at=stale.isoformat())
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert report.healthy is False
        assert any("exceeds 72h" in e for e in report.errors)

    def test_warn_last_run_between_thresholds(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        in_warning = now - (WARN_LAST_RUN_AGE + timedelta(hours=1))
        _seed_run(conn, started_at=in_warning.isoformat())
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert report.healthy is True
        assert any("exceeds 36h" in w for w in report.warnings)

    def test_no_previous_run_is_warning(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert report.healthy is True
        assert any("no successful pipeline run" in w for w in report.warnings)

    def test_reauth_required_is_error(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        _seed_run(conn, started_at=now.isoformat())
        conn.execute(
            """
            INSERT INTO reauth_required (source, detected_at, retry_count)
            VALUES ('ms365', ?, 1)
            """,
            (now.isoformat(),),
        )
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert report.healthy is False
        assert any("ms365 needs reauth" in e for e in report.errors)

    def test_low_disk_space_errors(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        _seed_run(conn, started_at=now.isoformat())
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(ERROR_FREE_BYTES - 1):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert report.healthy is False
        assert any("free" in e for e in report.errors)

    def test_warn_disk_space(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        _seed_run(conn, started_at=now.isoformat())
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(WARN_FREE_BYTES - 1):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert report.healthy is True
        assert any("free" in w for w in report.warnings)

    def test_monzo_old_refresh_token_warns(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        _seed_run(conn, started_at=now.isoformat())
        stale_cache = monzo_mod.TokenCache(
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(hours=1),
            first_auth_at=now - timedelta(days=75),
            last_refresh_at=now - timedelta(hours=1),
        )
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: stale_cache)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert report.healthy is True
        assert any("Monzo refresh token" in w for w in report.warnings)

    def test_monzo_fresh_refresh_token_is_clean(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        _seed_run(conn, started_at=now.isoformat())
        fresh_cache = monzo_mod.TokenCache(
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(hours=1),
            first_auth_at=now - timedelta(days=5),
            last_refresh_at=now,
        )
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: fresh_cache)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert not any("Monzo refresh token" in w for w in report.warnings)

    def test_report_shape(
        self,
        conn,
        mock_secrets,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
        _seed_run(conn, started_at=now.isoformat())
        monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)
        with _fake_disk_usage(2 * WARN_FREE_BYTES):
            report = run_healthcheck(conn, state_dir=tmp_path, now=now)
        assert "keyring_backend" in report.checks
        assert "missing_secrets" in report.checks
        assert "db_integrity" in report.checks
        assert "disk_free_bytes" in report.checks
        assert report.checks["db_integrity"] == "ok"
