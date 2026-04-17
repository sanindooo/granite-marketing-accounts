"""Claude client: budget ledger, cost estimation, construction guards."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from execution.shared import claude_client as cc
from execution.shared import secrets
from execution.shared.claude_client import (
    HAIKU,
    SONNET,
    ClaudeBudget,
    ClaudeCall,
    ClaudeClient,
    ClaudeUsage,
    estimate_cost_gbp,
)
from execution.shared.errors import BudgetExceededError, ConfigError


class TestEstimateCostGbp:
    def test_haiku_no_cache(self) -> None:
        usage = ClaudeUsage(
            input_tokens=1_000_000,
            output_tokens=200_000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        # 1M @ $1 in + 0.2M @ $5 out = $2.00; x 0.79 -> £1.58
        cost = estimate_cost_gbp(HAIKU, usage, "5m")
        assert cost == Decimal("1.5800")

    def test_cache_read_is_ten_percent(self) -> None:
        # 1M cache reads on Haiku = $1 x 0.10 = $0.10 -> £0.079
        usage = ClaudeUsage(
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=1_000_000,
        )
        cost = estimate_cost_gbp(HAIKU, usage, "5m")
        assert cost == Decimal("0.0790")

    def test_cache_write_5m_vs_1h(self) -> None:
        usage = ClaudeUsage(
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=1_000_000,
            cache_read_input_tokens=0,
        )
        five = estimate_cost_gbp(HAIKU, usage, "5m")
        one = estimate_cost_gbp(HAIKU, usage, "1h")
        # 1h write multiplier is 2x; 5m is 1.25x; 1h is exactly 1.6x the 5m cost
        assert one == (five * Decimal("1.6")).quantize(Decimal("0.0001"))

    def test_sonnet_pricier_than_haiku(self) -> None:
        usage = ClaudeUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        assert estimate_cost_gbp(SONNET, usage, "5m") > estimate_cost_gbp(
            HAIKU, usage, "5m"
        )


class TestClaudeBudget:
    def test_reserve_under_ceiling_passes(self) -> None:
        b = ClaudeBudget(ceiling_gbp=Decimal("1.00"))
        b.reserve(Decimal("0.50"))

    def test_reserve_over_ceiling_raises(self) -> None:
        b = ClaudeBudget(ceiling_gbp=Decimal("1.00"))
        with pytest.raises(BudgetExceededError):
            b.reserve(Decimal("1.50"))

    def test_reserve_accumulates_spent(self) -> None:
        b = ClaudeBudget(ceiling_gbp=Decimal("1.00"))
        b.record(_call(Decimal("0.40")))
        b.record(_call(Decimal("0.40")))
        # Third reservation that would push us over must raise
        with pytest.raises(BudgetExceededError):
            b.reserve(Decimal("0.40"))

    def test_non_positive_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError):
            ClaudeBudget(ceiling_gbp=Decimal("0"))
        with pytest.raises(ValueError):
            ClaudeBudget(ceiling_gbp=Decimal("-1"))

    def test_stats_shape_is_stable(self) -> None:
        b = ClaudeBudget(ceiling_gbp=Decimal("1.00"))
        b.record(_call(Decimal("0.10"), model=HAIKU))
        b.record(_call(Decimal("0.20"), model=SONNET))
        stats = b.stats()
        assert stats["call_count"] == 2
        assert stats["by_model"] == {HAIKU: 1, SONNET: 1}
        assert stats["spent_gbp"] == "0.3000"

    def test_remaining(self) -> None:
        b = ClaudeBudget(ceiling_gbp=Decimal("2.00"))
        b.record(_call(Decimal("0.75")))
        assert b.remaining_gbp == Decimal("1.25")


class TestClaudeClient:
    def test_refuses_real_client_under_mock_mode(
        self, mock_secrets: None
    ) -> None:
        del mock_secrets
        with pytest.raises(ConfigError):
            ClaudeClient(budget_gbp=Decimal("0.05"))

    def test_accepts_injected_client_under_mock_mode(
        self, mock_secrets: None
    ) -> None:
        del mock_secrets
        fake = _FakeAnthropic(
            input_tokens=10,
            output_tokens=4,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        client = ClaudeClient(budget_gbp=Decimal("0.05"), client=fake)
        call = client.smoke()
        assert call.model == HAIKU
        assert call.stage == "smoke"
        assert call.usage.input_tokens == 10
        assert call.usage.output_tokens == 4
        assert client.budget.calls == [call]

    def test_uses_keyring_api_key_when_not_mocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, str] = {}

        def fake_anthropic_ctor(*, api_key: str) -> _FakeAnthropic:
            recorded["api_key"] = api_key
            return _FakeAnthropic(
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

        monkeypatch.setattr(cc.anthropic, "Anthropic", fake_anthropic_ctor)
        monkeypatch.setattr(
            cc.secrets, "is_mock", lambda: False
        )
        monkeypatch.setattr(
            cc.secrets,
            "require",
            lambda namespace, key: "sk-test-123",
        )
        client = ClaudeClient(budget_gbp=Decimal("0.05"))
        del client  # constructed; that's the assertion
        assert recorded["api_key"] == "sk-test-123"

    def test_explicit_api_key_overrides_keyring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, str] = {}

        def fake_anthropic_ctor(*, api_key: str) -> _FakeAnthropic:
            recorded["api_key"] = api_key
            return _FakeAnthropic(
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

        monkeypatch.setattr(cc.anthropic, "Anthropic", fake_anthropic_ctor)
        monkeypatch.setattr(cc.secrets, "is_mock", lambda: False)
        # secrets.require would be a bug path if called
        called: list[str] = []
        monkeypatch.setattr(
            cc.secrets,
            "require",
            lambda *_a, **_kw: called.append("x") or "keyring-value",
        )
        ClaudeClient(budget_gbp=Decimal("0.05"), api_key="sk-direct")
        assert recorded["api_key"] == "sk-direct"
        assert called == []


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


@dataclass
class _FakeMessage:
    usage: _FakeUsage


class _FakeMessages:
    def __init__(self, usage: _FakeUsage) -> None:
        self._usage = usage

    def create(self, **_kwargs: Any) -> _FakeMessage:
        return _FakeMessage(usage=self._usage)


class _FakeAnthropic:
    def __init__(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
    ) -> None:
        self.messages = _FakeMessages(
            _FakeUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
            )
        )


def _call(cost: Decimal, *, model: str = HAIKU) -> ClaudeCall:
    return ClaudeCall(
        model=model,  # type: ignore[arg-type]
        stage="smoke",
        usage=ClaudeUsage(
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        cost_gbp=cost,
        ttl="5m",
    )


def test_secrets_module_reference_stable() -> None:
    """Importing secrets via two paths yields the same module (monkeypatch target)."""
    assert cc.secrets is secrets
