"""Prompt + JSON-schema loader for the Claude stages.

The classifier and extractor system prompts live as Markdown files under
``execution/invoice/prompts/`` alongside their strict JSON schemas. This
module reads them from disk, derives a stable content-hash version so
``CLASSIFIER_VERSION`` / ``EXTRACTOR_VERSION`` never needs manual bumping,
and asserts each prompt clears Haiku 4.5's 4,096-token cache minimum.

Why a stable derived version: the row state machine in ``reconcile/state.py``
re-evaluates ``new / auto_matched / suggested / unmatched`` rows when the
classifier or matcher version changes. Forgetting to bump a constant after a
prompt edit would silently ship an inconsistent corpus. Deriving the version
from ``sha256(prompt_bytes + schema_bytes + model_id + weights_tuple)``
removes the manual step and makes the invariant "version is a pure function
of inputs" testable.

Token estimation is intentionally pessimistic (4 chars per token) so the
"estimate passes" → "real count passes" implication holds — if the cheap
character-based lower bound clears the cache minimum, the real Anthropic
tokenizer will too.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from execution.shared.claude_client import MIN_CACHEABLE_PREFIX_TOKENS

# Conservative lower-bound chars/token for English Markdown. Anthropic's
# practical ratio is closer to 3.5 — using 4 makes ``estimate_tokens`` an
# under-estimate, so "estimate ≥ 4,096" implies "real count ≥ 4,096".
_CHARS_PER_TOKEN: Final[int] = 4

PROMPTS_DIR: Final[Path] = (
    Path(__file__).resolve().parents[1] / "invoice" / "prompts"
)

# Weights that feed into the derived version hash. Bumping these triggers a
# silent reprocess across ``new/auto_matched/suggested/unmatched`` rows —
# that's the intent. Keep the tuple ordered and exhaustive.
CLASSIFIER_WEIGHTS: Final[tuple[tuple[str, float | int | str], ...]] = (
    ("confidence_floor_new_vendor", 0.85),
    ("confidence_floor_known_vendor", 0.70),
    ("schema_version", 1),
)
EXTRACTOR_WEIGHTS: Final[tuple[tuple[str, float | int | str], ...]] = (
    ("overall_confidence_floor", 0.75),
    ("critical_field_floor", 0.70),
    ("arithmetic_tolerance_gbp", "0.02"),
    ("date_window_days", 90),
    ("schema_version", 1),
)


@dataclass(frozen=True, slots=True)
class LoadedPrompt:
    """A system prompt + its strict JSON schema + a derived version hash."""

    name: str
    model_id: str
    text: str
    schema: dict[str, Any]
    version: str
    estimated_tokens: int

    def text_bytes(self) -> bytes:
        return self.text.encode("utf-8")

    def schema_json(self) -> str:
        return json.dumps(self.schema, sort_keys=True, separators=(",", ":"))


def estimate_tokens(text: str) -> int:
    """Conservative char-based token estimate (lower bound)."""
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def derive_version(
    *, text: str, schema: dict[str, Any], model_id: str, weights: tuple[tuple[str, Any], ...]
) -> str:
    """Return a stable 8-char hex tag identifying (prompt, schema, model, weights)."""
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    h.update(b"\x00")
    h.update(model_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(
        json.dumps(list(weights), sort_keys=False, separators=(",", ":")).encode("utf-8")
    )
    return h.hexdigest()[:8]


def load_prompt(
    name: str,
    *,
    model_id: str,
    weights: tuple[tuple[str, Any], ...],
    prompts_dir: Path | None = None,
    min_tokens: int = MIN_CACHEABLE_PREFIX_TOKENS,
) -> LoadedPrompt:
    """Load a prompt Markdown + strict JSON schema from disk.

    Raises ``FileNotFoundError`` if either file is missing and
    ``AssertionError`` if the prompt is too short to clear Haiku 4.5's
    cache minimum.
    """
    root = prompts_dir or PROMPTS_DIR
    md_path = root / f"{name}.md"
    schema_path = root / f"{name}.schema.json"
    text = md_path.read_text(encoding="utf-8")
    schema_text = schema_path.read_text(encoding="utf-8")
    schema = json.loads(schema_text)
    tokens = estimate_tokens(text)
    if tokens < min_tokens:
        raise AssertionError(
            f"prompt {name!r} estimates at {tokens} tokens (min {min_tokens}). "
            "Pad with genuine documentation (taxonomy glossary, few-shot gallery, "
            "vendor-hint table) — do not pad with filler."
        )
    version = derive_version(
        text=text, schema=schema, model_id=model_id, weights=weights
    )
    return LoadedPrompt(
        name=name,
        model_id=model_id,
        text=text,
        schema=schema,
        version=version,
        estimated_tokens=tokens,
    )


__all__ = [
    "CLASSIFIER_WEIGHTS",
    "EXTRACTOR_WEIGHTS",
    "PROMPTS_DIR",
    "LoadedPrompt",
    "derive_version",
    "estimate_tokens",
    "load_prompt",
]
