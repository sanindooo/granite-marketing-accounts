"""Single source of truth for time.

Everywhere else in ``execution/`` reaches for time through these helpers
so tests can freeze-time via ``time-machine`` and so nobody accidentally
writes a naive ``datetime.now()``.

Fiscal-year assignment and DST-sensitive logic use ``london_civil_date``
which converts to Europe/London BEFORE truncating to a date. A 2026-03-01
00:30 BST email received as 2026-02-28 23:30 UTC still belongs to March 1
in the UK.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def now_utc() -> datetime:
    """Current moment as a timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)


def today_london() -> date:
    """Today's civil date in Europe/London (what a UK accountant reads)."""
    return now_utc().astimezone(LONDON).date()


def to_london(dt: datetime) -> datetime:
    """Convert a timezone-aware datetime to Europe/London.

    Raises if the input is naive — every datetime in the system must carry
    an explicit tzinfo to avoid boundary bugs around DST and fiscal year.
    """
    if dt.tzinfo is None:
        raise ValueError("to_london requires a tz-aware datetime")
    return dt.astimezone(LONDON)


def london_civil_date(dt: datetime) -> date:
    """Civil (calendar) date in Europe/London for a tz-aware datetime.

    This is the correct way to assign an event to a fiscal year — UTC
    truncation would shift DST-boundary events by a day.
    """
    return to_london(dt).date()


def ensure_utc(dt: datetime) -> datetime:
    """Coerce a tz-aware datetime to UTC; reject naive inputs."""
    if dt.tzinfo is None:
        raise ValueError("ensure_utc requires a tz-aware datetime")
    return dt.astimezone(UTC)
