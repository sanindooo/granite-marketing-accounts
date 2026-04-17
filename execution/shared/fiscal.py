"""UK Ltd fiscal year helpers.

The user's company FY runs Mar 1 → Feb 28/29 (standard UK Ltd configuration).
FY label is ``FY-YYYY-YY`` where the first year is the start year. So a date
of 2026-05-15 lives in ``FY-2026-27`` (running 2026-03-01 → 2027-02-28).

All fiscal-year math is done on Europe/London civil dates. Never pass a
UTC-truncated date here — it shifts boundary events by up to five hours
which straddles DST into the wrong FY.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from execution.shared.clock import london_civil_date


@dataclass(frozen=True, slots=True)
class FiscalYear:
    """A fiscal year in the form ``FY-2026-27``.

    Use ``FiscalYear.of(d)`` rather than constructing directly; it handles
    leap years and normalises to Europe/London civil dates.
    """

    start: date  # Mar 1 of the start year
    end: date  # Feb 28 or Feb 29 of the following year

    @property
    def label(self) -> str:
        return f"FY-{self.start.year}-{str(self.end.year)[-2:]}"

    @property
    def slug(self) -> str:
        """Filesystem-safe form of ``label``."""
        return self.label  # already safe; keep alias for readers

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    @classmethod
    def of(cls, d: date | datetime) -> FiscalYear:
        """Return the FY containing ``d``.

        Accepts either a naive ``date`` (assumed to already be a London civil
        date by the caller) or a timezone-aware ``datetime`` which is
        converted to London before truncating.
        """
        civil = _to_civil_date(d)
        start_year = civil.year if civil.month >= 3 else civil.year - 1
        start = date(start_year, 3, 1)
        end = date(start_year + 1, 2, _feb_last(start_year + 1))
        return cls(start=start, end=end)

    @classmethod
    def from_label(cls, label: str) -> FiscalYear:
        """Parse ``FY-2026-27`` back into bounds."""
        if not label.startswith("FY-"):
            raise ValueError(f"bad FY label: {label!r}")
        parts = label[3:].split("-")
        if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
            raise ValueError(f"bad FY label: {label!r}")
        try:
            start_year = int(parts[0])
        except ValueError as exc:
            raise ValueError(f"bad FY label: {label!r}") from exc
        # The two-digit tail must match start_year + 1.
        expected_tail = f"{(start_year + 1) % 100:02d}"
        if parts[1] != expected_tail:
            raise ValueError(f"inconsistent FY label: {label!r}")
        return cls(
            start=date(start_year, 3, 1),
            end=date(start_year + 1, 2, _feb_last(start_year + 1)),
        )

    def days(self) -> int:
        return (self.end - self.start).days + 1


def fy_of(d: date | datetime) -> str:
    """Shortcut: FY label for a given date/datetime."""
    return FiscalYear.of(d).label


def fy_bounds(label: str) -> tuple[date, date]:
    """Shortcut: (start, end) dates for a given FY label."""
    fy = FiscalYear.from_label(label)
    return fy.start, fy.end


def london_today_fy() -> str:
    """FY containing today's London civil date."""
    from execution.shared.clock import today_london

    return fy_of(today_london())


def iter_fy_labels(start: str, end: str) -> list[str]:
    """Inclusive range of FY labels from ``start`` to ``end``."""
    s = FiscalYear.from_label(start)
    e = FiscalYear.from_label(end)
    if s.start > e.start:
        raise ValueError(f"start after end: {start} > {end}")
    labels: list[str] = []
    cursor = s
    while cursor.start <= e.start:
        labels.append(cursor.label)
        next_start = date(cursor.start.year + 1, 3, 1)
        cursor = FiscalYear.of(next_start)
    return labels


def _feb_last(year: int) -> int:
    """Last day of February in ``year`` (28 or 29 on leap years)."""
    return 29 if calendar.isleap(year) else 28


def _to_civil_date(d: date | datetime) -> date:
    if isinstance(d, datetime):
        if d.tzinfo is None:
            raise ValueError("fiscal.of requires tz-aware datetime or plain date")
        return london_civil_date(d)
    return d


__all__ = [
    "FiscalYear",
    "fy_bounds",
    "fy_of",
    "iter_fy_labels",
    "london_today_fy",
]

# Re-export so callers don't need to touch datetime directly for boundary math.
_ = timedelta
