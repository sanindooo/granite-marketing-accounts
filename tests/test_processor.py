"""Tests for execution.invoice.processor helpers — HTML body extraction path."""

from __future__ import annotations

from typing import Any

import pytest

from execution.invoice.pdf_fetcher import FetchOutcome, FetchStatus
from execution.invoice.processor import (
    _html_to_text,
    _try_fetch_pdf_from_body,
)

# ---------------------------------------------------------------------------
# _html_to_text
# ---------------------------------------------------------------------------


def test_html_to_text_strips_tags_and_returns_prose() -> None:
    html = """
    <html><body>
      <table><tr><td>Hello <b>Stephen</b></td></tr></table>
      <p>Your invoice is ready.</p>
    </body></html>
    """
    assert _html_to_text(html) == "Hello Stephen Your invoice is ready."


def test_html_to_text_empty_input_returns_empty_string() -> None:
    assert _html_to_text("") == ""


def test_html_to_text_preserves_anchor_label_not_href() -> None:
    # Anchor href is for the URL extractor, not the classifier prompt.
    html = '<p>Click <a href="https://invoice.stripe.com/foo/pdf">PDF</a> here</p>'
    assert _html_to_text(html) == "Click PDF here"


# ---------------------------------------------------------------------------
# _try_fetch_pdf_from_body — HTML-first ordering + finditer
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch fetch_invoice_pdf to record URLs and return PDF bytes for Stripe."""
    seen: list[str] = []

    def stub(url: str, *, client: Any, max_bytes: int = 0) -> FetchOutcome:
        seen.append(url)
        if "invoice.stripe.com" in url:
            return FetchOutcome(
                status=FetchStatus.OK,
                url=url,
                provider="stripe",
                body=b"%PDF-1.4 fake",
                content_type="application/pdf",
            )
        # Generic .pdf URL — pretend it 404s upstream so we keep iterating.
        return FetchOutcome(
            status=FetchStatus.UPSTREAM_ERROR,
            url=url,
            provider="generic",
            reason="404",
        )

    monkeypatch.setattr(
        "execution.invoice.processor.fetch_invoice_pdf", stub
    )
    return seen


def test_html_body_with_stripe_anchor_resolves_to_pdf(
    fake_fetch: list[str],
) -> None:
    # Webflow's Stripe-billed receipt: HTML anchor whose href points at
    # invoice.stripe.com/.../pdf. Must be discovered when only HTML is present.
    html = (
        '<p>Your receipt: '
        '<a href="https://invoice.stripe.com/i/acct_X/in_Y/pdf">PDF</a></p>'
    )
    pdf, outcome = _try_fetch_pdf_from_body(
        text_body="", html_body=html, http_client=object()  # type: ignore[arg-type]
    )
    assert pdf == b"%PDF-1.4 fake"
    assert outcome is not None and outcome.status == FetchStatus.OK


def test_text_body_only_still_works(fake_fetch: list[str]) -> None:
    # Plaintext fallback: bare URL in text body.
    text = "Download: https://invoice.stripe.com/i/acct_X/in_Y/pdf"
    pdf, outcome = _try_fetch_pdf_from_body(
        text_body=text, html_body="", http_client=object()  # type: ignore[arg-type]
    )
    assert pdf == b"%PDF-1.4 fake"
    assert outcome is not None and outcome.status == FetchStatus.OK


def test_html_with_no_invoice_url_returns_none(
    fake_fetch: list[str],
) -> None:
    html = "<p>Hello with no links</p>"
    pdf, outcome = _try_fetch_pdf_from_body(
        text_body="", html_body=html, http_client=object()  # type: ignore[arg-type]
    )
    assert pdf is None
    assert outcome is None
    assert fake_fetch == []
