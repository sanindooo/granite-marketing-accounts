"""Tests for execution.shared.prompts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution.shared import prompts as prompts_mod
from execution.shared.claude_client import HAIKU, MIN_CACHEABLE_PREFIX_TOKENS
from execution.shared.prompts import (
    CLASSIFIER_WEIGHTS,
    EXTRACTOR_WEIGHTS,
    LoadedPrompt,
    derive_version,
    estimate_tokens,
    load_prompt,
)

# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------


def test_estimate_tokens_rounds_up():
    # 13 chars at 4 chars/token → ceil(13/4) = 4
    assert estimate_tokens("a" * 13) == 4


def test_estimate_tokens_on_empty_string():
    assert estimate_tokens("") == 0


# ---------------------------------------------------------------------------
# derive_version — stability + sensitivity
# ---------------------------------------------------------------------------


def _args(
    *,
    text: str = "hello world",
    schema: dict | None = None,
    model_id: str = "claude-haiku-4-5",
    weights: tuple[tuple[str, object], ...] = (("w", 1),),
) -> dict:
    return {
        "text": text,
        "schema": schema if schema is not None else {"type": "object"},
        "model_id": model_id,
        "weights": weights,
    }


def test_derive_version_is_deterministic():
    a = derive_version(**_args())
    b = derive_version(**_args())
    assert a == b
    assert len(a) == 8
    assert all(c in "0123456789abcdef" for c in a)


def test_derive_version_changes_when_text_changes():
    a = derive_version(**_args(text="one"))
    b = derive_version(**_args(text="two"))
    assert a != b


def test_derive_version_changes_when_schema_changes():
    a = derive_version(**_args(schema={"type": "object"}))
    b = derive_version(**_args(schema={"type": "array"}))
    assert a != b


def test_derive_version_changes_when_model_changes():
    a = derive_version(**_args(model_id="claude-haiku-4-5"))
    b = derive_version(**_args(model_id="claude-sonnet-4-6"))
    assert a != b


def test_derive_version_changes_when_weights_change():
    a = derive_version(**_args(weights=(("w", 1),)))
    b = derive_version(**_args(weights=(("w", 2),)))
    assert a != b


def test_derive_version_changes_when_schema_key_order_different_false():
    """Sort-key JSON encoding → key order doesn't matter, hash is stable."""
    a = derive_version(**_args(schema={"a": 1, "b": 2}))
    b = derive_version(**_args(schema={"b": 2, "a": 1}))
    assert a == b


# ---------------------------------------------------------------------------
# load_prompt — real files under execution/invoice/prompts/
# ---------------------------------------------------------------------------


def test_classifier_prompt_loads_and_clears_cache_minimum():
    loaded = load_prompt(
        "classifier",
        model_id=HAIKU,
        weights=CLASSIFIER_WEIGHTS,
    )
    assert isinstance(loaded, LoadedPrompt)
    assert loaded.name == "classifier"
    assert loaded.model_id == HAIKU
    assert loaded.estimated_tokens >= MIN_CACHEABLE_PREFIX_TOKENS
    assert loaded.text.startswith("# Email Classifier")
    # Schema sanity checks
    assert loaded.schema["type"] == "object"
    assert set(loaded.schema["required"]) == {
        "classification",
        "confidence",
        "reasoning",
        "signals",
    }
    assert loaded.schema["additionalProperties"] is False


def test_extractor_prompt_loads_and_clears_cache_minimum():
    loaded = load_prompt(
        "extractor",
        model_id=HAIKU,
        weights=EXTRACTOR_WEIGHTS,
    )
    assert loaded.name == "extractor"
    assert loaded.estimated_tokens >= MIN_CACHEABLE_PREFIX_TOKENS
    assert loaded.text.startswith("# Invoice Extractor")
    required = set(loaded.schema["required"])
    for field in (
        "supplier_name",
        "supplier_vat_number",
        "invoice_number",
        "invoice_date",
        "amount_net",
        "amount_vat",
        "amount_gross",
        "vat_rate",
        "reverse_charge",
        "arithmetic_ok",
        "line_items",
        "field_confidence",
        "overall_confidence",
        "extraction_notes",
    ):
        assert field in required, f"missing required field {field}"


def test_loaded_prompt_version_stable_across_reloads():
    a = load_prompt("classifier", model_id=HAIKU, weights=CLASSIFIER_WEIGHTS)
    b = load_prompt("classifier", model_id=HAIKU, weights=CLASSIFIER_WEIGHTS)
    assert a.version == b.version


def test_loaded_prompt_version_changes_on_weight_edit():
    a = load_prompt("classifier", model_id=HAIKU, weights=CLASSIFIER_WEIGHTS)
    bumped = (*CLASSIFIER_WEIGHTS, ("extra", 1))
    b = load_prompt("classifier", model_id=HAIKU, weights=bumped)
    assert a.version != b.version


# ---------------------------------------------------------------------------
# load_prompt — failure modes against a temp dir
# ---------------------------------------------------------------------------


def test_load_prompt_raises_when_prompt_too_short(tmp_path: Path):
    (tmp_path / "tiny.md").write_text("not nearly long enough", encoding="utf-8")
    (tmp_path / "tiny.schema.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="Pad with genuine documentation"):
        load_prompt(
            "tiny",
            model_id=HAIKU,
            weights=(("x", 1),),
            prompts_dir=tmp_path,
        )


def test_load_prompt_raises_when_schema_missing(tmp_path: Path):
    (tmp_path / "solo.md").write_text("x" * 20_000, encoding="utf-8")
    # no schema file
    with pytest.raises(FileNotFoundError):
        load_prompt(
            "solo",
            model_id=HAIKU,
            weights=(("x", 1),),
            prompts_dir=tmp_path,
        )


def test_load_prompt_allows_custom_minimum(tmp_path: Path):
    (tmp_path / "short.md").write_text("x" * 100, encoding="utf-8")
    (tmp_path / "short.schema.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    loaded = load_prompt(
        "short",
        model_id=HAIKU,
        weights=(("x", 1),),
        prompts_dir=tmp_path,
        min_tokens=1,
    )
    assert loaded.estimated_tokens == 25


def test_loaded_prompt_exposes_bytes_and_schema_json():
    loaded = load_prompt(
        "classifier", model_id=HAIKU, weights=CLASSIFIER_WEIGHTS
    )
    assert isinstance(loaded.text_bytes(), bytes)
    assert loaded.text_bytes().decode("utf-8") == loaded.text
    reparsed = json.loads(loaded.schema_json())
    assert reparsed == loaded.schema


def test_prompts_dir_is_under_execution_invoice():
    assert prompts_mod.PROMPTS_DIR.exists()
    assert prompts_mod.PROMPTS_DIR.is_dir()
    assert (prompts_mod.PROMPTS_DIR / "classifier.md").exists()
    assert (prompts_mod.PROMPTS_DIR / "extractor.md").exists()
