"""Tests for execution.invoice.category."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution.invoice.category import (
    DOMAIN_CATEGORY_HINTS,
    CategoryDecision,
    CategorySource,
    load_overrides,
    resolve_category,
)


class TestResolveCategory:
    def test_override_wins_over_domain_hint(self):
        decision = resolve_category(
            sender_domain="stripe.com",  # domain-hint says software_saas
            vendor_name="Stripe",
            overrides={"stripe.com": "other"},
        )
        assert decision.source is CategorySource.OVERRIDE
        assert decision.category == "other"
        assert decision.matched_key == "stripe.com"

    def test_override_by_vendor_name(self):
        decision = resolve_category(
            sender_domain=None,
            vendor_name="Acme Corp",
            overrides={"acme corp": "professional_services"},
        )
        assert decision.source is CategorySource.OVERRIDE
        assert decision.category == "professional_services"

    def test_override_rejected_if_unknown_category(self):
        with pytest.raises(ValueError):
            resolve_category(
                sender_domain="x.com",
                vendor_name=None,
                overrides={"x.com": "bogus_bucket"},
            )

    @pytest.mark.parametrize(
        "domain, expected",
        [
            ("stripe.com", "software_saas"),
            ("github.com", "software_saas"),
            ("airbnb.com", "travel"),
            ("britishgas.co.uk", "utilities"),
            ("amazon.co.uk", "hardware_office"),
            ("facebook.com", "advertising"),
        ],
    )
    def test_domain_hint_direct_match(self, domain, expected):
        decision = resolve_category(
            sender_domain=domain, vendor_name=None,
        )
        assert decision.source is CategorySource.DOMAIN_HINT
        assert decision.category == expected
        assert decision.matched_key == domain

    def test_domain_hint_subdomain_match(self):
        decision = resolve_category(
            sender_domain="billing.stripe.com", vendor_name=None,
        )
        assert decision.source is CategorySource.DOMAIN_HINT
        assert decision.category == "software_saas"
        assert decision.matched_key == "stripe.com"

    def test_llm_decision_used_when_no_override_and_no_hint(self):
        decision = resolve_category(
            sender_domain="unknown.example",
            vendor_name="Some Supplier",
            llm_decision="professional_services",
        )
        assert decision.source is CategorySource.LLM
        assert decision.category == "professional_services"

    def test_llm_decision_rejected_if_not_canonical(self):
        decision = resolve_category(
            sender_domain="unknown.example",
            vendor_name=None,
            llm_decision="bogus_bucket",  # type: ignore[arg-type]
        )
        assert decision.source is CategorySource.FALLBACK
        assert decision.category == "other"

    def test_fallback_when_nothing_matches(self):
        decision = resolve_category(
            sender_domain=None,
            vendor_name=None,
        )
        assert decision.source is CategorySource.FALLBACK
        assert decision.category == "other"

    def test_case_insensitive_domain(self):
        decision = resolve_category(
            sender_domain="STRIPE.com", vendor_name=None,
        )
        assert decision.source is CategorySource.DOMAIN_HINT
        assert decision.category == "software_saas"

    def test_trailing_dot_tolerated(self):
        decision = resolve_category(
            sender_domain="stripe.com.", vendor_name=None,
        )
        assert decision.source is CategorySource.DOMAIN_HINT


class TestLoadOverrides:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert load_overrides(tmp_path / "nope.json") == {}

    def test_valid_file(self, tmp_path: Path):
        p = tmp_path / "overrides.json"
        p.write_text(
            json.dumps({"Stripe.com": "Other", "Acme": "professional_services"}),
            encoding="utf-8",
        )
        out = load_overrides(p)
        # Keys lowercased
        assert out == {"stripe.com": "Other", "acme": "professional_services"}

    def test_malformed_json_returns_empty(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        assert load_overrides(p) == {}

    def test_non_dict_root_returns_empty(self, tmp_path: Path):
        p = tmp_path / "list.json"
        p.write_text(json.dumps(["stripe.com", "other"]), encoding="utf-8")
        assert load_overrides(p) == {}


def test_domain_category_hints_covers_each_bucket():
    """Every canonical bucket must have at least one vendor example."""
    present = set(DOMAIN_CATEGORY_HINTS.values())
    # 'professional_services' isn't in the starter table (the user's
    # accountant / lawyer is vendor-specific). Everything else is.
    for bucket in (
        "software_saas",
        "travel",
        "meals_entertainment",
        "hardware_office",
        "advertising",
        "utilities",
        "other",
    ):
        assert bucket in present, f"no example for {bucket}"


def test_decision_is_frozen():
    decision = CategoryDecision(category="other", source=CategorySource.FALLBACK)
    with pytest.raises(AttributeError):
        decision.category = "travel"  # type: ignore[misc]
