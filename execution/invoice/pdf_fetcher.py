"""PDF fetcher — resolves invoice-attachment links into bytes on disk.

Stage 2 of the invoice pipeline. After the classifier says an email is an
invoice but no PDF is attached, this module chases the billing link. It is
intentionally conservative:

- Every fetch goes through :mod:`execution.shared.http` so SSRF validation,
  streaming size limits, and bounded redirects apply uniformly.
- Every response is PDF-magic-byte checked. HTML bodies — the signature of
  a login-gated portal like Zoom, Notion, AWS, or GitHub — are rejected
  as :data:`FetchStatus.NEEDS_MANUAL_DOWNLOAD` rather than stored. The
  caller surfaces those as an Exceptions-tab row.
- Known providers with short-lived URLs (Stripe 30-day, Paddle 1-hour)
  are still fetched via the generic path; the plan's provider-specific
  modules arrive in Phase 6 behind a feature flag. All we do now is
  annotate ``provider`` so the filer and the Exceptions-tab row know
  where the PDF came from.

The module never writes to disk. The caller (``invoice/filer.py``) takes
the bytes, writes the sandboxed ``.tmp/invoices/`` file, uploads to
Drive, then commits the SQLite row.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlparse

import httpx

from execution.shared.errors import RateLimitedError, SSRFValidationError
from execution.shared.http import FetchResult, SafeHttpClient, validate_url


class FetchStatus(StrEnum):
    """Outcome of a single :func:`fetch_invoice_pdf` call."""

    OK = "ok"
    NEEDS_MANUAL_DOWNLOAD = "needs_manual_download"
    SSRF_REJECTED = "ssrf_rejected"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"


# Known billing-portal hosts whose "hosted invoice" URL returns HTML.
# Listed with a reason so the Exceptions-tab entry carries context.
LOGIN_GATED_HOSTS: Final[dict[str, str]] = {
    "zoom.us": "Zoom requires portal login to download the PDF",
    "us02web.zoom.us": "Zoom requires portal login to download the PDF",
    "notion.so": "Notion billing PDFs live behind workspace login",
    "www.notion.so": "Notion billing PDFs live behind workspace login",
    "console.aws.amazon.com": "AWS console login required",
    "github.com": "GitHub billing PDFs require account login",
    "portal.azure.com": "Azure portal login required",
    "platform.openai.com": "OpenAI requires account login for billing history",
    "openai.com": "OpenAI requires account login for billing history",
    "billing.google.com": "Google Cloud billing requires login",
    "console.cloud.google.com": "Google Cloud billing requires login",
    "dashboard.heroku.com": "Heroku billing requires login",
    "vercel.com": "Vercel billing requires login",
    "railway.app": "Railway billing requires login",
    "easyjet.com": "easyJet requires account login for booking confirmation",
    "www.easyjet.com": "easyJet requires account login for booking confirmation",
    "thetrainline.com": "Trainline requires account login for ticket PDF",
    "www.thetrainline.com": "Trainline requires account login for ticket PDF",
    "booking.thetrainline.com": "Trainline requires account login for ticket PDF",
    # Webflow's "PDF" anchor in receipt emails points at
    # webflow.com/dashboard/invoice/pdf/<cus_X>/<in_X>.pdf which 302s to
    # webflow.com/login → 403 when unauthenticated. Short-circuit to
    # NEEDS_MANUAL_DOWNLOAD so the user gets a clickable link rather than
    # an "unexpected" crash on the HTTPStatusError.
    "webflow.com": "Webflow requires account login for invoice PDF",
    "www.webflow.com": "Webflow requires account login for invoice PDF",
}

# Hosts whose every subdomain should also be treated as login-gated.
# webflow.com publishes new billing/console subdomains periodically (e.g.
# accounts.webflow.com); without a suffix match new ones silently regress
# from "needs_manual_download" to "unexpected error" the first time they
# appear.
_LOGIN_GATED_DOMAIN_SUFFIXES: Final[tuple[str, ...]] = ("webflow.com",)


def _login_gated_reason(host: str) -> str | None:
    """Return the operator-facing reason if ``host`` is login-gated, else None."""
    if host in LOGIN_GATED_HOSTS:
        return LOGIN_GATED_HOSTS[host]
    for suffix in _LOGIN_GATED_DOMAIN_SUFFIXES:
        if host.endswith("." + suffix):
            return LOGIN_GATED_HOSTS.get(suffix)
    return None

# Providers whose URLs expire quickly — fetch on email receipt, not deferred.
# We recognise them to tag the provider on the FetchOutcome; resolving the
# hosted_invoice_url → invoice_pdf redirect is handled by the SafeHttpClient
# redirect loop.
_STRIPE_HOSTS: Final[frozenset[str]] = frozenset(
    {"pay.stripe.com", "invoice.stripe.com", "files.stripe.com"}
)
_PADDLE_HOSTS: Final[frozenset[str]] = frozenset(
    {"paddle.com", "paddle.net", "vendors.paddle.com", "checkout.paddle.com"}
)


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """Result of a fetch attempt — either PDF bytes or a clear failure reason."""

    status: FetchStatus
    url: str
    provider: str
    body: bytes | None = None
    content_type: str | None = None
    reason: str | None = None


def classify_provider(url: str) -> str:
    """Return a stable provider tag for downstream attribution."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    if host in _STRIPE_HOSTS:
        return "stripe"
    if host in _PADDLE_HOSTS:
        return "paddle"
    if _login_gated_reason(host) is not None:
        return "login_gated"
    return "generic"


def fetch_invoice_pdf(
    url: str,
    *,
    client: SafeHttpClient,
    max_bytes: int = 25 * 1024 * 1024,
) -> FetchOutcome:
    """Fetch ``url`` and return a :class:`FetchOutcome`.

    Never raises for expected failure modes (SSRF, login-gated, rate-limit,
    HTTP error) — all surface as a status so the pipeline can continue
    processing other invoices. Unexpected errors (e.g. timeout) still
    propagate so they can be observed in the run log.
    """
    provider = classify_provider(url)
    host = (urlparse(url).hostname or "").lower().rstrip(".")

    # Run SSRF / userinfo / scheme / port-confusion validation BEFORE the
    # login-gated short-circuit. Otherwise a URL like
    # ``https://attacker@webflow.com/x.pdf`` matches the host allowlist,
    # short-circuits to NEEDS_MANUAL_DOWNLOAD, and the persisted URL never
    # sees the userinfo guard. validate_url's tiny extra DNS hit is cheap
    # compared to the security gain (the result feeds straight into the
    # SafeHttpClient redirect loop's first hop on the non-gated path).
    try:
        validate_url(url)
    except SSRFValidationError as err:
        return FetchOutcome(
            status=FetchStatus.SSRF_REJECTED,
            url=url,
            provider=provider,
            reason=str(err),
        )

    # Short-circuit on known login-gated portals — saves a round-trip and
    # produces a better Exceptions-row reason than a generic HTML rejection.
    gated_reason = _login_gated_reason(host)
    if gated_reason:
        return FetchOutcome(
            status=FetchStatus.NEEDS_MANUAL_DOWNLOAD,
            url=url,
            provider=provider,
            reason=gated_reason,
        )

    try:
        result: FetchResult = client.fetch_bytes(
            url,
            max_bytes=max_bytes,
            require_pdf_magic=True,
        )
    except SSRFValidationError as err:
        # SSRF validator catches three things: blocked IP / host, size-cap
        # breach, PDF magic-byte mismatch. Distinguish HTML-masquerading
        # (magic fail) from actual SSRF rejection for a cleaner reason.
        if "magic" in str(err).lower():
            return FetchOutcome(
                status=FetchStatus.NEEDS_MANUAL_DOWNLOAD,
                url=url,
                provider=provider,
                reason=(
                    "response was not a PDF — likely login page or HTML "
                    "invoice viewer"
                ),
            )
        return FetchOutcome(
            status=FetchStatus.SSRF_REJECTED,
            url=url,
            provider=provider,
            reason=str(err),
        )
    except RateLimitedError as err:
        return FetchOutcome(
            status=FetchStatus.RATE_LIMITED,
            url=url,
            provider=provider,
            reason=str(err),
        )
    except httpx.HTTPStatusError as err:
        # The fetch reached the host but returned 4xx/5xx. Treat 401/403 as
        # NEEDS_MANUAL_DOWNLOAD (auth wall — user can finish the download
        # manually) and other status codes as UPSTREAM_ERROR. Without this
        # branch httpx would propagate up to _process_one's catch-all and
        # surface as "unexpected" — exactly the Webflow case we hit.
        status = err.response.status_code
        if status in (401, 403):
            return FetchOutcome(
                status=FetchStatus.NEEDS_MANUAL_DOWNLOAD,
                url=url,
                provider=provider,
                reason=(
                    f"upstream returned {status} — host likely requires "
                    "session login; download manually and re-upload"
                ),
            )
        return FetchOutcome(
            status=FetchStatus.UPSTREAM_ERROR,
            url=url,
            provider=provider,
            reason=f"HTTP {status} from upstream",
        )

    return FetchOutcome(
        status=FetchStatus.OK,
        url=result.url,
        provider=provider,
        body=result.body,
        content_type=result.content_type,
    )


__all__ = [
    "LOGIN_GATED_HOSTS",
    "FetchOutcome",
    "FetchStatus",
    "classify_provider",
    "fetch_invoice_pdf",
]
