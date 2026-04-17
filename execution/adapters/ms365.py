"""Microsoft 365 Graph mail adapter.

Reads the user's inbox via the delegated Graph API. First-run auth uses
MSAL's device-code flow (user copies a short code into a browser page
— no local HTTP redirect server needed, works over SSH). The refresh
token is cached in the system Keychain under
``granite-accounts/ms365/refresh_token``; the msal token cache
serialisation is persisted so access tokens and account identities
survive restarts.

Incremental fetch is a delta query on the inbox
(``/me/mailFolders/inbox/messages/delta``). The ``@odata.deltaLink``
is stored per-adapter in the ``watermarks`` table and replayed on the
next run, so a crash mid-run leaves state consistent at the last
completed batch.

Transport rules (plan § Reliability Contracts):

- ``401`` → :class:`AuthExpiredError` (not retryable; caller writes a
  ``reauth_required`` row).
- ``429`` / ``503`` → :class:`RateLimitedError`; the outer tenacity
  wrapper decides retry-or-skip based on ``Retry-After``.
- Other 4xx → raise; they aren't transient.

PHI / email bodies are sensitive. We fetch the fields we need (id,
subject, sender, received, internetMessageId, hasAttachments,
bodyPreview) and never log bodies. Full body download happens later
only when the classifier + extractor ask for it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from execution.shared import secrets
from execution.shared.errors import (
    AuthExpiredError,
    ConfigError,
    RateLimitedError,
    SchemaViolationError,
)

if TYPE_CHECKING:  # pragma: no cover
    import httpx
    import msal

SOURCE_ID: Final[str] = "ms365"
SECRETS_NAMESPACE: Final[str] = "ms365"

DEFAULT_SCOPES: Final[tuple[str, ...]] = (
    "Mail.Read",
    "offline_access",
)
AUTHORITY: Final[str] = "https://login.microsoftonline.com/common"
GRAPH_BASE: Final[str] = "https://graph.microsoft.com/v1.0"
INBOX_DELTA_URL: Final[str] = f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta"

SELECT_FIELDS: Final[str] = ",".join(
    [
        "id",
        "subject",
        "from",
        "receivedDateTime",
        "hasAttachments",
        "internetMessageId",
        "bodyPreview",
    ]
)

DEFAULT_BATCH_SIZE: Final[int] = 50
DEFAULT_PAGE_SIZE: Final[int] = 100  # Graph $top ceiling is 999; 100 keeps pages small


@dataclass(frozen=True, slots=True)
class RawEmail:
    """Normalised envelope for a single message."""

    msg_id: str
    internet_message_id: str | None
    subject: str
    from_addr: str
    received_at: datetime
    has_attachments: bool
    body_preview: str

    def as_email_row(self) -> dict[str, Any]:
        """Columns used by the classifier's DB write."""
        return {
            "msg_id": self.msg_id,
            "source_adapter": SOURCE_ID,
            "message_id_header": self.internet_message_id,
            "received_at": self.received_at.isoformat(),
            "from_addr": self.from_addr,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class FetchStats:
    """Per-run summary the orchestrator records on ``runs.stats_json``."""

    batches: int
    emails: int
    next_watermark: str | None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class Ms365Auth:
    """Wraps the MSAL public-client app + a Keychain-backed token cache.

    Exposes just what the adapter needs: ``access_token()`` which returns
    a usable bearer token (refreshing silently if possible, raising
    :class:`AuthExpiredError` when interactive re-auth is required).
    """

    def __init__(
        self,
        *,
        client_id: str,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        msal_app: msal.PublicClientApplication | None = None,
    ) -> None:
        self.client_id = client_id
        self.scopes = scopes
        self._cache_loaded = False
        self._msal_app_override = msal_app

    @classmethod
    def from_keychain(cls) -> Ms365Auth:
        """Load client_id from Keychain; fail loudly if not configured."""
        if secrets.is_mock():
            raise ConfigError(
                "Ms365Auth.from_keychain() under MOCK_MODE — inject a fake auth "
                "via the adapter's client= argument.",
                source=SOURCE_ID,
            )
        client_id = secrets.require(SECRETS_NAMESPACE, "client_id")
        return cls(client_id=client_id)

    def _app(self) -> msal.PublicClientApplication:
        if self._msal_app_override is not None:
            return self._msal_app_override
        import msal

        cache = msal.SerializableTokenCache()
        cached = secrets.get(SECRETS_NAMESPACE, "token_cache")
        if cached:
            cache.deserialize(cached)
            self._cache_loaded = True
        return msal.PublicClientApplication(
            client_id=self.client_id,
            authority=AUTHORITY,
            token_cache=cache,
        )

    def _persist_cache(self, app: msal.PublicClientApplication) -> None:
        cache = app.token_cache
        if cache.has_state_changed:
            secrets.put(SECRETS_NAMESPACE, "token_cache", cache.serialize())

    def access_token(self) -> str:
        """Return a usable access token, refreshing if needed.

        Raises :class:`AuthExpiredError` when no cached account is
        available or the refresh fails — the orchestrator treats that as
        a ``reauth_required`` state, not a retryable error.
        """
        app = self._app()
        result: dict[str, Any] | None = None

        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(
                scopes=list(self.scopes), account=accounts[0]
            )
        if not result or "access_token" not in result:
            raise AuthExpiredError(
                "MS Graph silent token refresh failed; run `granite ops reauth ms365`",
                source=SOURCE_ID,
                details={"error": result.get("error") if result else None},
            )
        self._persist_cache(app)
        return str(result["access_token"])

    def initiate_device_flow(self) -> dict[str, Any]:
        """Start a device-code flow; returns the payload containing user_code + url."""
        app = self._app()
        flow = app.initiate_device_flow(scopes=list(self.scopes))
        if "user_code" not in flow:
            raise ConfigError(
                "MS Graph device flow failed to start",
                source=SOURCE_ID,
                details=flow,
            )
        # Stash the flow so complete_device_flow() can finish it.
        self._pending_flow = flow
        self._pending_app = app
        return flow

    def complete_device_flow(self) -> None:
        """Block on the user completing the device flow; persist tokens."""
        if not getattr(self, "_pending_flow", None):
            raise ConfigError(
                "complete_device_flow() called without initiate_device_flow()",
                source=SOURCE_ID,
            )
        result = self._pending_app.acquire_token_by_device_flow(self._pending_flow)
        if "access_token" not in result:
            raise AuthExpiredError(
                "MS Graph device flow did not yield a token",
                source=SOURCE_ID,
                details={"error": result.get("error")},
            )
        self._persist_cache(self._pending_app)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class Ms365Adapter:
    """MS Graph inbox adapter."""

    source_id: str = SOURCE_ID

    def __init__(
        self,
        *,
        auth: Ms365Auth,
        http: httpx.Client | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._auth = auth
        self._http = http
        self._batch_size = batch_size
        self._page_size = page_size

    def _client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=httpx.Timeout(connect=5.0, read=60.0))
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def fetch_since(self, watermark: str | None) -> Iterator[list[RawEmail]]:
        """Yield batches of :class:`RawEmail` from the inbox delta query.

        ``watermark`` is the last ``@odata.deltaLink`` or ``None`` for an
        initial sync. The final delta link (only present on the last
        page) is attached to each batch via the adapter's
        :attr:`next_watermark` attribute once iteration completes.
        """
        self._last_watermark: str | None = None
        token = self._auth.access_token()
        client = self._client()

        url = watermark or INBOX_DELTA_URL
        params: dict[str, str] = (
            {} if watermark else {"$top": str(self._page_size), "$select": SELECT_FIELDS}
        )

        buffered: list[RawEmail] = []
        while url:
            response = client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Prefer": 'odata.maxpagesize="' + str(self._page_size) + '"',
                },
            )
            _raise_for_graph_status(response)
            payload = response.json()
            for raw in payload.get("value", []):
                parsed = _parse_graph_message(raw)
                if parsed is None:
                    continue
                buffered.append(parsed)
                if len(buffered) >= self._batch_size:
                    yield buffered
                    buffered = []
            url = payload.get("@odata.nextLink")
            params = {}  # nextLink already carries params
            delta = payload.get("@odata.deltaLink")
            if delta is not None:
                self._last_watermark = delta

        if buffered:
            yield buffered

    @property
    def next_watermark(self) -> str | None:
        """Delta link from the most recent fetch, or ``None`` if fetch not run."""
        return getattr(self, "_last_watermark", None)

    def reauth(self) -> None:
        """Run the device-code flow end-to-end."""
        flow = self._auth.initiate_device_flow()
        # The caller (granite ops reauth ms365) prints flow['message'] to
        # the user's terminal; we just block on completion.
        del flow
        self._auth.complete_device_flow()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raise_for_graph_status(response: httpx.Response) -> None:
    """Translate Graph HTTP statuses into the PipelineError hierarchy."""
    if response.status_code < 400:
        return
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        body = response.text[:200]
    if response.status_code == 401:
        raise AuthExpiredError(
            "MS Graph returned 401 — refresh token invalid",
            source=SOURCE_ID,
            details={"body": body},
        )
    if response.status_code == 403:
        raise AuthExpiredError(
            "MS Graph returned 403 — consent revoked",
            source=SOURCE_ID,
            details={"body": body},
        )
    if response.status_code in (429, 503):
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"MS Graph returned {response.status_code}",
            source=SOURCE_ID,
            details={"retry_after": retry_after, "body": body},
        )
    raise SchemaViolationError(
        f"MS Graph returned unexpected status {response.status_code}",
        source=SOURCE_ID,
        details={"status": response.status_code, "body": body},
    )


def _parse_graph_message(raw: dict[str, Any]) -> RawEmail | None:
    """Normalise a Graph message into :class:`RawEmail`.

    Returns ``None`` for entries that aren't real messages (Graph's delta
    feed sometimes sends ``@removed`` entries).
    """
    if raw.get("@removed"):
        return None
    msg_id = raw.get("id")
    if not msg_id:
        return None
    subject = str(raw.get("subject") or "")
    from_addr = _extract_from(raw.get("from"))
    received_raw = raw.get("receivedDateTime")
    if not received_raw:
        return None
    received = _parse_graph_datetime(received_raw)
    return RawEmail(
        msg_id=str(msg_id),
        internet_message_id=(
            str(raw["internetMessageId"]) if raw.get("internetMessageId") else None
        ),
        subject=subject,
        from_addr=from_addr,
        received_at=received,
        has_attachments=bool(raw.get("hasAttachments")),
        body_preview=str(raw.get("bodyPreview") or ""),
    )


def _extract_from(field: Any) -> str:
    if not field or not isinstance(field, dict):
        return ""
    email_addr = field.get("emailAddress") or {}
    return str(email_addr.get("address") or "")


def _parse_graph_datetime(value: str) -> datetime:
    """Graph returns RFC 3339 with a ``Z`` or ``+00:00`` suffix."""
    cleaned = value.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


__all__ = [
    "AUTHORITY",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SCOPES",
    "GRAPH_BASE",
    "INBOX_DELTA_URL",
    "SECRETS_NAMESPACE",
    "SELECT_FIELDS",
    "SOURCE_ID",
    "FetchStats",
    "Ms365Adapter",
    "Ms365Auth",
    "RawEmail",
]
