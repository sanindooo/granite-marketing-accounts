"""SQLite schema, connection factory, and migration runner.

Every connection opens with the same PRAGMAs:
    journal_mode=WAL, synchronous=NORMAL, cache_size=-64MB, mmap_size=256MB,
    temp_store=MEMORY, foreign_keys=ON, busy_timeout=30s.

Migrations are numbered SQL files under ``execution/shared/migrations/``.
``apply_migrations`` is idempotent, transactional per migration, and records
a SHA-256 checksum so a tampered migration can't silently take effect.

The full ERD from the plan (see
``docs/plans/2026-04-17-001-feat-accounting-assistant-pipeline-plan.md``)
is materialised in ``001_init.sql``. Later migrations add columns / tables
as phases land.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from execution.shared.errors import ConfigError

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# PRAGMAs applied on every connection. cache_size is negative → kibibytes.
_PRAGMAS: tuple[tuple[str, str | int], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("cache_size", -64_000),
    ("mmap_size", 268_435_456),  # 256 MB
    ("temp_store", "MEMORY"),
    ("foreign_keys", "ON"),
    ("busy_timeout", 30_000),
)


def default_db_path() -> Path:
    """Project-local durable DB path. Respects ``GRANITE_DB`` override."""
    override = os.environ.get("GRANITE_DB")
    if override:
        return Path(override)
    project_root = Path(__file__).resolve().parents[2]
    return project_root / ".state" / "pipeline.db"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with all PRAGMAs applied.

    Accepts ``:memory:`` for tests. Creates the parent directory on disk
    paths so first-run bootstrap doesn't need a separate step. Files get
    0o600 and parent directories 0o700 for the data-protection guard.
    """
    if db_path is None:
        db_path = default_db_path()
    if isinstance(db_path, Path):
        target = str(db_path)
        _ensure_parent_dir(db_path)
    else:
        target = db_path
        if target != ":memory:":
            _ensure_parent_dir(Path(target))
    conn = sqlite3.connect(
        target,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # explicit BEGIN/COMMIT; autocommit off
    )
    conn.row_factory = sqlite3.Row
    for pragma, value in _PRAGMAS:
        conn.execute(f"PRAGMA {pragma}={value};")
    # Tighten file permissions post-open if it's an on-disk file.
    # Chmod is best-effort; WAL/SHM files inherit umask. The plan mandates
    # 0o600; surface via lint, not runtime failure.
    if isinstance(db_path, Path) and db_path.exists():
        with suppress(OSError):
            db_path.chmod(0o600)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a block in ``BEGIN IMMEDIATE; ... COMMIT;`` — rollback on error."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK;")
        raise
    else:
        conn.execute("COMMIT;")


def apply_migrations(
    conn: sqlite3.Connection, *, migrations_dir: Path | None = None
) -> list[str]:
    """Apply any pending migrations in order.

    Returns the list of migration filenames that ran on this call. A
    migration whose checksum changed raises ``ConfigError`` — never silently
    re-execute a tampered file.

    Concurrent CLI invocations (e.g. two dashboard buttons clicked back to
    back) used to race here: both processes would read ``_applied_map``
    before either had committed, both would try to ALTER the same table, and
    the loser would crash on a duplicate-column error. We now run the entire
    pending-migration loop under a single ``BEGIN IMMEDIATE`` (which takes
    SQLite's reserved lock + waits up to ``busy_timeout`` for any peer
    holding it). The second caller blocks until the first commits, then re-
    reads ``_applied_map`` and finds nothing pending — no duplicate attempt.
    Each script is split into statements so a mid-script failure rolls back
    the whole outer transaction; ``executescript`` would issue its own
    COMMIT semantics that conflict with this manual transaction handling.
    """
    mig_dir = migrations_dir or _MIGRATIONS_DIR
    _ensure_migrations_table(conn)
    conn.execute("BEGIN IMMEDIATE;")
    try:
        applied = _applied_map(conn)
        available = sorted(mig_dir.glob("*.sql"))
        ran: list[str] = []
        for path in available:
            version = path.stem  # e.g. "001_init"
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise ConfigError(
                        f"migration {version} has changed on disk since it "
                        f"was applied (checksum mismatch). Refusing to "
                        f"re-apply.",
                        source="db",
                    )
                continue
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, checksum) "
                "VALUES (?, datetime('now'), ?);",
                (version, checksum),
            )
            ran.append(path.name)
        conn.execute("COMMIT;")
    except BaseException:
        with suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK;")
        raise
    return ran


def current_version(conn: sqlite3.Connection) -> str | None:
    _ensure_migrations_table(conn)
    row = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return row["version"] if row else None


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            checksum   TEXT NOT NULL
        );
        """
    )


def _applied_map(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT version, checksum FROM schema_migrations"
    ).fetchall()
    return {row["version"]: row["checksum"] for row in rows}


def _split_sql(sql: str) -> list[str]:
    """Split a SQL script into statements on semicolons.

    Strips SQL-line comments (``-- …``) and blank lines first. Assumes no
    literal ``;`` inside string literals — acceptable for our migrations,
    which are plain DDL.
    """
    cleaned_lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        cleaned_lines.append(line)
    joined = "\n".join(cleaned_lines)
    return [p.strip() for p in joined.split(";") if p.strip()]


def _ensure_parent_dir(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        parent.chmod(0o700)


__all__ = [
    "apply_migrations",
    "connect",
    "current_version",
    "default_db_path",
    "transaction",
]
