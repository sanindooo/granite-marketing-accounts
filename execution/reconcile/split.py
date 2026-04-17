"""Subset-sum matcher for 1:N and N:1 invoice↔transaction links.

The primary matcher in :mod:`execution.reconcile.match` handles the 1:1
case where one invoice equals one transaction. This module covers:

- **1:N (one invoice, many transactions)** — a single invoice paid by
  two or more charges (e.g. a retainer settled via two card payments
  on the same day, or an annual subscription split across months).
- **N:1 (many invoices, one transaction)** — one combined charge that
  covers several line-item invoices from the same vendor.

Per the plan (`Phase 4 → split.py`):

- Cap subset size at 3. Brute-forcing larger subsets is both
  computationally pointless at this volume and invites false
  positives.
- Exact-sum match required for ``auto_matched``; anything with a
  non-zero residual still surfaces as ``suggested`` so the user can
  confirm.
- ±£0.50 total tolerance absorbs fee drift and rounding on multi-leg
  payments.
- ±7-day window — the constituent rows must fall within ±7 days of
  the *anchor* row (the invoice date in the 1:N case, the transaction
  booking date in the N:1 case).

The output is deliberately shaped to feed
:class:`execution.reconcile.state.RowState.SUGGESTED` /
:class:`AUTO_MATCHED` plus the ``reconciliation_links`` rows — the
caller allocates ``allocated_amount_gbp`` per constituent and writes
``link_kind='split_txn'`` or ``'split_invoice'``.

Scope boundaries carried forward from the plan:

- This module does not touch FX, does not touch the state machine,
  and does not write to SQLite. It is a pure function over two lists
  and a policy; the orchestrator composes it with the rest.
- We don't try to guess partial allocations against *different*
  invoices — if no exact (±tolerance) subset matches the anchor, we
  return ``None`` and let the 1:1 matcher / Exceptions flow handle it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from typing import Final

DEFAULT_SUBSET_CAP: Final[int] = 3
DEFAULT_AMOUNT_TOLERANCE: Final[Decimal] = Decimal("0.50")
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 7
DEFAULT_AUTO_EXACT_TOLERANCE: Final[Decimal] = Decimal("0.01")


class SplitKind(StrEnum):
    """Direction of the split relation."""

    ONE_TO_MANY = "split_txn"  # one invoice, many transactions
    MANY_TO_ONE = "split_invoice"  # many invoices, one transaction


@dataclass(frozen=True, slots=True)
class SplitPolicy:
    """Tunables carried alongside the matcher in ``.state/match_config.json``."""

    subset_cap: int = DEFAULT_SUBSET_CAP
    tolerance_abs: Decimal = DEFAULT_AMOUNT_TOLERANCE
    auto_tolerance_abs: Decimal = DEFAULT_AUTO_EXACT_TOLERANCE
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS


DEFAULT_POLICY: Final[SplitPolicy] = SplitPolicy()


@dataclass(frozen=True, slots=True)
class SplitCandidate:
    """A row that can participate in a subset-sum match."""

    row_id: str
    amount: Decimal
    row_date: date


@dataclass(frozen=True, slots=True)
class SplitMatch:
    """The result of a successful subset-sum search."""

    kind: SplitKind
    anchor_id: str
    constituent_ids: tuple[str, ...]
    total: Decimal
    residual: Decimal  # ``target - total``; positive means undershoot
    auto: bool  # True iff within auto_tolerance of target
    reason: str


def find_split_for_invoice(
    *,
    anchor: SplitCandidate,
    candidates: list[SplitCandidate],
    policy: SplitPolicy = DEFAULT_POLICY,
) -> SplitMatch | None:
    """1:N search — find the best subset of transactions that sums to the invoice.

    The anchor is the **invoice amount** (positive Decimal). Candidates
    are **card debits** (negative Decimals in the ledger); we compare
    against their absolute value so the subset sums in the same sign
    space as the invoice.

    Returns the highest-quality subset (smallest residual, then
    smallest subset size) or ``None`` when nothing clears tolerance.
    """
    return _find_subset(
        anchor=anchor,
        candidates=candidates,
        policy=policy,
        kind=SplitKind.ONE_TO_MANY,
        abs_amounts=True,
    )


def find_split_for_transaction(
    *,
    anchor: SplitCandidate,
    candidates: list[SplitCandidate],
    policy: SplitPolicy = DEFAULT_POLICY,
) -> SplitMatch | None:
    """N:1 search — find a subset of invoices that sums to the transaction.

    Mirror of :func:`find_split_for_invoice`. The anchor is the
    **transaction amount** (in the caller's sign convention — we take
    ``abs()`` internally); candidates are **invoice gross amounts**.
    """
    return _find_subset(
        anchor=anchor,
        candidates=candidates,
        policy=policy,
        kind=SplitKind.MANY_TO_ONE,
        abs_amounts=True,
    )


# ---------------------------------------------------------------------------
# Internal — shared brute-force search
# ---------------------------------------------------------------------------


def _find_subset(
    *,
    anchor: SplitCandidate,
    candidates: list[SplitCandidate],
    policy: SplitPolicy,
    kind: SplitKind,
    abs_amounts: bool,
) -> SplitMatch | None:
    """Brute-force subset-sum constrained to the date window + subset cap.

    Returns the best match by (auto-first, smallest residual, smallest
    subset size, earliest candidate). ``None`` when no subset clears
    ``policy.tolerance_abs``.
    """
    if policy.subset_cap < 2:
        return None

    target = abs(anchor.amount) if abs_amounts else anchor.amount
    window = timedelta(days=policy.date_window_days)
    cutoff_low = anchor.row_date - window
    cutoff_high = anchor.row_date + window

    # Early prune: keep only same-sign non-zero candidates inside the window.
    pool: list[tuple[SplitCandidate, Decimal]] = []
    seen_ids: set[str] = set()
    for cand in candidates:
        if cand.row_id in seen_ids:
            # Defensive — callers shouldn't pass dupes, but if they do we
            # keep the first (stable ordering downstream).
            continue
        seen_ids.add(cand.row_id)
        if cand.row_date < cutoff_low or cand.row_date > cutoff_high:
            continue
        value = abs(cand.amount) if abs_amounts else cand.amount
        if value <= 0:
            continue
        # Skip anything single-handedly larger than the loose tolerance —
        # it can't contribute to an exact-sum subset without a negative
        # counterpart.
        if value > target + policy.tolerance_abs:
            continue
        pool.append((cand, value))

    if not pool:
        return None

    # Sort by absolute amount descending — bigger rows prune the
    # combinations space faster.
    pool.sort(key=lambda pair: (-pair[1], pair[0].row_date, pair[0].row_id))

    best: SplitMatch | None = None
    # Start from size 2; a single candidate is a 1:1 match and belongs
    # to the main matcher, not this module.
    max_size = min(policy.subset_cap, len(pool))
    for size in range(2, max_size + 1):
        for combo in combinations(pool, size):
            total = sum((pair[1] for pair in combo), start=Decimal("0"))
            residual = target - total
            if abs(residual) > policy.tolerance_abs:
                continue
            auto = abs(residual) <= policy.auto_tolerance_abs
            ids = tuple(pair[0].row_id for pair in combo)
            reason = (
                f"subset-sum {len(combo)}-way: total={total} "
                f"target={target} residual={residual}"
            )
            match = SplitMatch(
                kind=kind,
                anchor_id=anchor.row_id,
                constituent_ids=ids,
                total=total,
                residual=residual,
                auto=auto,
                reason=reason,
            )
            if best is None or _prefer(match, best):
                best = match
    return best


def _prefer(left: SplitMatch, right: SplitMatch) -> bool:
    """Return True if ``left`` is a strictly better match than ``right``.

    Ordering:

    1. Auto-matches beat suggestions.
    2. Smaller residual (closer to exact) wins.
    3. Smaller constituent count wins.
    4. Lexicographic tie-break on constituent ids for determinism.
    """
    if left.auto != right.auto:
        return left.auto and not right.auto
    if abs(left.residual) != abs(right.residual):
        return abs(left.residual) < abs(right.residual)
    if len(left.constituent_ids) != len(right.constituent_ids):
        return len(left.constituent_ids) < len(right.constituent_ids)
    return left.constituent_ids < right.constituent_ids


__all__ = [
    "DEFAULT_AMOUNT_TOLERANCE",
    "DEFAULT_AUTO_EXACT_TOLERANCE",
    "DEFAULT_DATE_WINDOW_DAYS",
    "DEFAULT_POLICY",
    "DEFAULT_SUBSET_CAP",
    "SplitCandidate",
    "SplitKind",
    "SplitMatch",
    "SplitPolicy",
    "find_split_for_invoice",
    "find_split_for_transaction",
]
