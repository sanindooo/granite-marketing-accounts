"""Wise (TransferWise) banking adapter.

Wise exposes a personal-token API with mandatory **Strong Customer
Authentication** (SCA) on any endpoint that returns account-holder or
statement data. The shape of an SCA-gated call is:

1. Send the request with the normal ``Authorization: Bearer <token>``.
2. Wise responds ``403 Forbidden`` with a one-time challenge header
   ``x-2fa-approval`` and ``x-2fa-approval-result: REJECTED``.
3. We sign the challenge string (the UUID-ish value in
   ``x-2fa-approval``) with an RSA private key — the matching public
   key is pre-registered in the developer portal.
4. Replay the identical request with two extra headers:
   ``x-2fa-approval: <same challenge>`` and
   ``X-Signature: <base64(SHA256-RSA(challenge))>``.
5. Wise returns 200 with the payload.

Design decisions carried forward from the plan:

- Private key lives in the macOS Keychain under
  ``granite-accounts/wise/private_key_pem`` (namespace ``wise``). It
  never touches disk. Bearer token lives at ``wise/api_token``.
- The pipeline only needs read-only endpoints: ``/v2/profiles`` to
  enumerate profiles and
  ``/v1/profiles/{profile_id}/borderless-accounts/{account_id}/statement.json``
  to pull a windowed statement. Statement currencies other than GBP are
  handed back verbatim; the ledger normaliser does the FX conversion.
- SCA retry is a deliberate single-shot: if the signed replay still
  fails with 403 we raise :class:`AuthExpiredError` — running the
  signing logic twice against the same challenge indicates a
  key/key-pair mismatch, not a transient condition.
- Upstream 429 / 5xx map to :class:`RateLimitedError` (retryable);
  everything 4xx except 401/403 maps to :class:`SchemaViolationError`.
- The adapter is intentionally **generator-of-batches** shaped so the
  orchestrator can commit per-batch and advance the watermark without
  holding a full month of transactions in memory.

Security rails:

- We never log the bearer token or the challenge; only the response
  status and the challenge length (for diagnostics).
- The signing helper takes the raw PEM bytes, loads with
  ``cryptography.hazmat.primitives.serialization.load_pem_private_key``
  (``password=None`` — the plan stores unencrypted PEMs because the
  Keychain entry itself is the protection boundary), and produces a
  base64 SHA-256-RSA signature against the challenge bytes.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from execution.adapters.amex_csv import canonicalise_description
from execution.shared import secrets
from execution.shared.errors import (
    AuthExpiredError,
    ConfigError,
    DataQualityError,
    RateLimitedError,
    SchemaViolationError,
)
from execution.shared.money import to_money

if TYPE_CHECKING:  # pragma: no cover
    import httpx

SOURCE_ID: Final[str] = "wise"
SECRETS_NAMESPACE: Final[str] = "wise"
ACCOUNT_LABEL: Final[str] = "wise"

WISE_API_BASE: Final[str] = "https://api.wise.com"
WISE_PROFILES_PATH: Final[str] = "/v2/profiles"
DEFAULT_BATCH_SIZE: Final[int] = 50
DEFAULT_WINDOW_DAYS: Final[int] = 60  # matches plan's sliding-window rule

SCA_HEADER: Final[str] = "x-2fa-approval"
SCA_SIGNATURE_HEADER: Final[str] = "X-Signature"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WiseProfile:
    """A single Wise profile — a personal or business identity."""

    profile_id: int
    profile_type: str  # "personal" | "business"
    name: str


@dataclass(frozen=True, slots=True)
class WiseAccount:
    """One multi-currency balance within a profile."""

    account_id: int
    profile_id: int
    currency: str
    name: str


@dataclass(frozen=True, slots=True)
class RawTransaction:
    """Statement line normalised onto the ledger's ``RawTransaction`` shape."""

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


@dataclass(frozen=True, slots=True)
class WiseFetchStats:
    """Summary of one statement fetch — emitted to Run Status."""

    profiles: int
    accounts: int
    statement_fetches: int
    transactions: int


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------


class WiseSigner:
    """Signs SCA challenges with the Keychain-stored RSA private key.

    Tests inject a deterministic signer via ``Ms365Adapter(signer=...)``
    equivalent wiring here; production loads the PEM from Keychain on
    first sign.
    """

    def __init__(self, *, private_key_pem: bytes) -> None:
        if not private_key_pem.strip():
            raise ConfigError(
                "Wise private key PEM is empty",
                source=SOURCE_ID,
            )
        # Defer the cryptography import so tests that stub out signing
        # don't pay the import cost.
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        self._key = load_pem_private_key(private_key_pem, password=None)

    @classmethod
    def from_keychain(cls) -> WiseSigner:
        if secrets.is_mock():
            raise ConfigError(
                "WiseSigner.from_keychain() under MOCK_MODE — inject a signer= "
                "argument on the adapter.",
                source=SOURCE_ID,
            )
        pem = secrets.require(SECRETS_NAMESPACE, "private_key_pem")
        return cls(private_key_pem=pem.encode("utf-8"))

    def sign(self, challenge: str) -> str:
        """Return the base64-encoded RSA-PKCS1v15-SHA256 signature."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        if not isinstance(self._key, rsa.RSAPrivateKey):
            raise ConfigError(
                "Wise private key is not RSA — Wise only accepts RSA keys",
                source=SOURCE_ID,
            )
        signature = self._key.sign(
            challenge.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class WiseAuth:
    """Bearer token + RSA signer bundled together."""

    def __init__(
        self,
        *,
        api_token: str,
        signer: WiseSigner,
    ) -> None:
        if not api_token.strip():
            raise ConfigError(
                "Wise API token is empty",
                source=SOURCE_ID,
            )
        self._api_token = api_token.strip()
        self._signer = signer

    @classmethod
    def from_keychain(cls) -> WiseAuth:
        if secrets.is_mock():
            raise ConfigError(
                "WiseAuth.from_keychain() under MOCK_MODE — inject auth= "
                "explicitly on the adapter.",
                source=SOURCE_ID,
            )
        token = secrets.require(SECRETS_NAMESPACE, "api_token")
        signer = WiseSigner.from_keychain()
        return cls(api_token=token, signer=signer)

    def authorization_header(self) -> str:
        return f"Bearer {self._api_token}"

    def sign_challenge(self, challenge: str) -> str:
        return self._signer.sign(challenge)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class WiseAdapter:
    """Wise SCA-gated statement adapter."""

    source_id: str = SOURCE_ID

    def __init__(
        self,
        *,
        auth: WiseAuth,
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
            base_url=WISE_API_BASE,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
        )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def list_profiles(self) -> list[WiseProfile]:
        """GET /v2/profiles — returns the caller's Wise profiles."""
        response = self._request("GET", WISE_PROFILES_PATH)
        payload = response.json()
        if not isinstance(payload, list):
            raise SchemaViolationError(
                "Wise /v2/profiles returned a non-list payload",
                source=SOURCE_ID,
                details={"type": type(payload).__name__},
            )
        return [_parse_profile(raw) for raw in payload]

    def list_accounts(self, profile_id: int) -> list[WiseAccount]:
        """GET /v4/profiles/{id}/balances?types=STANDARD — multi-currency balances."""
        path = f"/v4/profiles/{profile_id}/balances"
        response = self._request("GET", path, params={"types": "STANDARD"})
        payload = response.json()
        if not isinstance(payload, list):
            raise SchemaViolationError(
                "Wise balances endpoint returned a non-list payload",
                source=SOURCE_ID,
                details={"type": type(payload).__name__},
            )
        return [_parse_account(raw, profile_id=profile_id) for raw in payload]

    def fetch_statement(
        self,
        *,
        profile_id: int,
        account_id: int,
        currency: str,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch a windowed statement from a borderless account."""
        path = (
            f"/v1/profiles/{profile_id}/borderless-accounts/"
            f"{account_id}/statement.json"
        )
        params = {
            "currency": currency,
            "intervalStart": _iso_z(since),
            "intervalEnd": _iso_z(until),
            "type": "COMPACT",
        }
        response = self._request("GET", path, params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SchemaViolationError(
                "Wise statement endpoint returned a non-dict payload",
                source=SOURCE_ID,
                details={"type": type(payload).__name__},
            )
        transactions = payload.get("transactions")
        if not isinstance(transactions, list):
            raise SchemaViolationError(
                "Wise statement payload missing transactions[]",
                source=SOURCE_ID,
            )
        return transactions

    def fetch_since(
        self,
        watermark: str | None,
        *,
        now: datetime | None = None,
    ) -> Iterator[list[RawTransaction]]:
        """Yield batches of :class:`RawTransaction` since ``watermark``.

        ``watermark`` is an ISO-8601 timestamp. On the first run we fall
        back to ``now - window_days``. Per the plan we clamp the lower
        bound to ``max(watermark, now - window_days)`` so a 3-week
        laptop-sleep still triggers a full sliding-window pull.
        """
        self._last_watermark: str | None = None
        current = now or datetime.now(tz=UTC)
        lower_floor = current - timedelta(days=self._window_days)
        if watermark:
            try:
                parsed = _parse_iso(watermark)
            except ValueError as err:
                raise SchemaViolationError(
                    f"bad Wise watermark {watermark!r}",
                    source=SOURCE_ID,
                    cause=err,
                ) from err
            since = max(parsed, lower_floor)
        else:
            since = lower_floor

        profiles = self.list_profiles()
        buffered: list[RawTransaction] = []
        transactions_seen = 0
        for profile in profiles:
            accounts = self.list_accounts(profile.profile_id)
            for account in accounts:
                raws = self.fetch_statement(
                    profile_id=profile.profile_id,
                    account_id=account.account_id,
                    currency=account.currency,
                    since=since,
                    until=current,
                )
                for raw in raws:
                    parsed_txn = _parse_transaction(raw, account=account)
                    if parsed_txn is None:
                        continue
                    transactions_seen += 1
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
        """No interactive re-auth — Wise tokens rotate via the developer portal."""
        raise ConfigError(
            "Wise uses static API tokens + developer-portal key rotation. "
            "Follow directives/reauth.md -> wise to rotate.",
            source=SOURCE_ID,
        )

    # ------------------------------------------------------------------
    # Internal transport — the SCA dance
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute one request, completing the SCA handshake if triggered."""
        client = self._client()
        headers = {
            "Authorization": self._auth.authorization_header(),
            "Accept": "application/json",
        }
        response = client.request(method, path, params=params, headers=headers)
        if response.status_code != 403:
            _raise_for_wise_status(response)
            return response

        challenge = response.headers.get(SCA_HEADER)
        if not challenge:
            # Not an SCA challenge — this is a normal 403.
            _raise_for_wise_status(response)
            return response

        signature = self._auth.sign_challenge(challenge)
        signed_headers = {
            **headers,
            SCA_HEADER: challenge,
            SCA_SIGNATURE_HEADER: signature,
        }
        signed_response = client.request(
            method, path, params=params, headers=signed_headers
        )
        if signed_response.status_code == 403:
            raise AuthExpiredError(
                "Wise SCA signing failed — the public key registered in "
                "the developer portal does not match the Keychain private key",
                source=SOURCE_ID,
                details={
                    "path": path,
                    "challenge_len": len(challenge),
                    "status": signed_response.status_code,
                },
            )
        _raise_for_wise_status(signed_response)
        return signed_response


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_profile(raw: dict[str, Any]) -> WiseProfile:
    try:
        profile_id = int(raw["id"])
    except (KeyError, TypeError, ValueError) as err:
        raise SchemaViolationError(
            "Wise profile missing id",
            source=SOURCE_ID,
            details={"raw": {k: raw.get(k) for k in ("id", "type") if k in raw}},
            cause=err,
        ) from err
    profile_type = str(raw.get("type") or "").lower() or "personal"
    # Newer /v2/profiles returns details under `fullName`; legacy `name`
    # fallback keeps the parser tolerant.
    name = (
        str(raw.get("fullName") or "").strip()
        or str(raw.get("name") or "").strip()
        or f"profile-{profile_id}"
    )
    return WiseProfile(
        profile_id=profile_id,
        profile_type=profile_type,
        name=name,
    )


def _parse_account(raw: dict[str, Any], *, profile_id: int) -> WiseAccount:
    try:
        account_id = int(raw["id"])
    except (KeyError, TypeError, ValueError) as err:
        raise SchemaViolationError(
            "Wise account missing id",
            source=SOURCE_ID,
            cause=err,
        ) from err
    currency = str(raw.get("currency") or "").upper()
    if len(currency) != 3:
        raise SchemaViolationError(
            f"Wise account has implausible currency {currency!r}",
            source=SOURCE_ID,
            details={"account_id": account_id, "currency": currency},
        )
    name = str(raw.get("name") or f"{currency}-balance")
    return WiseAccount(
        account_id=account_id,
        profile_id=profile_id,
        currency=currency,
        name=name,
    )


def _parse_transaction(
    raw: dict[str, Any],
    *,
    account: WiseAccount,
) -> RawTransaction | None:
    reference_number = _coerce_str(raw.get("referenceNumber"))
    details = raw.get("details") or {}
    description = _coerce_str(details.get("description")) or _coerce_str(
        raw.get("description")
    )
    if not description:
        return None

    date_raw = raw.get("date") or raw.get("completedOn")
    if not isinstance(date_raw, str):
        return None
    try:
        booking_date = _parse_iso(date_raw).date()
    except ValueError:
        return None

    # Wise returns ``amount`` as {"value": 12.34, "currency": "GBP"}.
    amount_obj = raw.get("amount")
    if not isinstance(amount_obj, dict):
        return None
    amount = _decimal_from(amount_obj.get("value"))
    if amount is None:
        return None
    currency = str(amount_obj.get("currency") or account.currency).upper()

    txn_type_raw = str(raw.get("type") or "").upper()
    if txn_type_raw == "DEBIT" and amount > 0:
        amount = -amount

    description_canonical = canonicalise_description(description)
    status = str(raw.get("status") or "COMPLETED").upper()
    provider_auth_id = (
        reference_number
        or _coerce_str(raw.get("id"))
        or _coerce_str(raw.get("activityId"))
    )

    # Stable txn_id: Wise's referenceNumber when present (unique per
    # account per posting), else the internal id.
    txn_id = _compute_txn_id(
        account=f"{ACCOUNT_LABEL}-{account.currency}",
        provider_auth_id=provider_auth_id,
        booking_date=booking_date,
        canonical_description=description_canonical,
        amount=amount,
    )

    return RawTransaction(
        txn_id=txn_id,
        account=f"{ACCOUNT_LABEL}-{account.currency}",
        booking_date=booking_date,
        description_raw=description,
        description_canonical=description_canonical,
        currency=currency,
        amount=to_money(amount, currency),
        reference=reference_number,
        category_hint=None,
        status="pending" if status == "PENDING" else "settled",
        provider_auth_id=provider_auth_id,
    )


def _compute_txn_id(
    *,
    account: str,
    provider_auth_id: str | None,
    booking_date: date,
    canonical_description: str,
    amount: Decimal,
) -> str:
    import hashlib

    if provider_auth_id:
        payload = f"{account}\x00{provider_auth_id}".encode()
        return hashlib.sha256(payload).hexdigest()[:16]
    payload = (
        f"{account}\x00"
        f"{booking_date.isoformat()}\x00"
        f"{canonical_description}\x00"
        f"{format(amount, 'f')}"
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTTP status translation + small helpers
# ---------------------------------------------------------------------------


def _raise_for_wise_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text[:200]
    if response.status_code in (401, 403):
        raise AuthExpiredError(
            f"Wise returned {response.status_code}",
            source=SOURCE_ID,
            details={"body": body},
        )
    if response.status_code in (429, 500, 502, 503, 504):
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"Wise returned {response.status_code}",
            source=SOURCE_ID,
            details={"retry_after": retry_after, "body": body},
        )
    raise SchemaViolationError(
        f"Wise returned unexpected status {response.status_code}",
        source=SOURCE_ID,
        details={"status": response.status_code, "body": body},
    )


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _decimal_from(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _iso_z(value: datetime) -> str:
    """Wise expects the ``...Z`` suffix for UTC; coerce explicit offsets."""
    if value.tzinfo is None:
        raise DataQualityError(
            f"naive datetime {value!r} supplied to Wise window",
            source=SOURCE_ID,
        )
    stamped = value.astimezone(UTC).replace(tzinfo=None)
    return stamped.isoformat(timespec="milliseconds") + "Z"


def _parse_iso(value: str) -> datetime:
    cleaned = value.rstrip("Z").replace("Z", "")
    # Wise mixes ISO 8601 flavours; accept ``2026-04-10`` and
    # ``2026-04-10T08:30:00.000Z`` alike.
    if len(cleaned) == 10 and cleaned.count("-") == 2:
        return datetime.fromisoformat(cleaned + "T00:00:00+00:00")
    if "+" not in cleaned and "-" not in cleaned[10:]:
        cleaned += "+00:00"
    return datetime.fromisoformat(cleaned)


__all__ = [
    "ACCOUNT_LABEL",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_WINDOW_DAYS",
    "SCA_HEADER",
    "SCA_SIGNATURE_HEADER",
    "SECRETS_NAMESPACE",
    "SOURCE_ID",
    "WISE_API_BASE",
    "WISE_PROFILES_PATH",
    "RawTransaction",
    "WiseAccount",
    "WiseAdapter",
    "WiseAuth",
    "WiseFetchStats",
    "WiseProfile",
    "WiseSigner",
]
