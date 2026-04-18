"""Pre-run healthcheck — gate the scheduled pipeline.

Runs before the launchd-triggered ``granite reconcile run`` and
emits a structured ``{status, checks, warnings, errors, healthy}``
JSON payload. A non-empty ``errors`` list aborts the scheduled run
and a loud notification is sent; warnings are surfaced in the Run
Status tab but don't block.

What we check (plan § Phase 5):

- Every adapter's Keychain secret is present + non-empty.
- Keyring backend is the pinned macOS Keychain (not a weaker fallback).
- SQLite DB integrity via ``PRAGMA integrity_check``.
- Last successful run age per adapter — warn at 36h, error at 72h.
- OAuth token expiry windows — flag Monzo refresh tokens older than
  60 days (90-day cliff with 30-day warning).
- Free disk space on the state/invoices directories — warn < 1 GB,
  error < 250 MB.
- Any ``reauth_required`` row still unresolved.

The module is deliberately pure-ish: it reads state, it does not
write. The caller (CLI / launchd wrapper) records warnings to the
``runs`` table and decides whether to escalate.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from execution.shared import secrets
from execution.shared.errors import ConfigError

# Age thresholds (plan + pragmatic slack for weekends / week-long leave)
WARN_LAST_RUN_AGE: Final[timedelta] = timedelta(hours=36)
ERROR_LAST_RUN_AGE: Final[timedelta] = timedelta(hours=72)

# Disk-space thresholds on the state/invoices parent directory
WARN_FREE_BYTES: Final[int] = 1 * 1024 * 1024 * 1024  # 1 GB
ERROR_FREE_BYTES: Final[int] = 256 * 1024 * 1024  # 256 MB

# Monzo 90-day cliff with a 30-day runway
MONZO_WARN_AGE: Final[timedelta] = timedelta(days=60)

EXPECTED_SECRETS: Final[dict[str, tuple[str, ...]]] = {
    "ms365": ("client_id",),
    "wise": ("api_token", "private_key_pem"),
    "monzo": ("client_id", "client_secret"),
    "claude": ("api_key",),
}


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Structured payload surfaced by the CLI / launchd wrapper."""

    healthy: bool
    checks: dict[str, object]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass
class _Mutable:
    """Accumulator used to assemble a :class:`HealthReport`."""

    checks: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_healthcheck(
    conn: sqlite3.Connection,
    *,
    state_dir: Path,
    now: datetime | None = None,
) -> HealthReport:
    """Run every check and collect the report.

    ``state_dir`` is the directory that holds ``pipeline.db``; we use
    it to probe free disk space and to verify the DB file is readable.
    """
    now = now or datetime.now(tz=UTC)
    acc = _Mutable()

    _check_keyring_backend(acc)
    _check_expected_secrets(acc)
    _check_db_integrity(conn, acc)
    _check_last_run_age(conn, now=now, acc=acc)
    _check_reauth_required(conn, acc)
    _check_monzo_refresh_age(now=now, acc=acc)
    _check_disk_space(state_dir=state_dir, acc=acc)

    return HealthReport(
        healthy=not acc.errors,
        checks=acc.checks,
        warnings=tuple(acc.warnings),
        errors=tuple(acc.errors),
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_keyring_backend(acc: _Mutable) -> None:
    try:
        if secrets.is_mock():
            acc.checks["keyring_backend"] = "mock"
            acc.warnings.append("keyring under MOCK_MODE — only valid in tests")
            return
        backend = secrets.ensure_backend()
        acc.checks["keyring_backend"] = type(backend).__name__
    except ConfigError as err:
        acc.checks["keyring_backend"] = None
        acc.errors.append(f"keyring: {err.user_message}")


def _check_expected_secrets(acc: _Mutable) -> None:
    missing: list[str] = []
    for ns, keys in EXPECTED_SECRETS.items():
        for key in keys:
            if secrets.get(ns, key) is None:
                missing.append(f"{ns}/{key}")
    acc.checks["missing_secrets"] = missing
    if missing:
        acc.warnings.append(
            f"{len(missing)} Keychain entries missing; some adapters will fail."
        )


def _check_db_integrity(conn: sqlite3.Connection, acc: _Mutable) -> None:
    try:
        row = conn.execute("PRAGMA integrity_check;").fetchone()
    except sqlite3.DatabaseError as err:
        acc.errors.append(f"sqlite integrity_check raised: {err}")
        acc.checks["db_integrity"] = None
        return
    status = row[0] if row else "unknown"
    acc.checks["db_integrity"] = status
    if status != "ok":
        acc.errors.append(f"sqlite integrity_check reported {status!r}")


def _check_last_run_age(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    acc: _Mutable,
) -> None:
    row = conn.execute(
        """
        SELECT started_at FROM runs
         WHERE status IN ('ok', 'partial')
         ORDER BY started_at DESC
         LIMIT 1
        """
    ).fetchone()
    if row is None:
        acc.checks["last_successful_run"] = None
        acc.warnings.append("no successful pipeline run on record yet")
        return
    last = _parse_iso(row["started_at"])
    age = now - last
    acc.checks["last_successful_run"] = row["started_at"]
    acc.checks["last_successful_run_hours"] = int(age.total_seconds() // 3600)
    if age > ERROR_LAST_RUN_AGE:
        acc.errors.append(f"last successful run {age} ago — exceeds 72h")
    elif age > WARN_LAST_RUN_AGE:
        acc.warnings.append(f"last successful run {age} ago — exceeds 36h")


def _check_reauth_required(conn: sqlite3.Connection, acc: _Mutable) -> None:
    rows = conn.execute(
        "SELECT source FROM reauth_required WHERE resolved_at IS NULL"
    ).fetchall()
    pending = [r["source"] for r in rows]
    acc.checks["pending_reauth"] = pending
    for source in pending:
        acc.errors.append(f"{source} needs reauth — run `granite ops reauth {source}`")


def _check_monzo_refresh_age(*, now: datetime, acc: _Mutable) -> None:
    from execution.adapters import monzo as monzo_mod

    try:
        cache = monzo_mod.load_token_cache()
    except ConfigError as err:
        acc.warnings.append(f"monzo token cache unreadable: {err.user_message}")
        return
    if cache is None:
        acc.checks["monzo_first_auth_at"] = None
        return
    age = cache.refresh_token_age(now=now)
    acc.checks["monzo_first_auth_at"] = cache.first_auth_at.isoformat()
    acc.checks["monzo_refresh_age_days"] = age.days
    if age > MONZO_WARN_AGE:
        acc.warnings.append(
            f"Monzo refresh token is {age.days}d old; re-auth before day 90."
        )


def _check_disk_space(*, state_dir: Path, acc: _Mutable) -> None:
    target = state_dir if state_dir.exists() else state_dir.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError as err:
        acc.errors.append(f"disk_usage probe failed at {target}: {err}")
        return
    acc.checks["disk_free_bytes"] = usage.free
    if usage.free < ERROR_FREE_BYTES:
        acc.errors.append(
            f"only {_human_bytes(usage.free)} free on {target}"
        )
    elif usage.free < WARN_FREE_BYTES:
        acc.warnings.append(
            f"only {_human_bytes(usage.free)} free on {target}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    cleaned = value
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return datetime.fromisoformat(cleaned)


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n} PB"


__all__ = [
    "ERROR_FREE_BYTES",
    "ERROR_LAST_RUN_AGE",
    "EXPECTED_SECRETS",
    "MONZO_WARN_AGE",
    "WARN_FREE_BYTES",
    "WARN_LAST_RUN_AGE",
    "HealthReport",
    "run_healthcheck",
]
