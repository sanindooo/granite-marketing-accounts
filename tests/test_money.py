"""Tests for the Decimal chokepoint and currency whitelist."""

from __future__ import annotations

from decimal import Decimal

import pytest

from execution.shared.money import (
    ALLOWED_CURRENCIES,
    money_str,
    to_money,
    to_rate,
    validate_currency,
)


class TestToMoney:
    def test_float_precision_goes_through_str(self) -> None:
        # 0.1 + 0.2 is the classic float drift case
        assert to_money(0.1 + 0.2, "GBP") == Decimal("0.30")

    def test_string_input(self) -> None:
        assert to_money("42.50", "GBP") == Decimal("42.50")

    def test_decimal_passthrough(self) -> None:
        assert to_money(Decimal("99.999"), "GBP") == Decimal("100.00")

    def test_rounds_half_up(self) -> None:
        assert to_money("0.005", "GBP") == Decimal("0.01")

    def test_negative_amounts_allowed(self) -> None:
        # Refunds and reversals are real money values
        assert to_money("-10.50", "GBP") == Decimal("-10.50")

    def test_unknown_currency_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported currency"):
            to_money(10, "XYZ")

    def test_gibberish_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot coerce"):
            to_money("not a number", "GBP")

    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            to_money(Decimal("NaN"), "GBP")


class TestValidateCurrency:
    def test_accepts_whitelist(self) -> None:
        for code in ("GBP", "USD", "EUR"):
            assert validate_currency(code) == code

    def test_normalizes_case(self) -> None:
        assert validate_currency("gbp") == "GBP"
        assert validate_currency(" usd ") == "USD"

    def test_whitelist_contains_expected(self) -> None:
        assert "GBP" in ALLOWED_CURRENCIES
        assert "USD" in ALLOWED_CURRENCIES
        assert "EUR" in ALLOWED_CURRENCIES

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValueError):
            validate_currency("")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            validate_currency(42)  # type: ignore[arg-type]


class TestToRate:
    def test_six_dp_precision(self) -> None:
        # ECB rates routinely carry 6 decimals
        assert to_rate("0.794012") == Decimal("0.794012")

    def test_float_input(self) -> None:
        assert to_rate(0.7940) == Decimal("0.794000")

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError):
            to_rate(0)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            to_rate(-1.5)


class TestMoneyStr:
    def test_canonical_form(self) -> None:
        assert money_str(Decimal("42.5")) == "42.50"
        assert money_str(Decimal("100")) == "100.00"
        assert money_str(Decimal("0")) == "0.00"
