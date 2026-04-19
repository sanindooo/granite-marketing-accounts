"""Tests for execution.shared.http."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable

import httpx
import pytest

from execution.shared import http as http_mod
from execution.shared import secrets
from execution.shared.errors import (
    ConfigError,
    RateLimitedError,
    SSRFValidationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_getaddrinfo(mapping: dict[str, list[str]]) -> Callable[..., list[tuple]]:
    """Return a :func:`socket.getaddrinfo` replacement backed by ``mapping``."""

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
    """Fixture: install a deterministic ``socket.getaddrinfo`` stub."""

    def install(mapping: dict[str, list[str]]) -> None:
        monkeypatch.setattr(
            http_mod.socket,
            "getaddrinfo",
            _fake_getaddrinfo(mapping),
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


# Routable public IPs chosen so ``ipaddress`` doesn't mark them reserved.
_PUB_A = "8.8.8.8"
_PUB_B = "1.1.1.1"
_PUB_C = "9.9.9.9"


# ---------------------------------------------------------------------------
# validate_url — scheme + host + IP rejection matrix
# ---------------------------------------------------------------------------


def test_validate_url_rejects_file_scheme(ip_map):
    ip_map({"example.com": [_PUB_A]})
    with pytest.raises(SSRFValidationError, match="scheme"):
        http_mod.validate_url("file:///etc/passwd")


def test_validate_url_rejects_javascript_scheme(ip_map):
    ip_map({"example.com": [_PUB_A]})
    with pytest.raises(SSRFValidationError, match="scheme"):
        http_mod.validate_url("javascript:alert(1)")


def test_validate_url_rejects_userinfo(ip_map):
    ip_map({"example.com": [_PUB_A]})
    with pytest.raises(SSRFValidationError, match="userinfo"):
        http_mod.validate_url("http://user:pass@example.com/")


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "172.16.0.5",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:10.0.0.1",
    ],
)
def test_validate_url_rejects_private_and_metadata_ips(ip_map, ip):
    ip_map({"evil.example": [ip]})
    with pytest.raises(SSRFValidationError):
        http_mod.validate_url("https://evil.example/pdf")


def test_validate_url_rejects_metadata_hostname(ip_map):
    ip_map({"metadata.google.internal": [_PUB_A]})
    with pytest.raises(SSRFValidationError, match="deny-list"):
        http_mod.validate_url("http://metadata.google.internal/")


def test_validate_url_rejects_metadata_hostname_bare(ip_map):
    ip_map({"metadata": [_PUB_A]})
    with pytest.raises(SSRFValidationError, match="deny-list"):
        http_mod.validate_url("http://metadata/")


def test_validate_url_accepts_public_https(ip_map):
    ip_map({"api.stripe.com": [_PUB_A]})
    out = http_mod.validate_url("https://api.stripe.com/v1/invoices")
    assert out.scheme == "https"
    assert out.host == "api.stripe.com"
    assert out.port == 443
    assert out.resolved_ips == (_PUB_A,)


def test_validate_url_accepts_explicit_port(ip_map):
    ip_map({"example.com": [_PUB_A]})
    out = http_mod.validate_url("http://example.com:8080/x")
    assert out.port == 8080


def test_validate_url_rejects_invalid_port(ip_map):
    ip_map({"example.com": [_PUB_A]})
    with pytest.raises(SSRFValidationError, match="port"):
        http_mod.validate_url("http://example.com:99999/x")


def test_validate_url_rejects_dns_failure(ip_map):
    ip_map({})  # every lookup misses
    with pytest.raises(SSRFValidationError, match="DNS"):
        http_mod.validate_url("https://no-such-host.example/")


def test_is_pdf_body_matches_magic():
    assert http_mod.is_pdf_body(b"%PDF-1.7\n...") is True
    assert http_mod.is_pdf_body(b"<html>") is False
    assert http_mod.is_pdf_body(b"") is False


# ---------------------------------------------------------------------------
# SafeHttpClient — transport behaviours
# ---------------------------------------------------------------------------


def test_mock_mode_refuses_live_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(secrets, "is_mock", lambda: True)
    with pytest.raises(ConfigError, match="MOCK_MODE"):
        http_mod.SafeHttpClient()


def test_fetch_bytes_happy_path(ip_map):
    ip_map({"api.stripe.com": [_PUB_A]})
    transport = _mock_transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.7\nhello",
            )
        ]
    )
    with http_mod.SafeHttpClient(transport=transport) as client:
        result = client.fetch_bytes(
            "https://api.stripe.com/v1/invoices/inv_1.pdf",
            expected_content_type="application/pdf",
            require_pdf_magic=True,
        )
    assert result.status_code == 200
    assert result.content_type == "application/pdf"
    assert result.body.startswith(b"%PDF-")


def test_fetch_bytes_rejects_oversize_stream(ip_map):
    ip_map({"big.example": [_PUB_A]})
    transport = _mock_transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"A" * 5000,
            )
        ]
    )
    with (
        http_mod.SafeHttpClient(transport=transport) as client,
        pytest.raises(SSRFValidationError, match="exceeded"),
    ):
        client.fetch_bytes("https://big.example/pdf", max_bytes=1000)


def test_fetch_bytes_rejects_pdf_without_magic(ip_map):
    ip_map({"api.example": [_PUB_A]})
    transport = _mock_transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"<html>not a pdf</html>",
            )
        ]
    )
    with (
        http_mod.SafeHttpClient(transport=transport) as client,
        pytest.raises(SSRFValidationError, match="magic"),
    ):
        client.fetch_bytes(
            "https://api.example/invoice.pdf",
            require_pdf_magic=True,
        )


def test_fetch_bytes_rejects_unexpected_content_type(ip_map):
    ip_map({"api.example": [_PUB_A]})
    transport = _mock_transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html/>",
            )
        ]
    )
    with (
        http_mod.SafeHttpClient(transport=transport) as client,
        pytest.raises(SSRFValidationError, match="content-type"),
    ):
        client.fetch_bytes(
            "https://api.example/invoice.pdf",
            expected_content_type="application/pdf",
        )


def test_fetch_bytes_follows_bounded_redirects(ip_map):
    ip_map({"hop1.example": [_PUB_A], "hop2.example": [_PUB_B]})
    transport = _mock_transport(
        [
            httpx.Response(
                302,
                headers={"Location": "https://hop2.example/final"},
            ),
            httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-ok",
            ),
        ]
    )
    with http_mod.SafeHttpClient(transport=transport) as client:
        result = client.fetch_bytes("https://hop1.example/start")
    assert result.body == b"%PDF-ok"


def test_fetch_bytes_revalidates_redirect_host(ip_map):
    ip_map({"start.example": [_PUB_A], "evil.example": ["10.0.0.5"]})
    transport = _mock_transport(
        [
            httpx.Response(
                302,
                headers={"Location": "https://evil.example/attack"},
            ),
        ]
    )
    with (
        http_mod.SafeHttpClient(transport=transport) as client,
        pytest.raises(SSRFValidationError, match="private"),
    ):
        client.fetch_bytes("https://start.example/start")


def test_fetch_bytes_caps_redirect_chain(ip_map):
    ip_map({"hop.example": [_PUB_A]})
    responses = [
        httpx.Response(302, headers={"Location": "https://hop.example/next"})
        for _ in range(http_mod.MAX_REDIRECTS + 1)
    ]
    transport = _mock_transport(responses)
    with (
        http_mod.SafeHttpClient(transport=transport) as client,
        pytest.raises(SSRFValidationError, match="too many redirects"),
    ):
        client.fetch_bytes("https://hop.example/start")


def test_fetch_bytes_rejects_redirect_without_location(ip_map):
    ip_map({"hop.example": [_PUB_A]})
    transport = _mock_transport([httpx.Response(302)])
    with (
        http_mod.SafeHttpClient(transport=transport) as client,
        pytest.raises(SSRFValidationError, match="redirect without Location"),
    ):
        client.fetch_bytes("https://hop.example/start")


def test_fetch_bytes_surfaces_rate_limit(ip_map):
    ip_map({"api.example": [_PUB_A]})
    transport = _mock_transport(
        [httpx.Response(429, headers={"Retry-After": "42"})]
    )
    with (
        http_mod.SafeHttpClient(transport=transport) as client,
        pytest.raises(RateLimitedError) as exc_info,
    ):
        client.fetch_bytes("https://api.example/x")
    assert exc_info.value.details.get("retry_after") == "42"


def test_fetch_bytes_follows_relative_redirect(ip_map):
    ip_map({"hop.example": [_PUB_A]})
    transport = _mock_transport(
        [
            httpx.Response(302, headers={"Location": "/next"}),
            httpx.Response(
                200,
                headers={"Content-Type": "application/pdf"},
                content=b"%PDF-ok",
            ),
        ]
    )
    with http_mod.SafeHttpClient(transport=transport) as client:
        result = client.fetch_bytes("https://hop.example/start")
    assert result.body == b"%PDF-ok"


def test_denied_ip_literals_constant_contains_imds():
    assert "169.254.169.254" in http_mod.DENIED_IP_LITERALS
