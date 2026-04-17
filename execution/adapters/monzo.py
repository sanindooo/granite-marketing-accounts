"""Monzo banking adapter.

Monzo uses OAuth 2.0 with a confidential-client authorization-code
grant. The first-run flow is:

1. We open ``https://auth.monzo.com/`` with our ``client_id``,
   ``redirect_uri=http://localhost:8080/callback``, and a random
   ``state`` token. The user confirms the app in their browser and
   then approves access again inside the Monzo app itself (this is
   Monzo's Strong Customer Authentication requirement).
2. Our local HTTP server receives the redirect with ``?code=...``.
3. We ``POST`` to ``https://api.monzo.com/oauth2/token`` exchanging
   ``code`` for ``access_token`` + ``refresh_token``.
4. **Inside the 5-minute SCA window** — the interval between the
   user's approval and now — we can call the SCA-gated endpoints
   (``/accounts``, ``/transactions``) with full history. After that,
   the same endpoints return empty arrays until the next re-auth.
   The plan's directive exploits this: backfill the entire history on
   first auth, then switch to sliding-window refresh.
5. Tokens are cached in the Keychain at ``granite-accounts/monzo/
   token_cache`` (JSON blob). Subsequent runs use ``refresh_token``
   to mint new access tokens without SCA — but after 90 days the
   refresh token expires and the user must re-auth.

Design decisions carried forward from the plan:

- ``client_id`` + ``client_secret`` live in Keychain under
  ``granite-accounts/monzo`` (namespace ``monzo``). ``client_secret``
  never appears in logs or error payloads.
- The local callback server binds to ``127.0.0.1:8080`` only; no
  external exposure. ``state`` is a 32-byte urlsafe random token
  validated on return to prevent CSRF against the callback.
- Refresh flow auto-retries on 401 by re-reading the cache — it
  covers the race where two adapters refresh concurrently. Persistent
  401/403 raise :class:`AuthExpiredError` and the user re-auths.
- 429 / 5xx → :class:`RateLimitedError` for the caller to retry.
- The plan marks Monzo's 90-day horizon as a cliff; the adapter
  records ``first_auth_at`` + ``last_refresh_at`` on the token cache
  so the healthcheck can warn at day 60.
"""

from __future__ import annotations

import json
import secrets as rnd
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlencode, urlparse

from execution.adapters.amex_csv import canonicalise_description
from execution.shared import secrets as secret_store
from execution.shared.errors import (
    AuthExpiredError,
    ConfigError,
    RateLimitedError,
    SchemaViolationError,
)
from execution.shared.money import to_money

if TYPE_CHECKING:  # pragma: no cover
    import httpx

SOURCE_ID: Final[str] = "monzo"
SECRETS_NAMESPACE: Final[str] = "monzo"
ACCOUNT_LABEL: Final[str] = "monzo"

MONZO_API_BASE: Final[str] = "https://api.monzo.com"
MONZO_AUTH_URL: Final[str] = "https://auth.monzo.com/"
MONZO_TOKEN_PATH: Final[str] = "/oauth2/token"  # noqa: S105 — URL path, not a secret
MONZO_ACCOUNTS_PATH: Final[str] = "/accounts"
MONZO_TRANSACTIONS_PATH: Final[str] = "/transactions"

DEFAULT_REDIRECT_HOST: Final[str] = "127.0.0.1"
DEFAULT_REDIRECT_PORT: Final[int] = 8080
DEFAULT_REDIRECT_URI: Final[str] = (
    f"http://localhost:{DEFAULT_REDIRECT_PORT}/callback"
)
DEFAULT_BATCH_SIZE: Final[int] = 50
DEFAULT_WINDOW_DAYS: Final[int] = 60  # inside Monzo's 90-day limit
# Monzo refresh tokens expire after ~90 days — we warn earlier so the
# healthcheck can reach the user before they're locked out.
REFRESH_TOKEN_WARN_DAYS: Final[int] = 30


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenCache:
    """The JSON shape stored under ``granite-accounts/monzo/token_cache``."""

    access_token: str
    refresh_token: str
    access_expires_at: datetime
    first_auth_at: datetime
    last_refresh_at: datetime
    user_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "access_expires_at": self.access_expires_at.isoformat(),
                "first_auth_at": self.first_auth_at.isoformat(),
                "last_refresh_at": self.last_refresh_at.isoformat(),
                "user_id": self.user_id,
            }
        )

    @classmethod
    def from_json(cls, blob: str) -> TokenCache:
        data = json.loads(blob)
        try:
            return cls(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                access_expires_at=_parse_iso(data["access_expires_at"]),
                first_auth_at=_parse_iso(data["first_auth_at"]),
                last_refresh_at=_parse_iso(data["last_refresh_at"]),
                user_id=data.get("user_id"),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise ConfigError(
                "Monzo token cache JSON is malformed",
                source=SOURCE_ID,
                cause=err,
            ) from err

    def is_expiring_soon(self, *, now: datetime, within: timedelta) -> bool:
        return self.access_expires_at <= now + within

    def refresh_token_age(self, *, now: datetime) -> timedelta:
        return now - self.first_auth_at


def load_token_cache() -> TokenCache | None:
    blob = secret_store.get(SECRETS_NAMESPACE, "token_cache")
    if not blob:
        return None
    return TokenCache.from_json(blob)


def save_token_cache(cache: TokenCache) -> None:
    secret_store.put(SECRETS_NAMESPACE, "token_cache", cache.to_json())


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonzoAccount:
    account_id: str
    description: str
    currency: str
    account_type: str  # "uk_retail" | "uk_prepaid" | ...


@dataclass(frozen=True, slots=True)
class RawTransaction:
    """Normalised Monzo transaction ready for the ledger."""

    txn_id: str
    account: str
    booking_date: date
    description_raw: str
    description_canonical: str
    currency: str
    amount: Decimal
    reference: str | None
    category_hint: str | None
    status: str  # "pending" | "settled"
    provider_auth_id: str | None
    source: str = SOURCE_ID

    def as_row(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "account": self.account,
            "booking_date": self.booking_date.isoformat(),
            "description_raw": self.description_raw,
            "description_canonical": self.description_canonical,
            "currency": self.currency,
            "amount": format(self.amount, "f"),
            "provider_auth_id": self.provider_auth_id or self.reference,
            "category": self.category_hint,
            "status": self.status,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class MonzoAuth:
    """Owns the token cache and knows how to refresh / exchange codes.

    Tests inject an ``httpx.Client`` plus a ``clock`` callable so the
    authorisation-code exchange can be exercised without hitting the
    real Monzo OAuth server.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        http: httpx.Client | None = None,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ConfigError(
                "Monzo client_id and client_secret must both be set",
                source=SOURCE_ID,
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._cache: TokenCache | None = None

    @classmethod
    def from_keychain(cls, *, redirect_uri: str = DEFAULT_REDIRECT_URI) -> MonzoAuth:
        if secret_store.is_mock():
            raise ConfigError(
                "MonzoAuth.from_keychain() under MOCK_MODE — inject auth= "
                "explicitly on the adapter.",
                source=SOURCE_ID,
            )
        client_id = secret_store.require(SECRETS_NAMESPACE, "client_id")
        client_secret = secret_store.require(SECRETS_NAMESPACE, "client_secret")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )

    # ------------------------------------------------------------------
    # External surface
    # ------------------------------------------------------------------

    def build_authorize_url(self, *, state: str) -> str:
        """Return the auth URL the user opens in their browser."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return f"{MONZO_AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str) -> TokenCache:
        """Exchange a one-shot authorization code for tokens."""
        payload = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "code": code,
        }
        data = self._token_request(payload)
        now = self._clock()
        cache = TokenCache(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            access_expires_at=now + timedelta(seconds=int(data["expires_in"])),
            first_auth_at=now,
            last_refresh_at=now,
            user_id=data.get("user_id"),
        )
        self._cache = cache
        save_token_cache(cache)
        return cache

    def refresh(self) -> TokenCache:
        """Refresh the access token using the cached refresh token."""
        cache = self._cache or load_token_cache()
        if cache is None:
            raise AuthExpiredError(
                "Monzo token cache is empty — run `granite ops reauth monzo`",
                source=SOURCE_ID,
            )
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": cache.refresh_token,
        }
        data = self._token_request(payload)
        now = self._clock()
        new_cache = TokenCache(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or cache.refresh_token),
            access_expires_at=now + timedelta(seconds=int(data["expires_in"])),
            first_auth_at=cache.first_auth_at,
            last_refresh_at=now,
            user_id=data.get("user_id") or cache.user_id,
        )
        self._cache = new_cache
        save_token_cache(new_cache)
        return new_cache

    def access_token(self) -> str:
        """Return a valid access token, refreshing with a 60s buffer."""
        cache = self._cache or load_token_cache()
        if cache is None:
            raise AuthExpiredError(
                "Monzo token cache is empty — run `granite ops reauth monzo`",
                source=SOURCE_ID,
            )
        self._cache = cache
        if cache.is_expiring_soon(now=self._clock(), within=timedelta(seconds=60)):
            cache = self.refresh()
        return cache.access_token

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _http_client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(
            base_url=MONZO_API_BASE,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
        )
        return self._http

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        response = self._http_client().post(MONZO_TOKEN_PATH, data=payload)
        if response.status_code in (400, 401, 403):
            # invalid_grant / expired refresh token / bad client creds.
            try:
                body = response.json()
            except ValueError:
                body = None
            # Never leak client_secret or tokens into exception details.
            raise AuthExpiredError(
                f"Monzo token endpoint returned {response.status_code}",
                source=SOURCE_ID,
                details={"error_code": _extract_error_code(body)},
            )
        if response.status_code >= 500 or response.status_code == 429:
            raise RateLimitedError(
                f"Monzo token endpoint returned {response.status_code}",
                source=SOURCE_ID,
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "access_token" not in data:
            raise SchemaViolationError(
                "Monzo token endpoint returned unexpected payload",
                source=SOURCE_ID,
            )
        return data


# ---------------------------------------------------------------------------
# Local callback server (one-shot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallbackResult:
    code: str
    state: str


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures ``?code=...&state=...``."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path != "/callback" or "code" not in params or "state" not in params:
            self.send_response(404)
            self.end_headers()
            return
        self.server._captured = CallbackResult(  # type: ignore[attr-defined]
            code=params["code"][0], state=params["state"][0]
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Monzo authorisation received.</h1>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, format: str, *args: Any) -> None:
        # The default implementation writes to stderr and would clutter
        # agent-native JSON stdout. Silence it — pipeline logging is
        # done via structlog upstream. ``format`` / ``args`` intentionally
        # shadow http.server's signature.
        del format, args


def run_callback_server(
    *,
    expected_state: str,
    host: str = DEFAULT_REDIRECT_HOST,
    port: int = DEFAULT_REDIRECT_PORT,
    timeout_seconds: float = 300.0,
) -> CallbackResult:
    """Block until a valid callback arrives or raise :class:`AuthExpiredError`.

    Runs one request loop then shuts down. Validates that the returned
    ``state`` matches the ``expected_state`` token we generated — this
    protects the callback against CSRF / forged redirects.
    """
    try:
        server = HTTPServer((host, port), _CallbackHandler)
    except OSError as err:
        raise ConfigError(
            f"Could not bind localhost callback on {host}:{port}: {err}",
            source=SOURCE_ID,
            cause=err,
        ) from err
    server._captured = None  # type: ignore[attr-defined]
    server.timeout = timeout_seconds

    thread = threading.Thread(target=server.serve_forever, name="monzo-callback")
    thread.daemon = True
    thread.start()
    ticker = threading.Event()
    try:
        deadline = datetime.now(tz=UTC) + timedelta(seconds=timeout_seconds)
        while datetime.now(tz=UTC) < deadline:
            if server._captured is not None:  # type: ignore[attr-defined]
                break
            ticker.wait(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    captured: CallbackResult | None = server._captured  # type: ignore[attr-defined]
    if captured is None:
        raise AuthExpiredError(
            "Monzo OAuth callback timed out — browser step was not completed",
            source=SOURCE_ID,
        )
    if captured.state != expected_state:
        raise AuthExpiredError(
            "Monzo OAuth callback state mismatch — possible CSRF, refusing",
            source=SOURCE_ID,
        )
    return captured


def find_free_port(*, host: str = DEFAULT_REDIRECT_HOST) -> int:
    """Return an OS-assigned free port on ``host``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def new_state_token() -> str:
    """Return a URL-safe CSRF state token for the OAuth flow."""
    return rnd.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MonzoAdapter:
    """Monzo OAuth-gated transaction adapter."""

    source_id: str = SOURCE_ID

    def __init__(
        self,
        *,
        auth: MonzoAuth,
        http: httpx.Client | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> None:
        self._auth = auth
        self._http = http
        self._batch_size = batch_size
        self._window_days = window_days

    def _client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(
            base_url=MONZO_API_BASE,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
        )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def list_accounts(self) -> list[MonzoAccount]:
        response = self._request("GET", MONZO_ACCOUNTS_PATH)
        payload = response.json()
        accounts = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(accounts, list):
            raise SchemaViolationError(
                "Monzo /accounts returned an unexpected payload",
                source=SOURCE_ID,
            )
        return [_parse_account(raw) for raw in accounts if not raw.get("closed")]

    def fetch_transactions(
        self,
        *,
        account_id: str,
        since: datetime,
        before: datetime,
    ) -> list[dict[str, Any]]:
        params = {
            "account_id": account_id,
            "since": _iso_z(since),
            "before": _iso_z(before),
            "expand[]": "merchant",
            "limit": "100",
        }
        response = self._request("GET", MONZO_TRANSACTIONS_PATH, params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SchemaViolationError(
                "Monzo /transactions returned a non-dict payload",
                source=SOURCE_ID,
            )
        txns = payload.get("transactions")
        if not isinstance(txns, list):
            raise SchemaViolationError(
                "Monzo /transactions missing transactions[]",
                source=SOURCE_ID,
            )
        return txns

    def fetch_since(
        self,
        watermark: str | None,
        *,
        now: datetime | None = None,
    ) -> Iterator[list[RawTransaction]]:
        """Yield batches of :class:`RawTransaction` since ``watermark``."""
        self._last_watermark: str | None = None
        current = now or datetime.now(tz=UTC)
        lower_floor = current - timedelta(days=self._window_days)
        if watermark:
            try:
                parsed = _parse_iso(watermark)
            except ValueError as err:
                raise SchemaViolationError(
                    f"bad Monzo watermark {watermark!r}",
                    source=SOURCE_ID,
                    cause=err,
                ) from err
            since = max(parsed, lower_floor)
        else:
            since = lower_floor

        accounts = self.list_accounts()
        buffered: list[RawTransaction] = []
        for account in accounts:
            raws = self.fetch_transactions(
                account_id=account.account_id,
                since=since,
                before=current,
            )
            for raw in raws:
                parsed_txn = _parse_transaction(raw, account=account)
                if parsed_txn is None:
                    continue
                buffered.append(parsed_txn)
                if len(buffered) >= self._batch_size:
                    yield buffered
                    buffered = []
        if buffered:
            yield buffered

        self._last_watermark = _iso_z(current)

    @property
    def next_watermark(self) -> str | None:
        return getattr(self, "_last_watermark", None)

    def reauth(self) -> None:
        """Interactive re-auth lives on the CLI — see ``granite ops reauth monzo``."""
        raise ConfigError(
            "Monzo re-auth is an interactive OAuth flow; run "
            "`granite ops reauth monzo` rather than calling reauth() "
            "programmatically.",
            source=SOURCE_ID,
        )

    # ------------------------------------------------------------------
    # Internal transport
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        client = self._client()
        token = self._auth.access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        response = client.request(method, path, params=params, headers=headers)
        if response.status_code == 401:
            # Access token may have expired between our refresh check
            # and the request. Force a refresh and retry once.
            self._auth.refresh()
            token = self._auth.access_token()
            headers["Authorization"] = f"Bearer {token}"
            response = client.request(method, path, params=params, headers=headers)
        _raise_for_monzo_status(response)
        return response


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_account(raw: dict[str, Any]) -> MonzoAccount:
    account_id = raw.get("id")
    if not isinstance(account_id, str) or not account_id:
        raise SchemaViolationError(
            "Monzo account missing id",
            source=SOURCE_ID,
        )
    account_type = str(raw.get("type") or "unknown")
    currency = (raw.get("currency") or "GBP").upper()
    description = str(raw.get("description") or account_id)
    return MonzoAccount(
        account_id=account_id,
        description=description,
        currency=currency,
        account_type=account_type,
    )


def _parse_transaction(
    raw: dict[str, Any],
    *,
    account: MonzoAccount,
) -> RawTransaction | None:
    txn_id = raw.get("id")
    if not isinstance(txn_id, str) or not txn_id:
        return None

    # ``created`` is the booking time; ``settled`` is non-empty when the
    # transaction has settled. Declined transactions carry a
    # ``decline_reason`` and should be ignored entirely.
    if raw.get("decline_reason"):
        return None

    created = raw.get("created")
    if not isinstance(created, str):
        return None
    try:
        booking_dt = _parse_iso(created)
    except ValueError:
        return None

    # Monzo returns amount as an integer minor unit (pence for GBP).
    amount_minor = raw.get("amount")
    if not isinstance(amount_minor, int):
        # Some test-mode payloads stringify it; be forgiving.
        try:
            amount_minor = int(amount_minor)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    currency = str(raw.get("currency") or account.currency).upper()
    try:
        amount = (Decimal(amount_minor) / Decimal("100")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None

    description = _choose_description(raw)
    if not description:
        return None
    description_canonical = canonicalise_description(description)

    settled_at = raw.get("settled")
    status = "settled" if isinstance(settled_at, str) and settled_at else "pending"

    provider_auth_id = _extract_auth_id(raw)
    category_hint = _coerce_str(raw.get("category")) or None
    reference = provider_auth_id or txn_id

    return RawTransaction(
        txn_id=_compute_txn_id(account=f"{ACCOUNT_LABEL}-{account.currency}", txn_id=txn_id),
        account=f"{ACCOUNT_LABEL}-{account.currency}",
        booking_date=booking_dt.date(),
        description_raw=description,
        description_canonical=description_canonical,
        currency=currency,
        amount=to_money(amount, currency),
        reference=reference,
        category_hint=category_hint,
        status=status,
        provider_auth_id=provider_auth_id,
    )


def _choose_description(raw: dict[str, Any]) -> str:
    merchant = raw.get("merchant")
    if isinstance(merchant, dict):
        name = merchant.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    counterparty = raw.get("counterparty")
    if isinstance(counterparty, dict):
        name = counterparty.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    description = raw.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    notes = raw.get("notes")
    if isinstance(notes, str) and notes.strip():
        return notes.strip()
    return ""


def _extract_auth_id(raw: dict[str, Any]) -> str | None:
    """Monzo's per-authorization identifier.

    For card payments Monzo surfaces ``atm_fees_detailed`` /
    ``scheme`` fields along with a ``local_amount`` block; the
    stable identifier across pending→settled is the top-level ``id``
    (which doesn't change when settled). We prefer ``scheme_reference``
    from the merchant block when present, otherwise fall back to the
    transaction id itself.
    """
    merchant = raw.get("merchant")
    if isinstance(merchant, dict):
        ref = merchant.get("scheme_reference")
        if isinstance(ref, str) and ref:
            return ref
    top_level = raw.get("id")
    return top_level if isinstance(top_level, str) else None


def _compute_txn_id(*, account: str, txn_id: str) -> str:
    import hashlib

    payload = f"{account}\x00{txn_id}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTTP status translation + small helpers
# ---------------------------------------------------------------------------


def _raise_for_monzo_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:200]
    if response.status_code in (401, 403):
        raise AuthExpiredError(
            f"Monzo returned {response.status_code}",
            source=SOURCE_ID,
            details={"error_code": _extract_error_code(body)},
        )
    if response.status_code in (429, 500, 502, 503, 504):
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"Monzo returned {response.status_code}",
            source=SOURCE_ID,
            details={"retry_after": retry_after},
        )
    raise SchemaViolationError(
        f"Monzo returned unexpected status {response.status_code}",
        source=SOURCE_ID,
        details={"status": response.status_code, "error_code": _extract_error_code(body)},
    )


def _extract_error_code(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("code", "error", "error_description"):
            value = body.get(key)
            if isinstance(value, str):
                return value
    return None


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        raise SchemaViolationError(
            f"naive datetime {value!r} supplied to Monzo window",
            source=SOURCE_ID,
        )
    stamped = value.astimezone(UTC).replace(tzinfo=None)
    return stamped.isoformat(timespec="milliseconds") + "Z"


def _parse_iso(value: str) -> datetime:
    cleaned = value
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return datetime.fromisoformat(cleaned)


__all__ = [
    "ACCOUNT_LABEL",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_REDIRECT_HOST",
    "DEFAULT_REDIRECT_PORT",
    "DEFAULT_REDIRECT_URI",
    "DEFAULT_WINDOW_DAYS",
    "MONZO_ACCOUNTS_PATH",
    "MONZO_API_BASE",
    "MONZO_AUTH_URL",
    "MONZO_TOKEN_PATH",
    "MONZO_TRANSACTIONS_PATH",
    "REFRESH_TOKEN_WARN_DAYS",
    "SECRETS_NAMESPACE",
    "SOURCE_ID",
    "CallbackResult",
    "MonzoAccount",
    "MonzoAdapter",
    "MonzoAuth",
    "RawTransaction",
    "TokenCache",
    "find_free_port",
    "load_token_cache",
    "new_state_token",
    "run_callback_server",
    "save_token_cache",
]
