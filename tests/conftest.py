"""Shared pytest fixtures.

- ``tmp_db`` — a fresh :memory: sqlite with migrations applied.
- ``mock_secrets`` — flips ``secrets.set_mock_mode(True)`` for isolation.
- ``frozen_london`` — time-machine helper for FY-boundary assertions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from execution.shared import db as db_mod
from execution.shared import fx, secrets


@pytest.fixture
def tmp_db() -> Iterator[sqlite3.Connection]:
    conn = db_mod.connect(":memory:")
    db_mod.apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def mock_secrets() -> Iterator[None]:
    secrets.set_mock_mode(True)
    try:
        yield
    finally:
        secrets.set_mock_mode(False)


@pytest.fixture(autouse=True)
def _fx_cleanup() -> Iterator[None]:
    yield
    fx.clear_mock_rates()
