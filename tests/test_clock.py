"""Timezone helper tests — critical for FY boundary correctness."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import time_machine

from execution.shared.clock import (
    LONDON,
    ensure_utc,
    london_civil_date,
    now_utc,
    to_london,
    today_london,
)


def test_now_utc_is_tz_aware() -> None:
    now = now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0


def test_london_zoneinfo_constant() -> None:
    assert ZoneInfo("Europe/London") == LONDON


def test_to_london_rejects_naive() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        to_london(datetime(2026, 4, 17, 10, 0))  # naive


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        ensure_utc(datetime(2026, 4, 17, 10, 0))  # naive


class TestLondonCivilDate:
    """The single most bug-prone function in the pipeline. Test hard."""

    def test_midnight_utc_in_summer_is_previous_day_in_london(self) -> None:
        # 2026-06-15 01:00 BST == 2026-06-15 00:00 UTC — no date shift
        dt = datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
        assert london_civil_date(dt).isoformat() == "2026-06-15"

    def test_late_night_utc_in_summer_becomes_next_day_in_london(self) -> None:
        # 2026-06-14 23:30 UTC == 2026-06-15 00:30 BST
        dt = datetime(2026, 6, 14, 23, 30, tzinfo=UTC)
        assert london_civil_date(dt).isoformat() == "2026-06-15"

    def test_winter_boundary_unchanged(self) -> None:
        # 2026-01-15 00:30 UTC == 2026-01-15 00:30 GMT (no offset)
        dt = datetime(2026, 1, 15, 0, 30, tzinfo=UTC)
        assert london_civil_date(dt).isoformat() == "2026-01-15"

    def test_fiscal_boundary_feb_28_late_evening_utc_is_march_1_london(self) -> None:
        # The reviewer-flagged FY-straddle case: an email arriving at 23:30
        # UTC on Feb 28 is 00:30 local BST on Mar 1 — belongs to the next FY.
        # BST transition is last Sun of March — so Feb 28 is still GMT.
        # Confirm: GMT == UTC, so 2027-02-28 23:30 UTC is 2027-02-28 23:30 London.
        dt = datetime(2027, 2, 28, 23, 30, tzinfo=UTC)
        assert london_civil_date(dt).isoformat() == "2027-02-28"

    def test_dst_transition_day_end_october(self) -> None:
        # DST ends 2026-10-25 at 02:00 BST → back to 01:00 GMT
        # 2026-10-25 00:30 UTC == 2026-10-25 01:30 BST
        dt = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        assert london_civil_date(dt).isoformat() == "2026-10-25"


@time_machine.travel("2026-04-17T10:00:00+00:00", tick=False)
def test_today_london_with_frozen_clock() -> None:
    # 10:00 UTC in April → 11:00 BST
    assert today_london().isoformat() == "2026-04-17"
