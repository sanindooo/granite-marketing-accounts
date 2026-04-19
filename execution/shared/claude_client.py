"""Anthropic Messages API wrapper.

Phase 1B ships the skeleton: API-key resolution via :mod:`secrets`, model
constants (Haiku 4.5 default, Sonnet 4.6 escalation), a per-run budget ledger
with cost estimation, and a ``smoke()`` call that proves end-to-end
connectivity. The full ``classify`` / ``extract`` surface lands in Phase 2
with the prompt files under ``execution/invoice/prompts/``.

Pricing figures (April 2026) come from the plan appendix:

- Haiku 4.5:  $1  / $5  per MTok (input / output)
- Sonnet 4.6: $3  / $15 per MTok
- Cache-write multiplier: 1.25x for 5m TTL, 2x for 1h TTL
- Cache-read multiplier:  0.10x

Costs are reported in GBP via a fixed USD→GBP heuristic (0.79) for budgeting.
Real run-close reconciliation uses :mod:`shared.fx` once invoices land.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

import anthropic

from execution.shared import secrets
from execution.shared.budget import LLMCall as GenericLLMCall
from execution.shared.budget import SharedBudget, Stage as GenericStage
from execution.shared.errors import BudgetExceededError, ConfigError

if TYPE_CHECKING:  # pragma: no cover — anthropic ships types lazily
    from anthropic.types import Message

    from execution.shared.prompts import LoadedPrompt

Model = Literal["claude-haiku-4-5", "claude-sonnet-4-6"]
CacheTTL = Literal["5m", "1h"]
Stage = Literal["classify", "extract", "smoke"]

HAIKU: Final[Model] = "claude-haiku-4-5"
SONNET: Final[Model] = "claude-sonnet-4-6"

# Haiku 4.5's prompt cache silently writes zero tokens below this threshold.
# The classifier + extractor system prompts are padded to clear it.
MIN_CACHEABLE_PREFIX_TOKENS: Final[int] = 4096

_PRICING: Final[dict[Model, dict[str, Decimal]]] = {
    HAIKU: {"input": Decimal("1.00"), "output": Decimal("5.00")},
    SONNET: {"input": Decimal("3.00"), "output": Decimal("15.00")},
}
_CACHE_WRITE_MULT: Final[dict[CacheTTL, Decimal]] = {
    "5m": Decimal("1.25"),
    "1h": Decimal("2.00"),
}
_CACHE_READ_MULT: Final[Decimal] = Decimal("0.10")
_PER_MTOK: Final[Decimal] = Decimal("1000000")
# Budgeting-only heuristic. Run-close recon uses the real ECB rate.
_USD_TO_GBP: Final[Decimal] = Decimal("0.79")
_FOUR_PLACES: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ClaudeUsage:
    """Normalised token accounting for one Messages call."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @classmethod
    def from_message(cls, msg: Message) -> ClaudeUsage:
        u = msg.usage
        return cls(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        )


@dataclass(frozen=True, slots=True)
class ClaudeCall:
    """One recorded invocation against the Messages API."""

    model: Model
    stage: Stage
    usage: ClaudeUsage
    cost_gbp: Decimal
    ttl: CacheTTL


def estimate_cost_gbp(model: Model, usage: ClaudeUsage, ttl: CacheTTL) -> Decimal:
    """Estimate the GBP cost of a single call from its usage object."""
    pricing = _PRICING[model]
    write_mult = _CACHE_WRITE_MULT[ttl]
    input_usd = (
        Decimal(usage.input_tokens) * pricing["input"]
        + Decimal(usage.cache_creation_input_tokens) * pricing["input"] * write_mult
        + Decimal(usage.cache_read_input_tokens) * pricing["input"] * _CACHE_READ_MULT
    ) / _PER_MTOK
    output_usd = Decimal(usage.output_tokens) * pricing["output"] / _PER_MTOK
    return ((input_usd + output_usd) * _USD_TO_GBP).quantize(_FOUR_PLACES)


class ClaudeBudget:
    """Per-run Claude spend ledger with a hard ceiling."""

    def __init__(self, ceiling_gbp: Decimal) -> None:
        if ceiling_gbp <= 0:
            raise ValueError(f"ceiling must be positive, got {ceiling_gbp}")
        self.ceiling_gbp: Decimal = ceiling_gbp
        self.spent_gbp: Decimal = Decimal("0.0000")
        self.calls: list[ClaudeCall] = []

    def reserve(self, estimated_gbp: Decimal) -> None:
        """Raise ``BudgetExceededError`` if ``estimated_gbp`` would exceed ceiling."""
        if self.spent_gbp + estimated_gbp > self.ceiling_gbp:
            raise BudgetExceededError(
                f"Claude budget of £{self.ceiling_gbp} would be exceeded "
                f"(spent £{self.spent_gbp}, requested £{estimated_gbp})",
                source="claude",
                details={
                    "ceiling_gbp": format(self.ceiling_gbp, "f"),
                    "spent_gbp": format(self.spent_gbp, "f"),
                    "requested_gbp": format(estimated_gbp, "f"),
                },
            )

    def record(self, call: ClaudeCall) -> None:
        self.calls.append(call)
        self.spent_gbp += call.cost_gbp

    @property
    def remaining_gbp(self) -> Decimal:
        return self.ceiling_gbp - self.spent_gbp

    def stats(self) -> dict[str, object]:
        """Summary safe to embed into ``runs.stats_json``."""
        return {
            "call_count": len(self.calls),
            "spent_gbp": format(self.spent_gbp, "f"),
            "ceiling_gbp": format(self.ceiling_gbp, "f"),
            "by_model": {
                HAIKU: sum(1 for c in self.calls if c.model == HAIKU),
                SONNET: sum(1 for c in self.calls if c.model == SONNET),
            },
            "cache_read_tokens": sum(c.usage.cache_read_input_tokens for c in self.calls),
            "cache_creation_tokens": sum(
                c.usage.cache_creation_input_tokens for c in self.calls
            ),
            "output_tokens": sum(c.usage.output_tokens for c in self.calls),
        }


class ClaudeClient:
    """Thin wrapper over :class:`anthropic.Anthropic`.

    Implements the LLMClient protocol for use in the invoice pipeline.
    Supports both standalone usage (with internal ClaudeBudget) and
    parallel processing (with shared SharedBudget).
    """

    def __init__(
        self,
        *,
        ttl: CacheTTL = "5m",
        budget_gbp: Decimal = Decimal("2.00"),
        shared_budget: SharedBudget | None = None,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.ttl: CacheTTL = ttl
        self._shared_budget = shared_budget
        self._claude_budget: ClaudeBudget | None = None
        if shared_budget is None:
            self._claude_budget = ClaudeBudget(ceiling_gbp=budget_gbp)
        if client is not None:
            self._client = client
            return
        if secrets.is_mock():
            raise ConfigError(
                "ClaudeClient requires an explicit client= in mock mode; "
                "refusing to construct a real Anthropic() against the API.",
                source="claude",
            )
        key = api_key or secrets.require("claude", "api_key")
        self._client = anthropic.Anthropic(api_key=key)

    @property
    def budget(self) -> ClaudeBudget | SharedBudget:
        """Access the budget tracker (ClaudeBudget or SharedBudget)."""
        if self._shared_budget is not None:
            return self._shared_budget
        assert self._claude_budget is not None
        return self._claude_budget

    def smoke(self) -> ClaudeCall:
        """Send the cheapest possible Haiku call and record it on the budget.

        Used by ``granite ops smoke-claude`` to validate keyring wiring,
        SDK compatibility, and network reachability in one round-trip.
        """
        msg = self._client.messages.create(
            model=HAIKU,
            max_tokens=16,
            system="Reply with a single word: pong.",
            messages=[{"role": "user", "content": "ping"}],
        )
        usage = ClaudeUsage.from_message(msg)
        cost = estimate_cost_gbp(HAIKU, usage, self.ttl)
        call = ClaudeCall(
            model=HAIKU, stage="smoke", usage=usage, cost_gbp=cost, ttl=self.ttl
        )
        self._record_call(call)
        return call

    def call_with_cached_prompt(
        self,
        *,
        loaded_prompt: LoadedPrompt,
        user_content: str,
        max_tokens: int,
        stage: Stage,
        model: Model = HAIKU,
        extra_system_suffix: str | None = None,
    ) -> tuple[str, ClaudeCall]:
        """Send a Messages call with ``loaded_prompt.text`` as a cached system block.

        ``extra_system_suffix`` is appended as a *non-cached* second system
        block — used by the extractor to inject per-invoice hints (e.g.
        "This email's sender domain is zoom.us") without busting the cache.

        Returns the raw response text and the :class:`ClaudeCall` record.
        ``tools=[]`` is explicit: we never let Claude drive external calls
        on adversary-controllable content (see plan § Prompt-injection
        defense).
        """
        system: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": loaded_prompt.text,
                "cache_control": {"type": "ephemeral", "ttl": self.ttl},
            }
        ]
        if extra_system_suffix:
            system.append({"type": "text", "text": extra_system_suffix})

        self._reserve(self._reserve_estimate(max_tokens, model))
        msg = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,  # type: ignore[arg-type]
            messages=[{"role": "user", "content": user_content}],
            tools=[],
        )
        usage = ClaudeUsage.from_message(msg)
        cost = estimate_cost_gbp(model, usage, self.ttl)
        call = ClaudeCall(
            model=model, stage=stage, usage=usage, cost_gbp=cost, ttl=self.ttl
        )
        self._record_call(call)
        return _extract_text(msg), call

    def complete(
        self,
        *,
        loaded_prompt: LoadedPrompt,
        user_content: str,
        max_tokens: int,
        stage: GenericStage,
    ) -> tuple[str, GenericLLMCall]:
        """LLMClient protocol: Send completion and return text + generic call record.

        This is a thin wrapper around call_with_cached_prompt that returns
        a provider-agnostic LLMCall instead of a Claude-specific ClaudeCall.
        """
        text, claude_call = self.call_with_cached_prompt(
            loaded_prompt=loaded_prompt,
            user_content=user_content,
            max_tokens=max_tokens,
            stage=stage,
            model=HAIKU,
        )
        generic_call = GenericLLMCall(
            provider="claude",
            model=claude_call.model,
            stage=stage,
            input_tokens=claude_call.usage.input_tokens,
            output_tokens=claude_call.usage.output_tokens,
            cost_gbp=claude_call.cost_gbp,
        )
        return text, generic_call

    def _reserve(self, estimated_gbp: Decimal) -> None:
        """Reserve budget using either ClaudeBudget or SharedBudget."""
        if self._shared_budget is not None:
            self._shared_budget.reserve(estimated_gbp)
        else:
            assert self._claude_budget is not None
            self._claude_budget.reserve(estimated_gbp)

    def _record_call(self, call: ClaudeCall) -> None:
        """Record call using either ClaudeBudget or SharedBudget."""
        if self._shared_budget is not None:
            generic_call = GenericLLMCall(
                provider="claude",
                model=call.model,
                stage=call.stage,
                input_tokens=call.usage.input_tokens,
                output_tokens=call.usage.output_tokens,
                cost_gbp=call.cost_gbp,
            )
            self._shared_budget.record(generic_call)
        else:
            assert self._claude_budget is not None
            self._claude_budget.record(call)

    @staticmethod
    def _reserve_estimate(max_tokens: int, model: Model) -> Decimal:
        """A rough per-call ceiling used for budget pre-check.

        Cost upper-bounded by assuming a full cache-write on input (worst-case
        5m TTL = 1.25x) and the full ``max_tokens`` on output. Keeps the
        reservation pessimistic so ``BudgetExceededError`` fires *before* the
        call, not after.
        """
        pricing = _PRICING[model]
        # Pessimistic: 8k input tokens at cache-write + max_tokens output.
        input_usd = Decimal(8192) * pricing["input"] * Decimal("1.25") / _PER_MTOK
        output_usd = Decimal(max_tokens) * pricing["output"] / _PER_MTOK
        return ((input_usd + output_usd) * _USD_TO_GBP).quantize(_FOUR_PLACES)


def _extract_text(msg: Any) -> str:
    """Collapse the ``content`` list of a Messages response into plain text."""
    parts: list[str] = []
    for block in getattr(msg, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts)


__all__ = [
    "HAIKU",
    "MIN_CACHEABLE_PREFIX_TOKENS",
    "SONNET",
    "CacheTTL",
    "ClaudeBudget",
    "ClaudeCall",
    "ClaudeClient",
    "ClaudeUsage",
    "Model",
    "Stage",
    "estimate_cost_gbp",
]
