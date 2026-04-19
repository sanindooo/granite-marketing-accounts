"""Name + path primitives used across the invoice pipeline.

Central home for:

- :func:`slug` — produces ``[a-z0-9-]`` slugs from LLM-derived vendor names
  and invoice numbers. Enforces a maximum length and never returns an empty
  string (falls back to a hash-based surrogate so uniqueness indexes stay
  safe).
- :data:`CATEGORIES` — the 8-bucket business-expense taxonomy; every
  filesystem and Drive path validates against this before being built.
- :data:`ALLOWED_CURRENCIES` — mirror of :data:`invoice.extractor.ALLOWED_CURRENCIES`
  so paths don't go around the currency whitelist.
- :func:`resolve_under` — sandbox assertion: the concrete :class:`Path`
  must resolve inside an allowed root, or we refuse.

Every value that eventually reaches a Drive ``files().create`` call or a
local file write passes through these helpers. The plan's
"path-traversal hardening" requirement is enforced here once, rather than
re-implemented per adapter.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Final, Literal

from execution.shared.errors import PathViolationError

Category = Literal[
    "software_saas",
    "travel",
    "meals_entertainment",
    "hardware_office",
    "professional_services",
    "advertising",
    "utilities",
    "other",
]

CATEGORIES: Final[frozenset[Category]] = frozenset(
    {
        "software_saas",
        "travel",
        "meals_entertainment",
        "hardware_office",
        "professional_services",
        "advertising",
        "utilities",
        "other",
    }
)

# Mirrors invoice/extractor.ALLOWED_CURRENCIES — lives here too so filer
# imports stay narrow (filer doesn't need the Pydantic model).
ALLOWED_CURRENCIES: Final[frozenset[str]] = frozenset(
    {
        "GBP", "USD", "EUR", "AUD", "CAD", "CHF", "JPY",
        "SEK", "NOK", "DKK", "PLN", "SGD", "HKD", "NZD",
        "ZAR", "AED", "CNY", "INR", "BRL", "MXN", "THB", "KRW",
    }
)

MAX_VENDOR_SLUG: Final[int] = 60
MAX_INV_NUMBER_SLUG: Final[int] = 64

_SLUG_STRIP: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_SLUG_EDGE_HYPHEN: Final[re.Pattern[str]] = re.compile(r"(^-+|-+$)")


def slug(raw: str | None, *, max_length: int, fallback_key: str | bytes) -> str:
    """Lowercase-hyphen slug of ``raw`` truncated to ``max_length``.

    Falls back to ``syn-<sha256(fallback_key)[:8]>`` when ``raw`` is empty,
    ``None``, or reduces to an empty string after stripping — guarantees
    paths and uniqueness indexes never get an empty string component.
    """
    text = (raw or "").lower()
    text = _SLUG_STRIP.sub("-", text)
    text = _SLUG_EDGE_HYPHEN.sub("", text)
    if not text:
        return _surrogate(fallback_key)
    if len(text) > max_length:
        text = _SLUG_EDGE_HYPHEN.sub("", text[:max_length])
    if not text:  # pathological: max_length 0 or hyphens at cut point
        return _surrogate(fallback_key)
    return text


def vendor_slug(name: str | None, *, fallback_key: str | bytes) -> str:
    return slug(name, max_length=MAX_VENDOR_SLUG, fallback_key=fallback_key)


def invoice_number_slug(number: str | None, *, fallback_key: str | bytes) -> str:
    return slug(number, max_length=MAX_INV_NUMBER_SLUG, fallback_key=fallback_key)


def _surrogate(fallback_key: str | bytes) -> str:
    """Deterministic ``syn-<8hex>`` surrogate for an empty slug."""
    if isinstance(fallback_key, str):
        fallback_key = fallback_key.encode("utf-8")
    digest = hashlib.sha256(fallback_key).hexdigest()[:8]
    return f"syn-{digest}"


def validate_category(value: str) -> Category:
    """Return ``value`` if it's a known category; raise :class:`ValueError` otherwise."""
    if value not in CATEGORIES:
        raise ValueError(
            f"unknown category {value!r}; expected one of {sorted(CATEGORIES)}"
        )
    return value


def validate_currency(value: str) -> str:
    if value not in ALLOWED_CURRENCIES:
        raise ValueError(
            f"currency {value!r} not in ISO 4217 whitelist"
        )
    return value


def resolve_under(path: Path, *, root: Path) -> Path:
    """Return ``path.resolve()`` only if it falls inside ``root.resolve()``.

    Protects against ``..`` segments, symlinks pointing outside the root,
    and absolute-path strings smuggled through slugging.
    """
    resolved = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as err:
        raise PathViolationError(
            f"path {resolved} resolved outside sandbox {resolved_root}",
            source="names",
            details={"path": str(resolved), "root": str(resolved_root)},
            cause=err,
        ) from err
    return resolved


__all__ = [
    "ALLOWED_CURRENCIES",
    "CATEGORIES",
    "MAX_INV_NUMBER_SLUG",
    "MAX_VENDOR_SLUG",
    "Category",
    "invoice_number_slug",
    "resolve_under",
    "slug",
    "validate_category",
    "validate_currency",
    "vendor_slug",
]
