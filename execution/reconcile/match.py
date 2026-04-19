"""Weighted invoice ↔ transaction matcher.

Core of the reconciliation engine. Takes one :class:`InvoiceCandidate`
and a collection of :class:`TransactionCandidate`s and returns the
best match decision (``auto_matched``, ``suggested``, or ``unmatched``)
plus the full score breakdown for audit.

Scoring, per the plan's § Phase 4:

- Short-circuit ladder (fast path) — avoids the weighted-score work
  when a high-confidence exact match is available:

    1. Same currency AND exact-amount AND vendor ≥ 0.85 AND
       date-within ±3 days → ``auto_matched`` with score 1.0.
    2. Same currency AND exact-amount AND vendor ≥ 0.80 AND
       date-within ±7 days → ``auto_matched`` with score 0.96.

- Weighted score for the rest:

      score = 0.50 * vendor_fuzz
            + 0.35 * amount_score
            + 0.10 * currency_score
            + 0.05 * date_score

- Thresholds:
    ``score ≥ 0.93`` → ``auto_matched``
    ``0.70 ≤ score < 0.93`` → ``suggested``
    below → ``unmatched``

- Unproven-vendor cap: if we haven't yet confirmed ≥3 matches for this
  vendor, the first auto_match is demoted to ``suggested`` (Midday
  learning). The caller supplies ``vendor_confirmed_count``.

- FX tolerance: when the invoice and transaction currencies differ
  and the invoice amount has been pre-converted by the caller into
  ``amount_gbp_converted``, we compare against ``txn.amount_gbp`` with
  a ±3 % tolerance to absorb Amex/Wise conversion margin.

All weights + thresholds are passed as an :class:`MatchPolicy` so the
plan's promise to keep them in ``.state/match_config.json`` is a
single-change integration away. No knob lives inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from rapidfuzz import fuzz

DEFAULT_AUTO_THRESHOLD: Final[Decimal] = Decimal("0.93")
DEFAULT_SUGGESTED_THRESHOLD: Final[Decimal] = Decimal("0.70")
DEFAULT_AMOUNT_TOLERANCE_ABS: Final[Decimal] = Decimal("0.01")
DEFAULT_FX_TOLERANCE: Final[Decimal] = Decimal("0.03")
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 14

DEFAULT_UNPROVEN_VENDOR_MIN_CONFIRMS: Final[int] = 3
DEFAULT_UNPROVEN_AUTO_CAP: Final[Decimal] = Decimal("0.85")


class MatchState(StrEnum):
    AUTO_MATCHED = "auto_matched"
    SUGGESTED = "suggested"
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class MatchPolicy:
    """Thresholds + weights. One struct → easy to load from JSON config."""

    vendor_weight: Decimal = Decimal("0.50")
    amount_weight: Decimal = Decimal("0.35")
    currency_weight: Decimal = Decimal("0.10")
    date_weight: Decimal = Decimal("0.05")
    auto_threshold: Decimal = DEFAULT_AUTO_THRESHOLD
    suggested_threshold: Decimal = DEFAULT_SUGGESTED_THRESHOLD
    amount_tolerance_abs: Decimal = DEFAULT_AMOUNT_TOLERANCE_ABS
    fx_tolerance: Decimal = DEFAULT_FX_TOLERANCE
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS
    unproven_vendor_min_confirms: int = DEFAULT_UNPROVEN_VENDOR_MIN_CONFIRMS
    unproven_auto_cap: Decimal = DEFAULT_UNPROVEN_AUTO_CAP


DEFAULT_POLICY: Final[MatchPolicy] = MatchPolicy()


@dataclass(frozen=True, slots=True)
class InvoiceCandidate:
    """What the matcher needs to know about one invoice."""

    invoice_id: str
    supplier_name: str
    invoice_date: date | None
    currency: str
    amount_gross: Decimal
    amount_gbp_converted: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TransactionCandidate:
    """What the matcher needs to know about one transaction."""

    txn_id: str
    description_canonical: str
    booking_date: date
    currency: str
    amount: Decimal
    amount_gbp: Decimal


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Per-dimension sub-scores in the range ``[0.0, 1.0]``."""

    vendor: Decimal
    amount: Decimal
    currency: Decimal
    date: Decimal


@dataclass(frozen=True, slots=True)
class MatchDecision:
    """Full record of a match attempt — enough to populate Run Status."""

    invoice_id: str
    txn_id: str | None
    state: MatchState
    score: Decimal
    breakdown: ScoreBreakdown | None
    reason: str
    demoted: bool = False


def score_pair(
    inv: InvoiceCandidate,
    txn: TransactionCandidate,
    *,
    policy: MatchPolicy = DEFAULT_POLICY,
) -> tuple[Decimal, ScoreBreakdown]:
    """Compute the weighted score for one (invoice, transaction) pair."""
    vendor = _vendor_score(inv.supplier_name, txn.description_canonical)
    amount = _amount_score(inv, txn, policy=policy)
    currency = Decimal("1.0") if inv.currency == txn.currency else Decimal("0.5")
    date = _date_score(inv.invoice_date, txn.booking_date, policy=policy)

    total = (
        policy.vendor_weight * vendor
        + policy.amount_weight * amount
        + policy.currency_weight * currency
        + policy.date_weight * date
    )
    return total.quantize(Decimal("0.0001")), ScoreBreakdown(
        vendor=vendor,
        amount=amount,
        currency=currency,
        date=date,
    )


def match_invoice(
    inv: InvoiceCandidate,
    candidates: list[TransactionCandidate],
    *,
    policy: MatchPolicy = DEFAULT_POLICY,
    vendor_confirmed_count: int = 0,
) -> MatchDecision:
    """Decide the best match (or lack thereof) for ``inv``."""
    if not candidates:
        return MatchDecision(
            invoice_id=inv.invoice_id,
            txn_id=None,
            state=MatchState.UNMATCHED,
            score=Decimal("0"),
            breakdown=None,
            reason="no candidates supplied",
        )

    # Short-circuit ladder.
    fast_match = _short_circuit(inv, candidates, policy=policy)
    if fast_match is not None:
        txn, fast_score, fast_reason = fast_match
        return _apply_demotion(
            inv=inv,
            txn=txn,
            score=fast_score,
            breakdown=None,
            reason=fast_reason,
            vendor_confirmed_count=vendor_confirmed_count,
            policy=policy,
        )

    # Weighted score over every candidate; pick the best.
    best_txn: TransactionCandidate | None = None
    best_score = Decimal("-1")
    best_breakdown: ScoreBreakdown | None = None
    for txn in candidates:
        score, breakdown = score_pair(inv, txn, policy=policy)
        if score > best_score:
            best_txn = txn
            best_score = score
            best_breakdown = breakdown

    assert best_txn is not None
    assert best_breakdown is not None

    if best_score >= policy.auto_threshold:
        return _apply_demotion(
            inv=inv,
            txn=best_txn,
            score=best_score,
            breakdown=best_breakdown,
            reason=f"weighted score {best_score}",
            vendor_confirmed_count=vendor_confirmed_count,
            policy=policy,
        )
    if best_score >= policy.suggested_threshold:
        return MatchDecision(
            invoice_id=inv.invoice_id,
            txn_id=best_txn.txn_id,
            state=MatchState.SUGGESTED,
            score=best_score,
            breakdown=best_breakdown,
            reason=f"weighted score {best_score}",
        )
    return MatchDecision(
        invoice_id=inv.invoice_id,
        txn_id=best_txn.txn_id,
        state=MatchState.UNMATCHED,
        score=best_score,
        breakdown=best_breakdown,
        reason=f"best weighted score {best_score} below suggested threshold",
    )


# ---------------------------------------------------------------------------
# Short-circuit ladder
# ---------------------------------------------------------------------------


def _short_circuit(
    inv: InvoiceCandidate,
    candidates: list[TransactionCandidate],
    *,
    policy: MatchPolicy,
) -> tuple[TransactionCandidate, Decimal, str] | None:
    """Return (txn, score=1.0/0.96, reason) if a fast-path auto-match fires."""
    for txn in candidates:
        if txn.currency != inv.currency:
            continue
        if abs(txn.amount - inv.amount_gross) > policy.amount_tolerance_abs:
            continue
        vendor = _vendor_score(inv.supplier_name, txn.description_canonical)
        if inv.invoice_date is None:
            continue
        delta_days = abs((txn.booking_date - inv.invoice_date).days)
        if vendor >= Decimal("0.85") and delta_days <= 3:
            return txn, Decimal("1.0"), "short-circuit: exact amount + vendor 0.85+ + ±3d"
        if vendor >= Decimal("0.80") and delta_days <= 7:
            return txn, Decimal("0.96"), "short-circuit: exact amount + vendor 0.80+ + ±7d"
    return None


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------


def _vendor_score(supplier: str, description: str) -> Decimal:
    """Fuzzy-match two strings using rapidfuzz.token_set_ratio (0-1)."""
    if not supplier or not description:
        return Decimal("0")
    ratio = fuzz.token_set_ratio(supplier.upper(), description.upper())
    return (Decimal(str(ratio)) / Decimal("100")).quantize(Decimal("0.0001"))


def _amount_score(
    inv: InvoiceCandidate,
    txn: TransactionCandidate,
    *,
    policy: MatchPolicy,
) -> Decimal:
    """Score the amount alignment between invoice and transaction."""
    if inv.currency == txn.currency:
        delta = abs(txn.amount - inv.amount_gross)
        if delta <= policy.amount_tolerance_abs:
            return Decimal("1.0")
        if inv.amount_gross == 0:
            return Decimal("0")
        relative = delta / inv.amount_gross
        # Scale linearly from 1 at 0% to 0 at 10%.
        score = Decimal("1.0") - (relative / Decimal("0.10"))
        return max(Decimal("0"), min(Decimal("1.0"), score))

    # Cross-currency — compare converted invoice GBP to txn GBP.
    if inv.amount_gbp_converted is None or txn.amount_gbp == 0:
        return Decimal("0")
    relative = abs(txn.amount_gbp - inv.amount_gbp_converted) / txn.amount_gbp
    if relative <= policy.fx_tolerance:
        return Decimal("1.0")
    if relative >= Decimal("0.10"):
        return Decimal("0")
    # Linear between fx_tolerance (1.0) and 10% (0)
    span = Decimal("0.10") - policy.fx_tolerance
    above_tolerance = relative - policy.fx_tolerance
    return max(Decimal("0"), Decimal("1.0") - above_tolerance / span)


def _date_score(
    invoice_date: date | None,
    booking_date: date,
    *,
    policy: MatchPolicy,
) -> Decimal:
    """1.0 for same day, 0 for beyond window, linear in between."""
    if invoice_date is None:
        return Decimal("0")
    delta = abs((booking_date - invoice_date).days)
    if delta >= policy.date_window_days:
        return Decimal("0")
    score = Decimal("1.0") - Decimal(delta) / Decimal(policy.date_window_days)
    return score.quantize(Decimal("0.0001"))


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------


def _apply_demotion(
    *,
    inv: InvoiceCandidate,
    txn: TransactionCandidate,
    score: Decimal,
    breakdown: ScoreBreakdown | None,
    reason: str,
    vendor_confirmed_count: int,
    policy: MatchPolicy,
) -> MatchDecision:
    """Cap auto-match for unproven vendors at ``unproven_auto_cap``."""
    if (
        vendor_confirmed_count < policy.unproven_vendor_min_confirms
        and score > policy.unproven_auto_cap
    ):
        return MatchDecision(
            invoice_id=inv.invoice_id,
            txn_id=txn.txn_id,
            state=MatchState.SUGGESTED,
            score=score,
            breakdown=breakdown,
            reason=(
                f"{reason}; demoted (vendor confirmed {vendor_confirmed_count}"
                f" < {policy.unproven_vendor_min_confirms})"
            ),
            demoted=True,
        )
    state = (
        MatchState.AUTO_MATCHED
        if score >= policy.auto_threshold
        else MatchState.SUGGESTED
    )
    return MatchDecision(
        invoice_id=inv.invoice_id,
        txn_id=txn.txn_id,
        state=state,
        score=score,
        breakdown=breakdown,
        reason=reason,
    )


__all__ = [
    "DEFAULT_AMOUNT_TOLERANCE_ABS",
    "DEFAULT_AUTO_THRESHOLD",
    "DEFAULT_DATE_WINDOW_DAYS",
    "DEFAULT_FX_TOLERANCE",
    "DEFAULT_SUGGESTED_THRESHOLD",
    "DEFAULT_UNPROVEN_AUTO_CAP",
    "DEFAULT_UNPROVEN_VENDOR_MIN_CONFIRMS",
    "InvoiceCandidate",
    "MatchDecision",
    "MatchPolicy",
    "MatchState",
    "ScoreBreakdown",
    "TransactionCandidate",
    "match_invoice",
    "score_pair",
]
