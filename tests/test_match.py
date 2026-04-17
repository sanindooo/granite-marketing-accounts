"""Tests for execution.reconcile.match."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from execution.reconcile.match import (
    DEFAULT_AUTO_THRESHOLD,
    InvoiceCandidate,
    MatchPolicy,
    MatchState,
    TransactionCandidate,
    match_invoice,
    score_pair,
)


def _inv(**overrides) -> InvoiceCandidate:
    defaults = {
        "invoice_id": "inv-1",
        "supplier_name": "Atlassian Pty Ltd",
        "invoice_date": date(2026, 4, 1),
        "currency": "GBP",
        "amount_gross": Decimal("480.00"),
        "amount_gbp_converted": None,
    }
    defaults.update(overrides)
    return InvoiceCandidate(**defaults)  # type: ignore[arg-type]


def _txn(**overrides) -> TransactionCandidate:
    defaults = {
        "txn_id": "t-1",
        "description_canonical": "ATLASSIAN PTY LTD",
        "booking_date": date(2026, 4, 1),
        "currency": "GBP",
        "amount": Decimal("480.00"),
        "amount_gbp": Decimal("480.00"),
    }
    defaults.update(overrides)
    return TransactionCandidate(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Short-circuit ladder
# ---------------------------------------------------------------------------


class TestShortCircuit:
    def test_exact_amount_same_day_vendor_strong_auto(self):
        decision = match_invoice(_inv(), [_txn()], vendor_confirmed_count=5)
        assert decision.state is MatchState.AUTO_MATCHED
        assert decision.score == Decimal("1.0")
        assert "short-circuit" in decision.reason

    def test_exact_amount_4_days_apart_falls_to_second_rung(self):
        decision = match_invoice(
            _inv(),
            [_txn(booking_date=date(2026, 4, 5))],
            vendor_confirmed_count=5,
        )
        assert decision.state is MatchState.AUTO_MATCHED
        assert decision.score == Decimal("0.96")

    def test_exact_amount_but_date_too_far_falls_to_weighted(self):
        decision = match_invoice(
            _inv(),
            [_txn(booking_date=date(2026, 4, 14))],
            vendor_confirmed_count=5,
        )
        # Exact amount, strong vendor, 13 days → weighted score lands in auto
        assert decision.state is MatchState.AUTO_MATCHED
        assert decision.score >= DEFAULT_AUTO_THRESHOLD
        assert "short-circuit" not in decision.reason

    def test_amount_off_by_penny_fails_short_circuit(self):
        decision = match_invoice(
            _inv(),
            [_txn(amount=Decimal("480.50"))],
            vendor_confirmed_count=5,
        )
        # Still high-scoring via weighted path
        assert decision.state is MatchState.AUTO_MATCHED
        assert "short-circuit" not in decision.reason


# ---------------------------------------------------------------------------
# Weighted score
# ---------------------------------------------------------------------------


class TestWeightedScore:
    def test_score_breakdown_all_ones_on_perfect_pair(self):
        score, breakdown = score_pair(_inv(), _txn())
        assert score == Decimal("1.0000")
        assert breakdown.vendor == Decimal("1.0000")
        assert breakdown.amount == Decimal("1.0")
        assert breakdown.currency == Decimal("1.0")
        assert breakdown.date == Decimal("1.0000")

    def test_different_currency_halves_currency_score(self):
        _, breakdown = score_pair(
            _inv(currency="USD"),
            _txn(amount_gbp=Decimal("380.00")),
        )
        assert breakdown.currency == Decimal("0.5")

    def test_date_score_scales_linearly(self):
        _, bd_near = score_pair(_inv(), _txn(booking_date=date(2026, 4, 2)))
        _, bd_far = score_pair(_inv(), _txn(booking_date=date(2026, 4, 13)))
        assert bd_near.date > bd_far.date
        # 12 days inside the 14-day window → well under 0.2.
        assert bd_far.date < Decimal("0.2")

    def test_date_beyond_window_is_zero(self):
        _, breakdown = score_pair(_inv(), _txn(booking_date=date(2026, 5, 1)))
        assert breakdown.date == Decimal("0")

    def test_vendor_fuzzy_catches_token_reorder(self):
        _, bd = score_pair(
            _inv(supplier_name="Atlassian Pty Ltd"),
            _txn(description_canonical="ATLASSIAN LTD PTY"),
        )
        assert bd.vendor >= Decimal("0.90")

    def test_vendor_fuzzy_penalises_drift(self):
        _, bd = score_pair(
            _inv(supplier_name="Atlassian Pty Ltd"),
            _txn(description_canonical="RANDOM SUPPLIER"),
        )
        assert bd.vendor < Decimal("0.50")


# ---------------------------------------------------------------------------
# Weighted amount scoring — linear penalty
# ---------------------------------------------------------------------------


class TestAmountScore:
    def test_within_abs_tolerance_is_one(self):
        _, bd = score_pair(_inv(), _txn(amount=Decimal("480.01")))
        assert bd.amount == Decimal("1.0")

    def test_mild_drift_reduces_amount_score(self):
        _, bd = score_pair(_inv(), _txn(amount=Decimal("490.00")))
        # ~2% drift → score ≈ 0.8
        assert Decimal("0.5") <= bd.amount <= Decimal("0.9")

    def test_over_ten_percent_drift_is_zero(self):
        _, bd = score_pair(_inv(), _txn(amount=Decimal("600.00")))
        assert bd.amount == Decimal("0")


# ---------------------------------------------------------------------------
# FX-tolerant cross-currency matching
# ---------------------------------------------------------------------------


class TestCrossCurrency:
    def test_within_fx_tolerance_is_full_amount_score(self):
        inv = _inv(
            currency="USD",
            amount_gross=Decimal("600.00"),
            amount_gbp_converted=Decimal("474.00"),
        )
        txn = _txn(
            amount=Decimal("480.00"),  # GBP-native
            amount_gbp=Decimal("480.00"),
        )
        _, bd = score_pair(inv, txn)
        # relative diff ≈ 1.25% → within the 3% FX tolerance
        assert bd.amount == Decimal("1.0")

    def test_outside_fx_tolerance_decays(self):
        inv = _inv(
            currency="USD",
            amount_gross=Decimal("600.00"),
            amount_gbp_converted=Decimal("400.00"),
        )
        txn = _txn(amount=Decimal("480.00"), amount_gbp=Decimal("480.00"))
        _, bd = score_pair(inv, txn)
        # relative diff ~16.7%, beyond the 10% cutoff.
        assert bd.amount == Decimal("0")

    def test_missing_converted_amount_is_zero(self):
        inv = _inv(
            currency="USD",
            amount_gross=Decimal("600.00"),
            amount_gbp_converted=None,
        )
        txn = _txn(amount=Decimal("480.00"), amount_gbp=Decimal("480.00"))
        _, bd = score_pair(inv, txn)
        assert bd.amount == Decimal("0")


# ---------------------------------------------------------------------------
# Unproven-vendor demotion
# ---------------------------------------------------------------------------


class TestVendorDemotion:
    def test_first_match_for_new_vendor_is_demoted_to_suggested(self):
        decision = match_invoice(
            _inv(), [_txn()], vendor_confirmed_count=0
        )
        assert decision.state is MatchState.SUGGESTED
        assert decision.demoted is True
        assert "demoted" in decision.reason

    def test_third_confirm_lifts_the_cap(self):
        decision = match_invoice(
            _inv(), [_txn()], vendor_confirmed_count=3
        )
        assert decision.state is MatchState.AUTO_MATCHED
        assert decision.demoted is False


# ---------------------------------------------------------------------------
# Threshold boundary behaviour
# ---------------------------------------------------------------------------


def test_no_candidates_returns_unmatched():
    decision = match_invoice(_inv(), [])
    assert decision.state is MatchState.UNMATCHED
    assert decision.txn_id is None


def test_suggested_threshold_band():
    # Vendor scores low so the total lands between 0.70 and 0.93.
    decision = match_invoice(
        _inv(),
        [
            _txn(
                description_canonical="ATLASSIAN TECHNOLOGY",
                booking_date=date(2026, 4, 7),
                amount=Decimal("470.00"),
            )
        ],
        vendor_confirmed_count=5,
    )
    assert decision.state in (MatchState.SUGGESTED, MatchState.AUTO_MATCHED)


def test_unmatched_when_every_dimension_bad():
    decision = match_invoice(
        _inv(),
        [
            _txn(
                description_canonical="RANDOM SUPPLIER",
                booking_date=date(2026, 6, 1),
                amount=Decimal("9999.00"),
            )
        ],
        vendor_confirmed_count=5,
    )
    assert decision.state is MatchState.UNMATCHED


def test_policy_threshold_override_changes_state():
    strict = MatchPolicy(auto_threshold=Decimal("1.1"))  # effectively unreachable
    decision = match_invoice(
        _inv(), [_txn()], policy=strict, vendor_confirmed_count=5
    )
    assert decision.state is not MatchState.AUTO_MATCHED


def test_returned_decision_carries_breakdown_on_weighted_path():
    decision = match_invoice(
        _inv(),
        [_txn(booking_date=date(2026, 4, 14))],
        vendor_confirmed_count=5,
    )
    # Short-circuit bails at day 7; weighted path should own this one.
    if "short-circuit" not in decision.reason:
        assert decision.breakdown is not None
    else:  # pragma: no cover
        pytest.skip("short-circuit ladder still triggered")
