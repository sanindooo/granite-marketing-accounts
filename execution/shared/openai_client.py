"""OpenAI API client with budget tracking.

Implements the LLMClient protocol for use in the invoice processing pipeline.
Uses GPT-4o-mini by default as a cost-effective alternative to Claude Haiku.

Pricing (April 2026):
- GPT-4o-mini: $0.15 / $0.60 per MTok (input / output)
- GPT-4o:      $2.50 / $10.00 per MTok

This client is designed to be a drop-in replacement for ClaudeClient when
the user selects `--model openai` in the CLI.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

import openai
from openai import OpenAI

from execution.shared import secrets
from execution.shared.budget import LLMCall, SharedBudget, Stage
from execution.shared.errors import ConfigError, RateLimitedError

if TYPE_CHECKING:
    from execution.shared.prompts import LoadedPrompt

Model = Literal["gpt-4o-mini", "gpt-4o"]

GPT4O_MINI: Final[Model] = "gpt-4o-mini"
GPT4O: Final[Model] = "gpt-4o"

# Pricing per million tokens (April 2026)
_PRICING: Final[dict[Model, dict[str, Decimal]]] = {
    GPT4O_MINI: {"input": Decimal("0.15"), "output": Decimal("0.60")},
    GPT4O: {"input": Decimal("2.50"), "output": Decimal("10.00")},
}
_PER_MTOK: Final[Decimal] = Decimal("1000000")
_USD_TO_GBP: Final[Decimal] = Decimal("0.79")
_FOUR_PLACES: Final[Decimal] = Decimal("0.0001")


def estimate_cost_gbp(model: Model, input_tokens: int, output_tokens: int) -> Decimal:
    """Estimate the GBP cost of a single call from token counts."""
    pricing = _PRICING[model]
    input_usd = Decimal(input_tokens) * pricing["input"] / _PER_MTOK
    output_usd = Decimal(output_tokens) * pricing["output"] / _PER_MTOK
    return ((input_usd + output_usd) * _USD_TO_GBP).quantize(_FOUR_PLACES)


class OpenAIClient:
    """OpenAI API client implementing the LLMClient protocol.

    Uses the same SharedBudget as ClaudeClient for unified cost tracking
    across providers.
    """

    def __init__(
        self,
        budget: SharedBudget,
        *,
        model: Model = GPT4O_MINI,
        max_retries: int = 5,
        api_key: str | None = None,
    ) -> None:
        self._budget = budget
        self._model = model
        if secrets.is_mock():
            raise ConfigError(
                "OpenAIClient requires an explicit api_key= in mock mode",
                source="openai",
            )
        key = api_key or secrets.require("openai", "api_key")
        self._client = OpenAI(api_key=key, max_retries=max_retries)

    @property
    def budget(self) -> SharedBudget:
        return self._budget

    def complete(
        self,
        *,
        loaded_prompt: LoadedPrompt,
        user_content: str,
        max_tokens: int,
        stage: Stage,
    ) -> tuple[str, LLMCall]:
        """Send a completion request and return text response + call record.

        Uses OpenAI's chat completions API with JSON mode enabled.
        The system prompt from loaded_prompt.text is sent as the system message.
        """
        # Reserve budget before making the call
        estimated_cost = self._reserve_estimate(
            len(loaded_prompt.text) + len(user_content),
            max_tokens,
        )
        self._budget.reserve(estimated_cost)

        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": loaded_prompt.text},
                    {"role": "user", "content": user_content},
                ],
            )
        except openai.RateLimitError as err:
            raise RateLimitedError(
                f"OpenAI rate limit: {err}",
                source="openai",
            ) from err

        # Extract response text
        text = completion.choices[0].message.content or ""

        # Calculate actual cost
        usage = completion.usage
        if usage is None:
            input_tokens = 0
            output_tokens = 0
        else:
            input_tokens = usage.prompt_tokens
            output_tokens = usage.completion_tokens

        actual_cost = estimate_cost_gbp(self._model, input_tokens, output_tokens)

        call = LLMCall(
            provider="openai",
            model=self._model,
            stage=stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_gbp=actual_cost,
        )
        self._budget.record(call)

        return text, call

    def _reserve_estimate(self, input_chars: int, max_output_tokens: int) -> Decimal:
        """Pessimistic cost estimate for budget pre-check.

        Assumes 4 chars per token (conservative) and full max_output_tokens.
        """
        estimated_input_tokens = input_chars // 4
        return estimate_cost_gbp(self._model, estimated_input_tokens, max_output_tokens)


__all__ = [
    "GPT4O",
    "GPT4O_MINI",
    "Model",
    "OpenAIClient",
    "estimate_cost_gbp",
]
