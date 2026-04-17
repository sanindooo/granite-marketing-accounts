"""Tests for execution.reconcile.split — 1:N / N:1 subset-sum matcher."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from execution.reconcile.split import (
    DEFAULT_AMOUNT_TOLERANCE,
    DEFAULT_DATE_WINDOW_DAYS,
    DEFAULT_SUBSET_CAP,
    SplitCandidate,
    SplitKind,
    SplitPolicy,
    find_split_for_invoice,
    find_split_for_transaction,
)


def _cand(row_id: str, amount: str | Decimal, row_date: date) -> SplitCandidate:
    return SplitCandidate(
        row_id=row_id,
        amount=Decimal(amount),
        row_date=row_date,
    )


# ---------------------------------------------------------------------------
# 1:N — invoice anchor, several transaction debits
# ---------------------------------------------------------------------------


def test_exact_two_way_split_is_auto():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-40.00", date(2026, 4, 10)),
        _cand("tx-2", "-60.00", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert match.auto is True
    assert match.kind == SplitKind.ONE_TO_MANY
    assert set(match.constituent_ids) == {"tx-1", "tx-2"}
    assert match.total == Decimal("100.00")
    assert match.residual == Decimal("0.00")


def test_three_way_split_auto():
    invoice = _cand("inv-1", "300.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-100.00", date(2026, 4, 9)),
        _cand("tx-2", "-100.00", date(2026, 4, 10)),
        _cand("tx-3", "-100.00", date(2026, 4, 11)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert match.auto is True
    assert set(match.constituent_ids) == {"tx-1", "tx-2", "tx-3"}


def test_tolerance_split_is_suggested_not_auto():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-40.30", date(2026, 4, 10)),
        _cand("tx-2", "-60.00", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    # Residual is £0.30, inside tolerance but outside auto-exact tolerance.
    assert match is not None
    assert match.auto is False
    assert abs(match.residual) == Decimal("-0.30").copy_abs()
    assert set(match.constituent_ids) == {"tx-1", "tx-2"}


def test_returns_none_when_no_subset_within_tolerance():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-20.00", date(2026, 4, 10)),
        _cand("tx-2", "-30.00", date(2026, 4, 10)),
    ]  # Max subset 50.00, target 100.00.
    assert find_split_for_invoice(anchor=invoice, candidates=txns) is None


def test_single_candidate_is_never_returned():
    invoice = _cand("inv-1", "40.00", date(2026, 4, 10))
    txns = [_cand("tx-1", "-40.00", date(2026, 4, 10))]
    # Single-candidate exact match is the main matcher's job; split
    # returns None because subset size < 2.
    assert find_split_for_invoice(anchor=invoice, candidates=txns) is None


def test_subset_cap_rejects_four_way_split():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-25.00", date(2026, 4, 10)),
        _cand("tx-2", "-25.00", date(2026, 4, 10)),
        _cand("tx-3", "-25.00", date(2026, 4, 10)),
        _cand("tx-4", "-25.00", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    # No valid 2- or 3-way subset sums to 100.00; 4-way is capped.
    assert match is None


def test_subset_cap_override():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-25.00", date(2026, 4, 10)),
        _cand("tx-2", "-25.00", date(2026, 4, 10)),
        _cand("tx-3", "-25.00", date(2026, 4, 10)),
        _cand("tx-4", "-25.00", date(2026, 4, 10)),
    ]
    policy = SplitPolicy(subset_cap=4)
    match = find_split_for_invoice(anchor=invoice, candidates=txns, policy=policy)
    assert match is not None
    assert len(match.constituent_ids) == 4


def test_date_window_excludes_stale_candidates():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-far", "-40.00", date(2026, 3, 1)),  # > 7 days away
        _cand("tx-near", "-40.00", date(2026, 4, 10)),
        _cand("tx-also-near", "-60.00", date(2026, 4, 11)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert "tx-far" not in match.constituent_ids
    assert set(match.constituent_ids) == {"tx-near", "tx-also-near"}


def test_prefers_auto_over_tolerance_match():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        # Tolerance match (£0.30 residual)
        _cand("tx-fuzzy-1", "-40.30", date(2026, 4, 10)),
        _cand("tx-fuzzy-2", "-60.00", date(2026, 4, 10)),
        # Exact match
        _cand("tx-exact-1", "-50.00", date(2026, 4, 9)),
        _cand("tx-exact-2", "-50.00", date(2026, 4, 9)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert match.auto is True
    assert set(match.constituent_ids) == {"tx-exact-1", "tx-exact-2"}


def test_prefers_smaller_subset_when_both_exact():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-half-1", "-50.00", date(2026, 4, 10)),
        _cand("tx-half-2", "-50.00", date(2026, 4, 10)),
        _cand("tx-third-1", "-33.33", date(2026, 4, 10)),
        _cand("tx-third-2", "-33.33", date(2026, 4, 10)),
        _cand("tx-third-3", "-33.34", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert len(match.constituent_ids) == 2


def test_drops_zero_and_same_sign_excess_candidates():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-zero", "0.00", date(2026, 4, 10)),
        _cand("tx-tiny", "-1.00", date(2026, 4, 10)),
        _cand("tx-huge", "-500.00", date(2026, 4, 10)),  # larger than target + tol
        _cand("tx-a", "-40.00", date(2026, 4, 10)),
        _cand("tx-b", "-60.00", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert "tx-huge" not in match.constituent_ids
    assert "tx-zero" not in match.constituent_ids


def test_deduplicates_candidate_row_ids():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-40.00", date(2026, 4, 10)),
        _cand("tx-1", "-40.00", date(2026, 4, 10)),  # duplicate
        _cand("tx-2", "-60.00", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert sorted(match.constituent_ids) == ["tx-1", "tx-2"]


# ---------------------------------------------------------------------------
# N:1 — transaction anchor, several invoices
# ---------------------------------------------------------------------------


def test_many_to_one_exact_sums_invoices():
    txn = _cand("tx-1", "-150.00", date(2026, 4, 10))
    invoices = [
        _cand("inv-1", "50.00", date(2026, 4, 9)),
        _cand("inv-2", "100.00", date(2026, 4, 10)),
    ]
    match = find_split_for_transaction(anchor=txn, candidates=invoices)
    assert match is not None
    assert match.kind == SplitKind.MANY_TO_ONE
    assert match.auto is True
    assert set(match.constituent_ids) == {"inv-1", "inv-2"}


def test_many_to_one_returns_none_outside_window():
    txn = _cand("tx-1", "-150.00", date(2026, 4, 10))
    invoices = [
        _cand("inv-far", "50.00", date(2026, 3, 1)),  # outside ±7d
        _cand("inv-2", "100.00", date(2026, 4, 10)),
    ]
    # Only inv-2 is in window; size-2 subset can't form.
    assert find_split_for_transaction(anchor=txn, candidates=invoices) is None


def test_many_to_one_prefers_smaller_residual():
    txn = _cand("tx-1", "-100.00", date(2026, 4, 10))
    invoices = [
        _cand("inv-a", "40.00", date(2026, 4, 10)),
        _cand("inv-b", "60.00", date(2026, 4, 10)),
        _cand("inv-c", "45.00", date(2026, 4, 10)),
        _cand("inv-d", "55.30", date(2026, 4, 10)),  # 45+55.30 = 100.30 → residual -0.30
    ]
    match = find_split_for_transaction(anchor=txn, candidates=invoices)
    assert match is not None
    # Exact 40+60 beats 45+55.30.
    assert set(match.constituent_ids) == {"inv-a", "inv-b"}
    assert match.residual == Decimal("0.00")


# ---------------------------------------------------------------------------
# Policy + module-level contract
# ---------------------------------------------------------------------------


def test_default_policy_matches_plan():
    assert DEFAULT_SUBSET_CAP == 3
    assert Decimal("0.50") == DEFAULT_AMOUNT_TOLERANCE
    assert DEFAULT_DATE_WINDOW_DAYS == 7


def test_subset_cap_one_returns_none():
    invoice = _cand("inv-1", "40.00", date(2026, 4, 10))
    txns = [_cand("tx-1", "-40.00", date(2026, 4, 10))]
    policy = SplitPolicy(subset_cap=1)
    assert find_split_for_invoice(anchor=invoice, candidates=txns, policy=policy) is None


def test_reason_includes_total_and_residual():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-1", "-40.00", date(2026, 4, 10)),
        _cand("tx-2", "-60.00", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    assert "total=100.00" in match.reason
    assert "residual=0.00" in match.reason


def test_empty_candidates_returns_none():
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    assert find_split_for_invoice(anchor=invoice, candidates=[]) is None


def test_deterministic_tie_break_on_constituent_ids():
    # Same residual, same size, different ids → lexicographic winner.
    invoice = _cand("inv-1", "100.00", date(2026, 4, 10))
    txns = [
        _cand("tx-a", "-50.00", date(2026, 4, 10)),
        _cand("tx-b", "-50.00", date(2026, 4, 10)),
        _cand("tx-c", "-50.00", date(2026, 4, 10)),
        _cand("tx-d", "-50.00", date(2026, 4, 10)),
    ]
    match = find_split_for_invoice(anchor=invoice, candidates=txns)
    assert match is not None
    # Each 2-of-4 pair sums to 100 exactly; tie-break picks the
    # lexicographically earliest pair (tx-a, tx-b).
    assert match.constituent_ids == ("tx-a", "tx-b")
