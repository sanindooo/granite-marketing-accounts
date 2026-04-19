"""Thread-safe budget tracking for concurrent LLM API calls.

This module provides a provider-agnostic budget tracker that can be shared
across multiple worker threads. It uses threading.Lock to ensure atomic
budget checks and updates.

Used by both ClaudeClient and OpenAIClient when running parallel processing
with ThreadPoolExecutor.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    pass

Provider = Literal["claude", "openai"]
Stage = Literal["classify", "extract", "smoke"]

_FOUR_PLACES: Decimal = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class LLMCall:
    """Record of a single LLM API call (provider-agnostic)."""

    provider: Provider
    model: str
    stage: Stage
    input_tokens: int
    output_tokens: int
    cost_gbp: Decimal


class SharedBudget:
    """Thread-safe per-run LLM spend ledger with a hard ceiling.

    All methods that read or modify budget state are protected by a Lock
    to ensure atomic operations when called from multiple worker threads.

    Example usage with ThreadPoolExecutor::

        budget = SharedBudget(ceiling_gbp=Decimal("5.00"))

        def worker(email):
            # Each worker shares the same budget
            budget.reserve(estimated_cost)  # Raises if ceiling exceeded
            result = call_llm(...)
            budget.record(call)
            return result

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, email) for email in emails]
            ...
    """

    def __init__(self, ceiling_gbp: Decimal) -> None:
        if ceiling_gbp <= 0:
            raise ValueError(f"ceiling must be positive, got {ceiling_gbp}")
        self._lock = threading.Lock()
        self._ceiling_gbp = ceiling_gbp
        self._spent_gbp = Decimal("0.0000")
        self._calls: list[LLMCall] = []

    def reserve(self, estimated_gbp: Decimal) -> None:
        """Pre-check that estimated cost fits within remaining budget.

        Call this BEFORE making an LLM API call. Raises BudgetExceededError
        if the ceiling would be exceeded. This allows workers to stop early
        rather than exceeding the budget.

        Thread-safe: uses Lock to ensure atomic check.
        """
        with self._lock:
            if self._spent_gbp + estimated_gbp > self._ceiling_gbp:
                from execution.shared.errors import BudgetExceededError

                raise BudgetExceededError(
                    f"LLM budget of £{self._ceiling_gbp} would be exceeded "
                    f"(spent £{self._spent_gbp}, requested £{estimated_gbp})",
                    source="budget",
                    details={
                        "ceiling_gbp": format(self._ceiling_gbp, "f"),
                        "spent_gbp": format(self._spent_gbp, "f"),
                        "requested_gbp": format(estimated_gbp, "f"),
                    },
                )

    def record(self, call: LLMCall) -> None:
        """Record a completed LLM call and update spent amount.

        Call this AFTER an LLM API call completes successfully.

        Thread-safe: uses Lock to ensure atomic update.
        """
        with self._lock:
            self._calls.append(call)
            self._spent_gbp += call.cost_gbp

    @property
    def spent_gbp(self) -> Decimal:
        """Current total spent amount in GBP. Thread-safe read."""
        with self._lock:
            return self._spent_gbp

    @property
    def ceiling_gbp(self) -> Decimal:
        """Budget ceiling in GBP. Thread-safe read."""
        with self._lock:
            return self._ceiling_gbp

    @property
    def remaining_gbp(self) -> Decimal:
        """Remaining budget in GBP. Thread-safe read."""
        with self._lock:
            return self._ceiling_gbp - self._spent_gbp

    def stats(self) -> dict[str, object]:
        """Summary safe to embed into runs.stats_json. Thread-safe read."""
        with self._lock:
            return {
                "call_count": len(self._calls),
                "spent_gbp": format(self._spent_gbp, "f"),
                "ceiling_gbp": format(self._ceiling_gbp, "f"),
                "by_provider": {
                    "claude": sum(1 for c in self._calls if c.provider == "claude"),
                    "openai": sum(1 for c in self._calls if c.provider == "openai"),
                },
                "total_input_tokens": sum(c.input_tokens for c in self._calls),
                "total_output_tokens": sum(c.output_tokens for c in self._calls),
            }


__all__ = [
    "LLMCall",
    "Provider",
    "SharedBudget",
    "Stage",
]
