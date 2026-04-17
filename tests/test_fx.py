"""FX cache and lookup behavior."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from execution.shared import fx
from execution.shared.errors import DataQualityError


class TestGetRate:
    def test_same_currency_is_one(self, tmp_db: sqlite3.Connection) -> None:
        r = fx.get_rate(tmp_db, date(2026, 4, 17), "GBP", "GBP")
        assert r == Decimal("1.000000")

    def test_cached_rate_returned(self, tmp_db: sqlite3.Connection) -> None:
        tmp_db.execute(
            """INSERT INTO fx_rates (date, from_ccy, to_ccy, rate, source, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("2026-04-10", "USD", "GBP", "0.794012", "ecb", "2026-04-10T16:00:00+00:00"),
        )
        r = fx.get_rate(tmp_db, date(2026, 4, 10), "USD", "GBP", allow_fetch=False)
        assert r == Decimal("0.794012")

    def test_weekend_falls_back_to_friday(self, tmp_db: sqlite3.Connection) -> None:
        # Friday 2026-04-10
        tmp_db.execute(
            """INSERT INTO fx_rates (date, from_ccy, to_ccy, rate, source, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("2026-04-10", "USD", "GBP", "0.794012", "ecb", "2026-04-10T16:00:00+00:00"),
        )
        # Sunday 2026-04-12 → should fall back to Friday
        r = fx.get_rate(tmp_db, date(2026, 4, 12), "USD", "GBP", allow_fetch=False)
        assert r == Decimal("0.794012")
        # And should be cached for Sunday now
        stored = tmp_db.execute(
            "SELECT rate FROM fx_rates WHERE date='2026-04-12' AND from_ccy='USD' AND to_ccy='GBP'"
        ).fetchone()
        assert stored is not None

    def test_missing_and_no_fetch_raises(self, tmp_db: sqlite3.Connection) -> None:
        with pytest.raises(DataQualityError):
            fx.get_rate(tmp_db, date(2026, 4, 17), "USD", "GBP", allow_fetch=False)

    def test_mock_rate_used_on_fetch(self, tmp_db: sqlite3.Connection) -> None:
        fx.set_mock_rate(date(2026, 4, 17), "USD", "GBP", "0.7950")
        r = fx.get_rate(tmp_db, date(2026, 4, 17), "USD", "GBP")
        assert r == Decimal("0.795000")
        # And subsequent call should be served from cache
        fx.clear_mock_rates()
        r2 = fx.get_rate(tmp_db, date(2026, 4, 17), "USD", "GBP", allow_fetch=False)
        assert r2 == Decimal("0.795000")

    def test_unknown_currency_rejected(self, tmp_db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            fx.get_rate(tmp_db, date(2026, 4, 17), "XYZ", "GBP")


class TestConvert:
    def test_basic_conversion(self, tmp_db: sqlite3.Connection) -> None:
        fx.set_mock_rate(date(2026, 4, 10), "USD", "GBP", "0.7940")
        gbp = fx.convert(tmp_db, Decimal("100.00"), date(2026, 4, 10), "USD", "GBP")
        # 100 * 0.7940 = 79.40
        assert gbp == Decimal("79.40")

    def test_same_currency_passthrough(self, tmp_db: sqlite3.Connection) -> None:
        gbp = fx.convert(tmp_db, Decimal("100.00"), date(2026, 4, 10), "GBP", "GBP")
        assert gbp == Decimal("100.00")
