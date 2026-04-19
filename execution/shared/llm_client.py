"""LLM client protocol for provider-agnostic invoice processing.

This module defines the LLMClient protocol that both ClaudeClient and
OpenAIClient implement, allowing the invoice processor to work with
either provider.

The protocol is designed to minimize changes to existing code - it uses
the same text-based response format that the classifier and extractor
expect, while abstracting away provider-specific details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from execution.shared.budget import LLMCall, SharedBudget, Stage
    from execution.shared.prompts import LoadedPrompt


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM API clients (Claude, OpenAI, etc.).

    Implementations must provide:
    - complete(): Send a prompt and return text response + call record
    - budget: Access to the shared budget tracker

    The protocol is designed for the invoice processing pipeline where:
    1. System prompts are loaded from files (LoadedPrompt)
    2. User content contains untrusted email/invoice data
    3. Responses are parsed as JSON by the caller
    4. Budget is tracked across all calls
    """

    @property
    def budget(self) -> SharedBudget:
        """Access the shared budget tracker."""
        ...

    def complete(
        self,
        *,
        loaded_prompt: LoadedPrompt,
        user_content: str,
        max_tokens: int,
        stage: Stage,
    ) -> tuple[str, LLMCall]:
        """Send a completion request and return text response + call record.

        Args:
            loaded_prompt: The system prompt loaded from a file
            user_content: The user message (typically untrusted email/invoice data)
            max_tokens: Maximum tokens for the response
            stage: The pipeline stage ("classify" | "extract" | "smoke")

        Returns:
            Tuple of (response_text, call_record) where:
            - response_text: Raw text from the model (typically JSON)
            - call_record: LLMCall with token usage and cost

        Raises:
            BudgetExceededError: If the call would exceed the budget ceiling
            RateLimitedError: If the API returns a rate limit error
            ConfigError: If API credentials are missing or invalid
        """
        ...


__all__ = [
    "LLMClient",
]
