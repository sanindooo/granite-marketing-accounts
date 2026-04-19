"""FX rates with durable SQLite cache.

Primary source: frankfurter.app (free, no key, ECB-backed). Rates published
daily ~16:00 CET; weekend/holiday requests fall back to the last working day.

Every rate goes into the ``fx_rates`` table keyed on ``(date, from, to)``.
Stored as 6-dp Decimal text; ``to_rate`` is the canonical parser.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from execution.shared.clock import now_utc
from execution.shared.errors import DataQualityError
from execution.shared.money import to_rate, validate_currency

RateSource = Literal["ecb", "frankfurter", "mock"]

_FRANKFURTER_URL = "https://api.frankfurter.app/{iso_date}?from={from_ccy}&to={to_ccy}"

# Test seam: set to a dict mapping (date_iso, from, to) → Decimal to make the
# live path deterministic without touching the network.
_MOCK_RATES: dict[tuple[str, str, str], Decimal] = {}


def set_mock_rate(d: date, from_ccy: str, to_ccy: str, rate: Decimal | float | str) -> None:
    """Seed the mock table. Useful for unit + integration tests."""
    _MOCK_RATES[(d.isoformat(), validate_currency(from_ccy), validate_currency(to_ccy))] = (
        to_rate(rate)
    )


def clear_mock_rates() -> None:
    _MOCK_RATES.clear()


def get_rate(
    conn: sqlite3.Connection,
    d: date,
    from_ccy: str,
    to_ccy: str,
    *,
    allow_fetch: bool = True,
) -> Decimal:
    """Return the FX rate for ``d`` converting ``from_ccy`` → ``to_ccy``.

    Cache is checked first. Same-currency conversions short-circuit to 1.
    Weekend / holiday rates fall back to the previous available rate in the
    cache (or a fetched working-day rate) rather than failing — FX on a
    Saturday doesn't exist.
    """
    from_ccy = validate_currency(from_ccy)
    to_ccy = validate_currency(to_ccy)
    if from_ccy == to_ccy:
        return to_rate(1)

    cached = _lookup(conn, d, from_ccy, to_ccy)
    if cached is not None:
        return cached

    # Fall back to previous working day (up to 5 days back) — covers the
    # Sat/Sun case and most bank holidays.
    for i in range(1, 6):
        prev = d - timedelta(days=i)
        cached_prev = _lookup(conn, prev, from_ccy, to_ccy)
        if cached_prev is not None:
            _store(conn, d, from_ccy, to_ccy, cached_prev, source="frankfurter")
            return cached_prev

    if not allow_fetch:
        raise DataQualityError(
            f"no cached FX rate for {d.isoformat()} {from_ccy}->{to_ccy} "
            "and allow_fetch is False",
            source="fx",
        )

    rate = _fetch_rate(d, from_ccy, to_ccy)
    _store(conn, d, from_ccy, to_ccy, rate, source="frankfurter")
    return rate


def _lookup(conn: sqlite3.Connection, d: date, f: str, t: str) -> Decimal | None:
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE date = ? AND from_ccy = ? AND to_ccy = ?",
        (d.isoformat(), f, t),
    ).fetchone()
    if row is None:
        return None
    return to_rate(row["rate"])


def _store(
    conn: sqlite3.Connection,
    d: date,
    from_ccy: str,
    to_ccy: str,
    rate: Decimal,
    *,
    source: RateSource,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fx_rates
            (date, from_ccy, to_ccy, rate, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            d.isoformat(),
            from_ccy,
            to_ccy,
            format(rate, "f"),
            source,
            now_utc().isoformat(),
        ),
    )


def _fetch_rate(d: date, from_ccy: str, to_ccy: str) -> Decimal:
    """Live rate fetch. Uses mock table when populated; else HTTP."""
    import httpx

    key = (d.isoformat(), from_ccy, to_ccy)
    if key in _MOCK_RATES:
        return _MOCK_RATES[key]

    url = _FRANKFURTER_URL.format(
        iso_date=d.isoformat(), from_ccy=from_ccy, to_ccy=to_ccy
    )
    try:
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as err:
        raise DataQualityError(
            f"FX API returned {err.response.status_code} for {from_ccy}->{to_ccy} on {d.isoformat()}",
            source="fx",
            details={"url": url, "status": err.response.status_code},
            cause=err,
        ) from err
    except httpx.RequestError as err:
        raise DataQualityError(
            f"FX API request failed for {from_ccy}->{to_ccy} on {d.isoformat()}: {err}",
            source="fx",
            details={"url": url},
            cause=err,
        ) from err

    try:
        data = response.json()
        rate_value = data["rates"][to_ccy]
    except (KeyError, ValueError, TypeError) as err:
        raise DataQualityError(
            f"FX API response missing expected rate for {to_ccy}",
            source="fx",
            details={"url": url, "response": response.text[:500]},
            cause=err,
        ) from err

    return to_rate(rate_value)


def convert(
    conn: sqlite3.Connection,
    amount: Decimal,
    booking_date: date,
    from_ccy: str,
    to_ccy: str,
) -> Decimal:
    """Convert ``amount`` in ``from_ccy`` to ``to_ccy`` at ``booking_date``."""
    from execution.shared.money import to_money

    rate = get_rate(conn, booking_date, from_ccy, to_ccy)
    converted = amount * rate
    return to_money(converted, to_ccy)


def get_rate_to_gbp(
    conn: sqlite3.Connection,
    currency: str,
    invoice_date: str,
) -> tuple[Decimal | None, str | None]:
    """Get FX rate to GBP, returning (rate, None) on success or (None, error) on failure.

    This is the safe entry point for invoice processing - errors don't raise,
    they return an error message for the fx_error column.
    """
    if currency.upper() == "GBP":
        return to_rate(1), None

    try:
        d = date.fromisoformat(invoice_date)
    except (ValueError, TypeError):
        return None, f"invalid invoice date: {invoice_date}"

    try:
        validate_currency(currency)
    except ValueError:
        return None, f"unsupported currency: {currency}"

    try:
        rate = get_rate(conn, d, currency, "GBP")
        return rate, None
    except DataQualityError as err:
        return None, str(err)
    except Exception as err:
        return None, f"FX lookup failed: {err}"
