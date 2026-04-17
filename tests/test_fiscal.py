"""Fiscal-year assignment — boundaries are the bug-magnet."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from execution.shared.fiscal import (
    FiscalYear,
    fy_bounds,
    fy_of,
    iter_fy_labels,
)


class TestFiscalYearOf:
    def test_march_1_starts_new_fy(self) -> None:
        assert fy_of(date(2026, 3, 1)) == "FY-2026-27"

    def test_february_28_still_in_previous_fy(self) -> None:
        assert fy_of(date(2027, 2, 28)) == "FY-2026-27"

    def test_february_29_leap_year(self) -> None:
        # 2027→2028: 2028 IS a leap year → FY-2027-28 ends Feb 29 2028
        assert fy_of(date(2028, 2, 29)) == "FY-2027-28"

    def test_march_1_after_leap_year(self) -> None:
        assert fy_of(date(2028, 3, 1)) == "FY-2028-29"

    def test_mid_year(self) -> None:
        assert fy_of(date(2026, 7, 15)) == "FY-2026-27"

    def test_january_belongs_to_previous_year_fy(self) -> None:
        assert fy_of(date(2027, 1, 10)) == "FY-2026-27"

    def test_tz_aware_datetime_accepted(self) -> None:
        dt = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        assert fy_of(dt) == "FY-2026-27"

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            fy_of(datetime(2026, 3, 1, 10, 0))


class TestFiscalYearBounds:
    def test_standard_year(self) -> None:
        start, end = fy_bounds("FY-2026-27")
        assert start == date(2026, 3, 1)
        assert end == date(2027, 2, 28)

    def test_leap_year_end(self) -> None:
        start, end = fy_bounds("FY-2027-28")
        assert start == date(2027, 3, 1)
        assert end == date(2028, 2, 29)  # leap

    def test_bad_label_shape(self) -> None:
        with pytest.raises(ValueError):
            fy_bounds("FY-2026")

    def test_inconsistent_tail(self) -> None:
        # "FY-2026-28" is not coherent — next-year tail must be 27
        with pytest.raises(ValueError, match="inconsistent"):
            fy_bounds("FY-2026-28")


class TestFiscalYearContains:
    def test_contains_start(self) -> None:
        fy = FiscalYear.from_label("FY-2026-27")
        assert fy.contains(fy.start)

    def test_contains_end(self) -> None:
        fy = FiscalYear.from_label("FY-2026-27")
        assert fy.contains(fy.end)

    def test_excludes_next_day(self) -> None:
        fy = FiscalYear.from_label("FY-2026-27")
        assert not fy.contains(date(2027, 3, 1))


class TestIterFyLabels:
    def test_single_fy(self) -> None:
        assert iter_fy_labels("FY-2026-27", "FY-2026-27") == ["FY-2026-27"]

    def test_three_year_range(self) -> None:
        assert iter_fy_labels("FY-2024-25", "FY-2026-27") == [
            "FY-2024-25",
            "FY-2025-26",
            "FY-2026-27",
        ]

    def test_reverse_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            iter_fy_labels("FY-2027-28", "FY-2024-25")


class TestDaysInFy:
    def test_standard_year(self) -> None:
        assert FiscalYear.from_label("FY-2026-27").days() == 365

    def test_leap_fy(self) -> None:
        # FY-2027-28 ends Feb 29 2028 (leap) → 366 days
        assert FiscalYear.from_label("FY-2027-28").days() == 366
