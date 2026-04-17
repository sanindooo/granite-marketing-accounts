"""Deterministic business-expense category resolver.

The reconciler and the filer need one of the 8 canonical categories for
every invoice. We derive it in three layers:

1. **Vendor override** — a user-maintained JSON file at
   ``config/vendor_categories.json`` explicitly maps a canonical vendor
   name (or sender domain) to a category. Highest priority; a single
   user correction wins over every heuristic.
2. **Domain-in-mapping** — the built-in :data:`DOMAIN_CATEGORY_HINTS`
   table handles the ~120 vendors we see most often. Lowercase domain
   matched against both exact and suffix (``*.paddle.com``).
3. **LLM fallback** — when neither layer has a match, the caller can
   ask Claude to classify into the 8 buckets. That path is optional
   here (returns ``None`` so the caller can decide whether to pay the
   API tokens). Many runs will never need it once the overrides file
   grows.

Returning a :class:`CategoryDecision` keeps the signal observable: the
Run Status tab reports how many invoices hit each source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from execution.shared.names import CATEGORIES, Category, validate_category

DEFAULT_OVERRIDES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "config" / "vendor_categories.json"
)


class CategorySource(StrEnum):
    OVERRIDE = "override"
    DOMAIN_HINT = "domain_hint"
    LLM = "llm"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class CategoryDecision:
    category: Category
    source: CategorySource
    matched_key: str | None = None


# Minimal starter table. The user can edit
# ``config/vendor_categories.json`` to override or extend per-vendor
# without touching code.
DOMAIN_CATEGORY_HINTS: Final[dict[str, Category]] = {
    # Software / SaaS — the dominant bucket
    "stripe.com": "software_saas",
    "paddle.com": "software_saas",
    "paddle.net": "software_saas",
    "fastspring.com": "software_saas",
    "chargebee.com": "software_saas",
    "github.com": "software_saas",
    "gitlab.com": "software_saas",
    "atlassian.com": "software_saas",
    "slack.com": "software_saas",
    "notion.so": "software_saas",
    "airtable.com": "software_saas",
    "figma.com": "software_saas",
    "linear.app": "software_saas",
    "framer.com": "software_saas",
    "zapier.com": "software_saas",
    "1password.com": "software_saas",
    "lastpass.com": "software_saas",
    "dashlane.com": "software_saas",
    "posthog.com": "software_saas",
    "amplitude.com": "software_saas",
    "segment.com": "software_saas",
    "plausible.io": "software_saas",
    "fathom.fm": "software_saas",
    "datadoghq.com": "software_saas",
    "newrelic.com": "software_saas",
    "sentry.io": "software_saas",
    "rollbar.com": "software_saas",
    "logrocket.com": "software_saas",
    "pingdom.com": "software_saas",
    "uptimerobot.com": "software_saas",
    "browserstack.com": "software_saas",
    "saucelabs.com": "software_saas",
    "cypress.io": "software_saas",
    "testrail.com": "software_saas",
    "miro.com": "software_saas",
    "mural.co": "software_saas",
    "loom.com": "software_saas",
    "dropbox.com": "software_saas",
    "google.com": "software_saas",
    "microsoft.com": "software_saas",
    "office.com": "software_saas",
    "zoho.com": "software_saas",
    "basecamp.com": "software_saas",
    "asana.com": "software_saas",
    "trello.com": "software_saas",
    "clickup.com": "software_saas",
    "monday.com": "software_saas",
    "superhuman.com": "software_saas",
    "hey.com": "software_saas",
    "protonmail.com": "software_saas",
    "proton.me": "software_saas",
    "apple.com": "software_saas",
    "icloud.com": "software_saas",
    "adobe.com": "software_saas",
    "canva.com": "software_saas",
    "descript.com": "software_saas",
    "openai.com": "software_saas",
    "anthropic.com": "software_saas",
    "cursor.sh": "software_saas",
    "cursor.com": "software_saas",
    "replit.com": "software_saas",
    "vercel.com": "software_saas",
    "netlify.com": "software_saas",
    "cloudflare.com": "software_saas",
    "fly.io": "software_saas",
    "render.com": "software_saas",
    "digitalocean.com": "software_saas",
    "linode.com": "software_saas",
    "hetzner.com": "software_saas",
    "amazonaws.com": "software_saas",
    "google-cloud.com": "software_saas",
    "azure.com": "software_saas",
    "zoom.us": "software_saas",
    # Travel
    "britishairways.com": "travel",
    "easyjet.com": "travel",
    "ryanair.com": "travel",
    "virgin-atlantic.com": "travel",
    "lner.co.uk": "travel",
    "avantiwestcoast.co.uk": "travel",
    "trainline.com": "travel",
    "trainpal.com": "travel",
    "enterprise.co.uk": "travel",
    "hertz.co.uk": "travel",
    "avis.co.uk": "travel",
    "sixt.co.uk": "travel",
    "europcar.co.uk": "travel",
    "uber.com": "travel",
    "lyft.com": "travel",
    "bolt.eu": "travel",
    "addisonlee.com": "travel",
    "premierinn.com": "travel",
    "travelodge.co.uk": "travel",
    "hilton.com": "travel",
    "marriott.com": "travel",
    "airbnb.com": "travel",
    "booking.com": "travel",
    "expedia.co.uk": "travel",
    "eurostar.com": "travel",
    # Meals & Entertainment
    "deliveroo.co.uk": "meals_entertainment",
    "ubereats.com": "meals_entertainment",
    "just-eat.co.uk": "meals_entertainment",
    "opentable.com": "meals_entertainment",
    "resy.com": "meals_entertainment",
    "eventbrite.co.uk": "meals_entertainment",
    "ticketmaster.co.uk": "meals_entertainment",
    # Hardware / Office
    "amazon.co.uk": "hardware_office",
    "amazon.com": "hardware_office",
    "johnlewis.com": "hardware_office",
    "argos.co.uk": "hardware_office",
    "ikea.com": "hardware_office",
    "dell.com": "hardware_office",
    "lenovo.com": "hardware_office",
    "logitech.com": "hardware_office",
    "keychron.com": "hardware_office",
    # Advertising
    "googleadservices.com": "advertising",
    "facebook.com": "advertising",
    "meta.com": "advertising",
    "tiktok.com": "advertising",
    "linkedin.com": "advertising",
    "reddit.com": "advertising",
    "x.com": "advertising",
    "bingads.microsoft.com": "advertising",
    # Utilities
    "britishgas.co.uk": "utilities",
    "octopus.energy": "utilities",
    "bt.com": "utilities",
    "virginmedia.com": "utilities",
    "sky.com": "utilities",
    "vodafone.co.uk": "utilities",
    "o2.co.uk": "utilities",
    "ee.co.uk": "utilities",
    "three.co.uk": "utilities",
    "plus.net": "utilities",
    "hyperoptic.com": "utilities",
    "community.fibre": "utilities",
    "wework.com": "utilities",
    "regus.com": "utilities",
    # Other (bank fees, postal, domains, training)
    "wise.com": "other",
    "monzo.com": "other",
    "royalmail.com": "other",
    "dpd.co.uk": "other",
    "namecheap.com": "other",
    "godaddy.com": "other",
    "nordvpn.com": "other",
    "mullvad.net": "other",
    "udemy.com": "other",
    "coursera.org": "other",
    "maven.com": "other",
}

_FALLBACK_CATEGORY: Final[Category] = "other"


def resolve_category(
    *,
    sender_domain: str | None,
    vendor_name: str | None,
    overrides: dict[str, str] | None = None,
    llm_decision: Category | None = None,
) -> CategoryDecision:
    """Resolve the category for one invoice.

    ``overrides`` is the dict loaded from
    ``config/vendor_categories.json`` (empty dict if the file is missing).
    ``llm_decision`` is an optional pre-computed category from Claude —
    the caller decides when to pay for that (low-confidence invoices,
    unknown vendors).
    """
    overrides = overrides or {}

    # 1. Overrides — accept either the exact sender_domain or the
    #    lowercased vendor_name as a key.
    for key in _override_keys(sender_domain=sender_domain, vendor_name=vendor_name):
        if key in overrides:
            return CategoryDecision(
                category=validate_category(overrides[key].lower()),
                source=CategorySource.OVERRIDE,
                matched_key=key,
            )

    # 2. Domain hints
    if sender_domain:
        host = sender_domain.lower().rstrip(".")
        hint = DOMAIN_CATEGORY_HINTS.get(host)
        if hint is None:
            # Try suffix match: ``billing.stripe.com`` → ``stripe.com``.
            for known in DOMAIN_CATEGORY_HINTS:
                if host.endswith("." + known):
                    hint = DOMAIN_CATEGORY_HINTS[known]
                    host = known
                    break
        if hint is not None:
            return CategoryDecision(
                category=hint,
                source=CategorySource.DOMAIN_HINT,
                matched_key=host,
            )

    # 3. LLM decision if supplied by caller
    if llm_decision is not None and llm_decision in CATEGORIES:
        return CategoryDecision(
            category=llm_decision,
            source=CategorySource.LLM,
            matched_key=None,
        )

    return CategoryDecision(
        category=_FALLBACK_CATEGORY,
        source=CategorySource.FALLBACK,
        matched_key=None,
    )


def load_overrides(path: Path | None = None) -> dict[str, str]:
    """Load the user-maintained vendor overrides; empty dict if missing."""
    p = path or DEFAULT_OVERRIDES_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in data.items()}


def _override_keys(
    *, sender_domain: str | None, vendor_name: str | None
) -> list[str]:
    keys: list[str] = []
    if sender_domain:
        keys.append(sender_domain.lower().rstrip("."))
    if vendor_name:
        keys.append(vendor_name.lower())
    return keys


__all__ = [
    "DEFAULT_OVERRIDES_PATH",
    "DOMAIN_CATEGORY_HINTS",
    "CategoryDecision",
    "CategorySource",
    "load_overrides",
    "resolve_category",
]
