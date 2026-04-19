"""Tests for OpenAI client."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from execution.shared.budget import SharedBudget
from execution.shared.errors import ConfigError


class TestEstimateCostGbp:
    """Test OpenAI cost estimation."""

    def test_gpt4o_mini_pricing(self) -> None:
        from execution.shared.openai_client import estimate_cost_gbp

        # 1M input + 1M output at GPT-4o-mini pricing
        # Input: $0.15/MTok, Output: $0.60/MTok
        # Total USD: $0.75, GBP: $0.75 * 0.79 = £0.5925
        cost = estimate_cost_gbp("gpt-4o-mini", 1_000_000, 1_000_000)
        assert cost == Decimal("0.5925")

    def test_gpt4o_pricing(self) -> None:
        from execution.shared.openai_client import estimate_cost_gbp

        # 1M input + 1M output at GPT-4o pricing
        # Input: $2.50/MTok, Output: $10.00/MTok
        # Total USD: $12.50, GBP: $12.50 * 0.79 = £9.875
        cost = estimate_cost_gbp("gpt-4o", 1_000_000, 1_000_000)
        assert cost == Decimal("9.8750")

    def test_small_call_cost(self) -> None:
        from execution.shared.openai_client import estimate_cost_gbp

        # 1000 input + 500 output at GPT-4o-mini
        cost = estimate_cost_gbp("gpt-4o-mini", 1000, 500)
        # Very small cost
        assert cost < Decimal("0.001")
        assert cost > Decimal("0")


class TestOpenAIClient:
    """Test OpenAI client initialization and protocol compliance."""

    def test_refuses_construction_in_mock_mode(self) -> None:
        from execution.shared.openai_client import OpenAIClient

        with patch("execution.shared.openai_client.secrets.is_mock", return_value=True):
            budget = SharedBudget(ceiling_gbp=Decimal("5.00"))
            with pytest.raises(ConfigError, match="mock mode"):
                OpenAIClient(budget=budget)

    def test_constructs_with_explicit_api_key(self) -> None:
        from execution.shared.openai_client import OpenAIClient

        with patch("execution.shared.openai_client.secrets.is_mock", return_value=False):
            with patch("execution.shared.openai_client.OpenAI") as mock_openai:
                budget = SharedBudget(ceiling_gbp=Decimal("5.00"))
                client = OpenAIClient(budget=budget, api_key="test-key")
                mock_openai.assert_called_once()
                assert client.budget is budget

    def test_budget_property_returns_shared_budget(self) -> None:
        from execution.shared.openai_client import OpenAIClient

        with patch("execution.shared.openai_client.secrets.is_mock", return_value=False):
            with patch("execution.shared.openai_client.OpenAI"):
                budget = SharedBudget(ceiling_gbp=Decimal("5.00"))
                client = OpenAIClient(budget=budget, api_key="test-key")
                assert client.budget is budget


class TestOpenAIClientComplete:
    """Test the complete() method."""

    def test_complete_returns_text_and_call(self) -> None:
        from execution.shared.openai_client import OpenAIClient
        from execution.shared.prompts import LoadedPrompt

        with patch("execution.shared.openai_client.secrets.is_mock", return_value=False):
            with patch("execution.shared.openai_client.OpenAI") as mock_openai_cls:
                # Mock the completion response
                mock_message = MagicMock()
                mock_message.content = '{"classification": "invoice"}'
                mock_choice = MagicMock()
                mock_choice.message = mock_message
                mock_usage = MagicMock()
                mock_usage.prompt_tokens = 10000
                mock_usage.completion_tokens = 5000
                mock_completion = MagicMock()
                mock_completion.choices = [mock_choice]
                mock_completion.usage = mock_usage

                mock_client = MagicMock()
                mock_client.chat.completions.create.return_value = mock_completion
                mock_openai_cls.return_value = mock_client

                budget = SharedBudget(ceiling_gbp=Decimal("5.00"))
                client = OpenAIClient(budget=budget, api_key="test-key")

                prompt = LoadedPrompt(
                    name="test",
                    model_id="gpt-4o-mini",
                    text="You are a classifier.",
                    schema={},
                    version="abc123",
                    estimated_tokens=100,
                )

                text, call = client.complete(
                    loaded_prompt=prompt,
                    user_content="Test email content",
                    max_tokens=512,
                    stage="classify",
                )

                assert text == '{"classification": "invoice"}'
                assert call.provider == "openai"
                assert call.model == "gpt-4o-mini"
                assert call.stage == "classify"
                assert call.input_tokens == 10000
                assert call.output_tokens == 5000
                assert budget.spent_gbp > Decimal("0")

    def test_complete_reserves_budget_before_call(self) -> None:
        from execution.shared.openai_client import OpenAIClient
        from execution.shared.budget import SharedBudget
        from execution.shared.errors import BudgetExceededError
        from execution.shared.prompts import LoadedPrompt

        with patch("execution.shared.openai_client.secrets.is_mock", return_value=False):
            with patch("execution.shared.openai_client.OpenAI") as mock_openai_cls:
                mock_client = MagicMock()
                mock_openai_cls.return_value = mock_client

                # Very low budget that should be exceeded
                budget = SharedBudget(ceiling_gbp=Decimal("0.0001"))
                client = OpenAIClient(budget=budget, api_key="test-key")

                prompt = LoadedPrompt(
                    name="test",
                    model_id="gpt-4o-mini",
                    text="You are a classifier." * 100,
                    schema={},
                    version="abc123",
                    estimated_tokens=1000,
                )

                with pytest.raises(BudgetExceededError):
                    client.complete(
                        loaded_prompt=prompt,
                        user_content="Test email content",
                        max_tokens=2048,
                        stage="classify",
                    )

                # The API should NOT have been called
                mock_client.chat.completions.create.assert_not_called()
