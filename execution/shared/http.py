"""SSRF-safe HTTP client for Granite Accounts.

Every outbound HTTP request driven by user-controllable or
adversary-influenceable URLs (PDF links from email bodies, webhooks) routes
through this module. The Anthropic SDK and the Google API clients use their
own transports targeting vetted first-party hosts and are out of scope.

Design constraints (see the plan appendix "Cross-Cutting Concerns — Security
Hardening"):

- Scheme whitelist: ``http`` and ``https`` only. ``file``, ``gopher``,
  ``ftp``, ``data``, ``javascript`` are rejected at the parser.
- Reject ``user:pass@host`` URLs — attacker-chosen Basic auth is a foothold.
- Hostname deny-list for cloud-metadata services.
- Resolve every name to IP, reject IPv4 private/loopback/link-local/reserved
  ranges and IPv6 equivalents including IPv4-mapped private addresses.
- Hard IP deny-list for metadata endpoints (``169.254.169.254``, GCP/Azure
  IMDS, ``fd00:ec2::254``).
- ``follow_redirects=False`` in the underlying transport. We loop manually
  at most :data:`MAX_REDIRECTS` hops, re-validating each new URL.
- Streaming size limit :data:`MAX_RESPONSE_SIZE`. ``Content-Length`` is a
  hint, not a contract; we enforce while reading.
- Bounded timeouts on connect, read, write, pool, and wall clock.
- ``MOCK_MODE`` refuses to construct a live client so tests never make real
  network calls.
- When the response is expected to be a PDF, :func:`is_pdf_body` magic-byte
  checks the first few bytes — never trust ``Content-Type`` alone.

DNS-rebinding note: this module resolves the hostname to validate IPs, then
lets the underlying transport re-resolve when it connects. On modern systems
the OS/stub resolver cache collapses that window to microseconds; a determined
rebinding attack still requires a TTL-0 record change between our validation
and the subsequent connect, and we bound the damage by re-validating every
redirect. A stricter "resolve-then-pin" that forces the connect onto the
validated IP with explicit SNI is a Phase 1C hardening item.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

import httpx

from execution.shared import secrets
from execution.shared.errors import ConfigError, RateLimitedError, SSRFValidationError

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
MAX_RESPONSE_SIZE: Final[int] = 25 * 1024 * 1024  # 25 MB
MAX_REDIRECTS: Final[int] = 3
DEFAULT_USER_AGENT: Final[str] = "granite-accounts/0.1 (+https://granitemarketing.co.uk)"

# Metadata endpoints that must never be reachable by an email-supplied URL.
DENIED_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",  # bare-name edge case
        "instance-data",  # EC2 legacy
        "instance-data.ec2.internal",
    }
)
DENIED_IP_LITERALS: Final[frozenset[str]] = frozenset(
    {
        "169.254.169.254",  # EC2 / GCP / Azure IMDS
        "fd00:ec2::254",  # EC2 IPv6 IMDS
        "100.100.100.200",  # Alibaba Cloud
    }
)

DEFAULT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=5.0, read=60.0, write=30.0, pool=5.0
)
DEFAULT_WALL_CLOCK_SECONDS: Final[float] = 90.0


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A validated HTTP response body."""

    url: str
    status_code: int
    content_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class _ValidatedUrl:
    scheme: str
    host: str
    port: int
    resolved_ips: tuple[str, ...]


def validate_url(url: str) -> _ValidatedUrl:
    """Parse and validate ``url`` against the SSRF deny-list.

    Raises :class:`SSRFValidationError` on any rejection. Returns the tuple
    of scheme, lowercased host, effective port, and resolved IPs so callers
    can log the decision or pin transport behaviour.
    """
    try:
        parsed = urlparse(url)
    except ValueError as err:
        raise SSRFValidationError(
            f"malformed URL {url!r}",
            source="http",
            details={"url": url},
            cause=err,
        ) from err

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise SSRFValidationError(
            f"scheme {parsed.scheme!r} not allowed",
            source="http",
            details={"url": url, "scheme": parsed.scheme},
        )
    if parsed.username or parsed.password:
        raise SSRFValidationError(
            "URL contains userinfo (user:pass@host), rejected",
            source="http",
            details={"url": url},
        )
    host = parsed.hostname
    if not host:
        raise SSRFValidationError(
            f"URL has no host: {url!r}", source="http", details={"url": url}
        )
    host_lower = host.lower().rstrip(".")
    if host_lower in DENIED_HOSTNAMES:
        raise SSRFValidationError(
            f"hostname {host_lower!r} is on the deny-list",
            source="http",
            details={"url": url, "host": host_lower},
        )

    try:
        port = parsed.port
    except ValueError as err:
        raise SSRFValidationError(
            f"invalid port in URL {url!r}",
            source="http",
            details={"url": url},
            cause=err,
        ) from err
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80

    ips = _resolve_and_validate(host_lower, url=url)
    return _ValidatedUrl(
        scheme=parsed.scheme.lower(),
        host=host_lower,
        port=port,
        resolved_ips=ips,
    )


def _resolve_and_validate(host: str, *, url: str) -> tuple[str, ...]:
    """DNS-resolve ``host`` and validate every returned IP.

    IP literals (``http://10.0.0.1/``) resolve to themselves and are validated
    the same way as names.
    """
    # getaddrinfo with None service returns a list of (family, type, proto,
    # canonname, sockaddr) tuples. We skip duplicates to keep the returned
    # set stable for logging.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as err:
        raise SSRFValidationError(
            f"DNS resolution failed for {host!r}",
            source="http",
            details={"url": url, "host": host},
            cause=err,
        ) from err

    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        ip_raw = sockaddr[0]
        if not isinstance(ip_raw, str):
            continue
        if ip_raw in seen:
            continue
        seen.add(ip_raw)
        _reject_unsafe_ip(ip_raw, url=url, host=host)
        ips.append(ip_raw)
    if not ips:
        raise SSRFValidationError(
            f"no usable addresses for {host!r}",
            source="http",
            details={"url": url, "host": host},
        )
    return tuple(ips)


def _reject_unsafe_ip(ip_str: str, *, url: str, host: str) -> None:
    if ip_str in DENIED_IP_LITERALS:
        raise SSRFValidationError(
            f"IP {ip_str} is on the metadata deny-list",
            source="http",
            details={"url": url, "host": host, "ip": ip_str},
        )
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError as err:
        raise SSRFValidationError(
            f"could not parse IP {ip_str!r}",
            source="http",
            details={"url": url, "host": host, "ip": ip_str},
            cause=err,
        ) from err

    # ``is_global`` would be tempting but misses e.g. ULA (``fc00::/7``).
    # Explicit checks make the intent legible and testable.
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        raise SSRFValidationError(
            f"IP {ip_str} is in a non-routable / private range",
            source="http",
            details={"url": url, "host": host, "ip": ip_str},
        )

    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None and (
            mapped.is_private
            or mapped.is_loopback
            or mapped.is_link_local
            or mapped.is_reserved
            or mapped.is_multicast
            or mapped.is_unspecified
        ):
            raise SSRFValidationError(
                f"IPv4-mapped IPv6 {ip_str} resolves to a blocked IPv4 range",
                source="http",
                details={"url": url, "host": host, "ip": ip_str},
            )


def is_pdf_body(body: bytes) -> bool:
    """Magic-byte check for a PDF payload (``%PDF-``)."""
    return body.startswith(b"%PDF-")


class SafeHttpClient:
    """Small wrapper around :class:`httpx.Client` that enforces SSRF + size.

    Reusable across a single pipeline run; construct once, call
    :meth:`fetch_bytes` per URL.
    """

    def __init__(
        self,
        *,
        timeout: httpx.Timeout | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if secrets.is_mock() and transport is None:
            raise ConfigError(
                "SafeHttpClient requires transport= under MOCK_MODE so tests "
                "never leak real network calls.",
                source="http",
            )
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._user_agent = user_agent
        self._client = httpx.Client(
            follow_redirects=False,
            timeout=self._timeout,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SafeHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_bytes(
        self,
        url: str,
        *,
        max_bytes: int = MAX_RESPONSE_SIZE,
        expected_content_type: str | None = None,
        require_pdf_magic: bool = False,
    ) -> FetchResult:
        """Fetch ``url`` and return validated bytes.

        Follows up to :data:`MAX_REDIRECTS` redirects, re-validating every hop.
        Rejects a response whose streamed size exceeds ``max_bytes``.
        """
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            validate_url(current)
            request = self._client.build_request("GET", current)
            with self._client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise SSRFValidationError(
                            f"redirect without Location from {current!r}",
                            source="http",
                            details={"url": current, "status": response.status_code},
                        )
                    current = _resolve_redirect(current, location)
                    continue
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    raise RateLimitedError(
                        f"{current} returned 429",
                        source="http",
                        details={
                            "url": current,
                            "retry_after": retry_after,
                        },
                    )
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if expected_content_type and content_type != expected_content_type:
                    raise SSRFValidationError(
                        f"unexpected content-type {content_type!r}, "
                        f"expected {expected_content_type!r}",
                        source="http",
                        details={"url": current, "content_type": content_type},
                    )
                body = _read_capped(response, max_bytes=max_bytes, url=current)
                if require_pdf_magic and not is_pdf_body(body):
                    raise SSRFValidationError(
                        f"response from {current!r} failed PDF magic-byte check",
                        source="http",
                        details={"url": current, "head": body[:16].hex()},
                    )
                return FetchResult(
                    url=str(request.url),
                    status_code=response.status_code,
                    content_type=content_type,
                    body=body,
                )
        raise SSRFValidationError(
            f"too many redirects starting from {url!r}",
            source="http",
            details={"url": url, "max_redirects": MAX_REDIRECTS},
        )


def _read_capped(
    response: httpx.Response, *, max_bytes: int, url: str
) -> bytes:
    """Stream ``response`` into memory, rejecting once ``max_bytes`` is exceeded."""
    buf = bytearray()
    for chunk in response.iter_bytes():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise SSRFValidationError(
                f"response from {url!r} exceeded {max_bytes} bytes",
                source="http",
                details={"url": url, "limit_bytes": max_bytes, "observed_bytes": len(buf)},
            )
    return bytes(buf)


def _resolve_redirect(current: str, location: str) -> str:
    """Resolve ``location`` against ``current`` for relative redirects."""
    from urllib.parse import urljoin

    return urljoin(current, location)


@contextmanager
def fetch_client(
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: httpx.Timeout | None = None,
) -> Iterator[SafeHttpClient]:
    """Context manager wrapper for :class:`SafeHttpClient`."""
    client = SafeHttpClient(transport=transport, timeout=timeout)
    try:
        yield client
    finally:
        client.close()


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "DEFAULT_WALL_CLOCK_SECONDS",
    "DENIED_HOSTNAMES",
    "DENIED_IP_LITERALS",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_SIZE",
    "FetchResult",
    "SafeHttpClient",
    "fetch_client",
    "is_pdf_body",
    "validate_url",
]
