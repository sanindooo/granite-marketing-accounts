"""Decimal chokepoint for every monetary value in the pipeline.

Floats never cross a function boundary inside ``execution/`` outside this
module. ECB rates arrive as floats; Claude JSON arrives with floats; CSVs
arrive as strings. All of them go through ``to_money`` (2dp) or
``to_rate`` (6dp) at ingest.

The ISO 4217 whitelist is narrow on purpose: accepting any 3-letter string
would turn a malformed extraction into a filesystem path-traversal or
sheet-row mis-assignment. Expand when a real invoice demands it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Currencies we actually see in UK Ltd accounting. Expand on demand;
# keep the whitelist narrow so Claude hallucinations like "£GBP" fail fast.
ALLOWED_CURRENCIES: frozenset[str] = frozenset(
    {
        "GBP",
        "USD",
        "EUR",
        "CAD",
        "AUD",
        "CHF",
        "JPY",
        "NZD",
        "SEK",
        "NOK",
        "DKK",
        "PLN",
    }
)

_TWO_PLACES = Decimal("0.01")
_SIX_PLACES = Decimal("0.000001")


def to_money(value: str | float | int | Decimal, currency: str) -> Decimal:
    """Coerce an input to a 2-decimal-place Decimal for the given currency.

    Floats are converted via ``str()`` first to avoid 0.1 + 0.2 binary drift.
    Raises ``ValueError`` on bad input or unknown currency.
    """
    validate_currency(currency)
    if isinstance(value, float):
        value = format(value, ".6f")
    try:
        d = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"cannot coerce {value!r} to Decimal") from exc
    if not d.is_finite():
        raise ValueError(f"non-finite money value: {value!r}")
    return d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def to_rate(value: str | float | int | Decimal) -> Decimal:
    """Coerce an FX rate to 6dp. Rates are dimensionless; no currency check."""
    if isinstance(value, float):
        value = format(value, ".10f")
    try:
        d = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"cannot coerce {value!r} to Decimal") from exc
    if not d.is_finite() or d <= 0:
        raise ValueError(f"invalid fx rate: {value!r}")
    return d.quantize(_SIX_PLACES, rounding=ROUND_HALF_UP)


def validate_currency(code: str) -> str:
    """Return ``code`` if it's in the whitelist; else raise ``ValueError``.

    Currencies are normalized to upper-case. Unknown codes fail fast rather
    than propagating into file paths and sheet column names.
    """
    if not isinstance(code, str):
        raise ValueError(f"currency must be str, got {type(code).__name__}")
    up = code.strip().upper()
    if up not in ALLOWED_CURRENCIES:
        raise ValueError(f"unsupported currency {code!r}; see ALLOWED_CURRENCIES")
    return up


def money_str(value: Decimal) -> str:
    """Canonical textual form for a money value, used in filenames and hashes."""
    return format(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP), "f")
