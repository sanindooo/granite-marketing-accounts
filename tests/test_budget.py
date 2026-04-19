"""Tests for thread-safe SharedBudget."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

import pytest

from execution.shared.budget import LLMCall, SharedBudget
from execution.shared.errors import BudgetExceededError


class TestSharedBudget:
    """Unit tests for SharedBudget thread safety."""

    def test_init_requires_positive_ceiling(self) -> None:
        with pytest.raises(ValueError, match="ceiling must be positive"):
            SharedBudget(ceiling_gbp=Decimal("0"))
        with pytest.raises(ValueError, match="ceiling must be positive"):
            SharedBudget(ceiling_gbp=Decimal("-1.00"))

    def test_reserve_passes_under_ceiling(self) -> None:
        budget = SharedBudget(ceiling_gbp=Decimal("5.00"))
        budget.reserve(Decimal("2.00"))  # Should not raise

    def test_reserve_raises_over_ceiling(self) -> None:
        budget = SharedBudget(ceiling_gbp=Decimal("5.00"))
        with pytest.raises(BudgetExceededError):
            budget.reserve(Decimal("6.00"))

    def test_record_updates_spent(self) -> None:
        budget = SharedBudget(ceiling_gbp=Decimal("10.00"))
        call = LLMCall(
            provider="claude",
            model="claude-haiku-4-5",
            stage="classify",
            input_tokens=100,
            output_tokens=50,
            cost_gbp=Decimal("0.50"),
        )
        budget.record(call)
        assert budget.spent_gbp == Decimal("0.50")
        assert budget.remaining_gbp == Decimal("9.50")

    def test_multiple_records_accumulate(self) -> None:
        budget = SharedBudget(ceiling_gbp=Decimal("10.00"))
        for i in range(5):
            call = LLMCall(
                provider="openai",
                model="gpt-4o-mini",
                stage="extract",
                input_tokens=100,
                output_tokens=50,
                cost_gbp=Decimal("1.00"),
            )
            budget.record(call)
        assert budget.spent_gbp == Decimal("5.00")
        assert budget.remaining_gbp == Decimal("5.00")

    def test_stats_returns_summary(self) -> None:
        budget = SharedBudget(ceiling_gbp=Decimal("10.00"))
        budget.record(
            LLMCall(
                provider="claude",
                model="claude-haiku-4-5",
                stage="classify",
                input_tokens=100,
                output_tokens=50,
                cost_gbp=Decimal("0.50"),
            )
        )
        budget.record(
            LLMCall(
                provider="openai",
                model="gpt-4o-mini",
                stage="extract",
                input_tokens=200,
                output_tokens=100,
                cost_gbp=Decimal("0.25"),
            )
        )
        stats = budget.stats()
        assert stats["call_count"] == 2
        assert stats["by_provider"]["claude"] == 1
        assert stats["by_provider"]["openai"] == 1
        assert stats["total_input_tokens"] == 300
        assert stats["total_output_tokens"] == 150


class TestSharedBudgetThreadSafety:
    """Thread safety tests for SharedBudget under concurrent access."""

    def test_concurrent_records_no_data_loss(self) -> None:
        """Multiple threads recording simultaneously should not lose data."""
        budget = SharedBudget(ceiling_gbp=Decimal("1000.00"))
        num_workers = 10
        calls_per_worker = 100
        cost_per_call = Decimal("0.01")

        def worker() -> int:
            recorded = 0
            for _ in range(calls_per_worker):
                call = LLMCall(
                    provider="claude",
                    model="test",
                    stage="classify",
                    input_tokens=10,
                    output_tokens=5,
                    cost_gbp=cost_per_call,
                )
                budget.record(call)
                recorded += 1
            return recorded

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker) for _ in range(num_workers)]
            total_recorded = sum(f.result() for f in as_completed(futures))

        expected_total = num_workers * calls_per_worker
        expected_cost = expected_total * cost_per_call

        assert total_recorded == expected_total
        assert budget.spent_gbp == expected_cost
        assert budget.stats()["call_count"] == expected_total

    def test_concurrent_reserve_respects_ceiling(self) -> None:
        """Concurrent reserves should not allow total to exceed ceiling."""
        budget = SharedBudget(ceiling_gbp=Decimal("1.00"))
        num_workers = 20
        cost_per_reserve = Decimal("0.10")
        successful_reserves = []
        lock = threading.Lock()

        def worker() -> bool:
            try:
                budget.reserve(cost_per_reserve)
                with lock:
                    successful_reserves.append(True)
                # Simulate work then record
                call = LLMCall(
                    provider="openai",
                    model="test",
                    stage="extract",
                    input_tokens=10,
                    output_tokens=5,
                    cost_gbp=cost_per_reserve,
                )
                budget.record(call)
                return True
            except BudgetExceededError:
                return False

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker) for _ in range(num_workers)]
            results = [f.result() for f in as_completed(futures)]

        # At most 10 should succeed (ceiling=1.00, cost=0.10)
        # Some may fail due to race between reserve and record
        successful = sum(1 for r in results if r)
        assert successful <= 10
        assert budget.spent_gbp <= Decimal("1.00")
