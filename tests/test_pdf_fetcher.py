"""Tests for execution.invoice.pdf_fetcher."""

from __future__ import annotations

from collections.abc import Callable, Iterable

import httpx
import pytest

from execution.invoice.pdf_fetcher import (
    LOGIN_GATED_HOSTS,
    FetchOutcome,
    FetchStatus,
    classify_provider,
    fetch_invoice_pdf,
)
from execution.shared import http as http_mod

# ---------------------------------------------------------------------------
# Helpers (mirrors the socket-stubbing pattern from test_http.py)
# ---------------------------------------------------------------------------


def _fake_getaddrinfo(mapping: dict[str, list[str]]) -> Callable[..., list[tuple]]:
    import socket

    def fake(host: str, _port=None, *_args, **_kwargs):
        if host not in mapping:
            raise socket.gaierror(f"no fake entry for {host!r}")
        out: list[tuple] = []
        for ip in mapping[host]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
            out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return out

    return fake


@pytest.fixture
def ip_map(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, list[str]]], None]:
    def install(mapping: dict[str, list[str]]) -> None:
        monkeypatch.setattr(
            http_mod.socket, "getaddrinfo", _fake_getaddrinfo(mapping)
        )

    return install


def _mock_transport(sequence: Iterable[httpx.Response]) -> httpx.MockTransport:
    it = iter(sequence)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return next(it)
        except StopIteration as err:  # pragma: no cover
            raise AssertionError(
                f"unexpected request to {request.url}"
            ) from err

    return httpx.MockTransport(handler)


_PUB = "8.8.8.8"


# ---------------------------------------------------------------------------
# classify_provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://pay.stripe.com/invoice/abc.pdf", "stripe"),
        ("https://invoice.stripe.com/i/acct_x/123", "stripe"),
        ("https://paddle.com/invoice/12345", "paddle"),
        ("https://checkout.paddle.com/i/x", "paddle"),
        ("https://zoom.us/billing/invoice/abc", "login_gated"),
        ("https://github.com/orgs/acme/billing/invoice.pdf", "login_gated"),
        # Webflow added 2026-05-01: their "PDF" anchor in receipt emails
        # 302s to webflow.com/login → 403. Short-circuit to NEEDS_MANUAL.
        ("https://webflow.com/dashboard/invoice/pdf/cus_X/in_X.pdf",
         "login_gated"),
        ("https://billing.example.com/inv.pdf", "generic"),
    ],
)
def test_classify_provider(url, expected):
    assert classify_provider(url) == expected


def test_classify_provider_handles_malformed_url():
    assert classify_provider("not-a-url") == "unknown"


# ---------------------------------------------------------------------------
# fetch_invoice_pdf
# ---------------------------------------------------------------------------


def test_fetch_succeeds_on_stripe_pdf(ip_map):
    ip_map({"files.stripe.com": [_PUB]})
    transport = _mock_transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.7\ndata",
            )
        ]
    )
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://files.stripe.com/invoices/acct/1.pdf", client=client
        )
    assert outcome.status is FetchStatus.OK
    assert outcome.provider == "stripe"
    assert outcome.body is not None
    assert outcome.body.startswith(b"%PDF-")


def test_fetch_short_circuits_known_login_gated_host(ip_map):
    ip_map({"zoom.us": [_PUB]})
    # No transport calls should be made — short-circuit in pdf_fetcher
    called: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(True)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://zoom.us/billing/invoice/abc", client=client
        )
    assert outcome.status is FetchStatus.NEEDS_MANUAL_DOWNLOAD
    assert outcome.provider == "login_gated"
    assert outcome.reason is not None
    assert "Zoom" in outcome.reason
    assert called == []  # no HTTP call was made


def test_fetch_rejects_html_response_as_needs_manual(ip_map):
    ip_map({"billing.example.com": [_PUB]})
    transport = _mock_transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html>login</html>",
            )
        ]
    )
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://billing.example.com/inv.pdf", client=client
        )
    # The SafeHttpClient raises SSRF on magic mismatch; fetch_invoice_pdf
    # should translate that to NEEDS_MANUAL_DOWNLOAD with a readable reason.
    assert outcome.status is FetchStatus.NEEDS_MANUAL_DOWNLOAD
    assert outcome.reason is not None
    assert "PDF" in outcome.reason


def test_fetch_short_circuits_webflow_invoice_pdf(ip_map):
    """Regression: 2026-05-01 the user reported every Webflow invoice email
    crashed with HTTPStatusError 403 because webflow.com/dashboard/invoice/pdf
    302s to webflow.com/login → 403. Webflow is now in LOGIN_GATED_HOSTS so
    the fetcher short-circuits before ever making the HTTP request."""
    ip_map({"webflow.com": [_PUB]})
    called: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(True)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://webflow.com/dashboard/invoice/pdf/cus_X/in_X.pdf",
            client=client,
        )
    assert outcome.status is FetchStatus.NEEDS_MANUAL_DOWNLOAD
    assert outcome.provider == "login_gated"
    assert outcome.reason and "Webflow" in outcome.reason
    assert called == [], "must not hit the network for known login-gated host"


def test_fetch_handles_403_from_unknown_host_as_needs_manual(ip_map):
    """Defensive: an unrecognised host returning 401/403 (e.g. session
    expired, IP-blocked) used to propagate HTTPStatusError to the
    processor's catch-all and surface as 'unexpected'. Now treated as
    NEEDS_MANUAL_DOWNLOAD so the user can finish the download manually."""
    ip_map({"unknown-billing.example": [_PUB]})
    transport = _mock_transport(
        [httpx.Response(403, content=b"Forbidden")]
    )
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://unknown-billing.example/inv.pdf", client=client
        )
    assert outcome.status is FetchStatus.NEEDS_MANUAL_DOWNLOAD
    assert outcome.reason and "403" in outcome.reason


def test_fetch_handles_5xx_as_upstream_error(ip_map):
    ip_map({"flaky.example": [_PUB]})
    transport = _mock_transport([httpx.Response(500, content=b"oops")])
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://flaky.example/inv.pdf", client=client
        )
    assert outcome.status is FetchStatus.UPSTREAM_ERROR
    assert outcome.reason and "500" in outcome.reason


def test_fetch_returns_ssrf_rejected_on_private_ip(ip_map):
    ip_map({"evil.example": ["10.0.0.5"]})
    transport = _mock_transport([httpx.Response(200)])  # never reached
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://evil.example/pdf", client=client
        )
    assert outcome.status is FetchStatus.SSRF_REJECTED
    assert outcome.reason is not None


def test_fetch_returns_rate_limited(ip_map):
    ip_map({"billing.example.com": [_PUB]})
    transport = _mock_transport(
        [httpx.Response(429, headers={"Retry-After": "60"})]
    )
    with http_mod.SafeHttpClient(transport=transport) as client:
        outcome = fetch_invoice_pdf(
            "https://billing.example.com/pdf", client=client
        )
    assert outcome.status is FetchStatus.RATE_LIMITED


def test_login_gated_hosts_includes_core_vendors():
    for host in ("zoom.us", "notion.so", "github.com"):
        assert host in LOGIN_GATED_HOSTS


def test_outcome_shape_is_frozen_dataclass():
    outcome = FetchOutcome(
        status=FetchStatus.OK,
        url="https://x",
        provider="stripe",
        body=b"%PDF-",
    )
    with pytest.raises(AttributeError):
        outcome.url = "other"  # type: ignore[misc]
