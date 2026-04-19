"""Tests for execution.shared.names."""

from __future__ import annotations

from pathlib import Path

import pytest

from execution.shared.errors import PathViolationError
from execution.shared.names import (
    ALLOWED_CURRENCIES,
    CATEGORIES,
    MAX_INV_NUMBER_SLUG,
    MAX_VENDOR_SLUG,
    invoice_number_slug,
    resolve_under,
    slug,
    validate_category,
    validate_currency,
    vendor_slug,
)


class TestSlug:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Hello World", "hello-world"),
            ("Acme Inc.", "acme-inc"),
            ("  whitespace   ", "whitespace"),
            ("Already-kebab-case", "already-kebab-case"),
            ("UPPER lowerUpper", "upper-lowerupper"),
            ("Atlassian Pty Ltd", "atlassian-pty-ltd"),
            ("British Gas (Business)", "british-gas-business"),
            ("OpenAI, Inc.", "openai-inc"),
            ("/../escape", "escape"),
        ],
    )
    def test_basic_slugging(self, raw, expected):
        assert slug(raw, max_length=60, fallback_key="x") == expected

    def test_empty_falls_back_to_surrogate(self):
        out = slug("", max_length=60, fallback_key="seed-1")
        assert out.startswith("syn-")
        assert len(out) == len("syn-") + 8

    def test_none_falls_back_to_surrogate(self):
        out = slug(None, max_length=60, fallback_key="seed-2")
        assert out.startswith("syn-")

    def test_punctuation_only_falls_back(self):
        out = slug("!!! ???", max_length=60, fallback_key="seed-3")
        assert out.startswith("syn-")

    def test_surrogate_is_deterministic(self):
        a = slug("", max_length=60, fallback_key="same")
        b = slug("", max_length=60, fallback_key="same")
        assert a == b

    def test_surrogate_changes_with_seed(self):
        a = slug("", max_length=60, fallback_key="one")
        b = slug("", max_length=60, fallback_key="two")
        assert a != b

    def test_truncates_to_max_length(self):
        long = "a" * 100
        out = slug(long, max_length=MAX_VENDOR_SLUG, fallback_key="x")
        assert len(out) <= MAX_VENDOR_SLUG
        assert out == "a" * MAX_VENDOR_SLUG

    def test_truncation_strips_trailing_hyphen(self):
        out = slug("a" * 30 + " " + "b" * 30, max_length=31, fallback_key="x")
        # After collapsing space→hyphen we have "aaaaa...-bbb..."; truncation
        # must not leave a trailing hyphen.
        assert not out.endswith("-")

    def test_surrogate_from_bytes_key(self):
        out = slug("", max_length=60, fallback_key=b"bytes-seed")
        assert out.startswith("syn-")


class TestVendorAndInvoiceSlug:
    def test_vendor_slug_uses_vendor_max(self):
        long = "v" * 200
        out = vendor_slug(long, fallback_key="x")
        assert len(out) <= MAX_VENDOR_SLUG

    def test_invoice_number_slug_uses_invoice_max(self):
        long = "I" * 200
        out = invoice_number_slug(long, fallback_key="x")
        assert len(out) <= MAX_INV_NUMBER_SLUG


class TestValidateCategory:
    @pytest.mark.parametrize("cat", sorted(CATEGORIES))
    def test_accepts_each_canonical_category(self, cat):
        assert validate_category(cat) == cat

    def test_rejects_unknown(self):
        with pytest.raises(ValueError, match="unknown category"):
            validate_category("not_a_category")

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError):
            validate_category("TRAVEL")


class TestValidateCurrency:
    @pytest.mark.parametrize("ccy", ["GBP", "USD", "EUR"])
    def test_accepts_core(self, ccy):
        assert validate_currency(ccy) == ccy

    def test_rejects_unknown(self):
        with pytest.raises(ValueError):
            validate_currency("XXX")

    def test_rejects_lowercase(self):
        with pytest.raises(ValueError):
            validate_currency("gbp")


class TestResolveUnder:
    def test_accepts_child_path(self, tmp_path: Path):
        child = tmp_path / "sub" / "file.pdf"
        child.parent.mkdir(parents=True)
        child.write_bytes(b"")
        assert resolve_under(child, root=tmp_path) == child.resolve()

    def test_rejects_parent_traversal(self, tmp_path: Path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        hostile = sandbox / ".." / "outside"
        with pytest.raises(PathViolationError):
            resolve_under(hostile, root=sandbox)

    def test_rejects_absolute_escape(self, tmp_path: Path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        with pytest.raises(PathViolationError):
            resolve_under(Path("/etc/passwd"), root=sandbox)

    def test_accepts_nonexistent_child(self, tmp_path: Path):
        """Sandboxing must not require the target to exist yet."""
        target = tmp_path / "does_not_exist.pdf"
        assert resolve_under(target, root=tmp_path) == target.resolve()


def test_allowed_currencies_mirrors_iso_4217_subset():
    for core in ("GBP", "USD", "EUR", "AUD", "CAD", "JPY"):
        assert core in ALLOWED_CURRENCIES


def test_categories_is_frozen_set_of_eight():
    assert isinstance(CATEGORIES, frozenset)
    assert len(CATEGORIES) == 8
