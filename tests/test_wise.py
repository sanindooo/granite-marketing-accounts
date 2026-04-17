"""Tests for execution.adapters.wise."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from execution.adapters import wise as wise_mod
from execution.adapters.wise import (
    ACCOUNT_LABEL,
    DEFAULT_BATCH_SIZE,
    DEFAULT_WINDOW_DAYS,
    SCA_HEADER,
    SCA_SIGNATURE_HEADER,
    SOURCE_ID,
    RawTransaction,
    WiseAccount,
    WiseAdapter,
    WiseAuth,
    WiseProfile,
    WiseSigner,
    _iso_z,
    _parse_account,
    _parse_iso,
    _parse_profile,
    _parse_transaction,
    _raise_for_wise_status,
)
from execution.shared import secrets
from execution.shared.errors import (
    AuthExpiredError,
    ConfigError,
    DataQualityError,
    RateLimitedError,
    SchemaViolationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FixedSigner:
    """Stub signer that returns a canned signature without touching cryptography."""

    def __init__(self, *, signature: str = "sig-abc") -> None:
        self._signature = signature
        self.calls: list[str] = []

    def sign(self, challenge: str) -> str:
        self.calls.append(challenge)
        return self._signature


def _auth(signer: _FixedSigner | None = None) -> WiseAuth:
    auth = WiseAuth.__new__(WiseAuth)
    auth._api_token = "test-token"  # type: ignore[attr-defined]
    auth._signer = signer or _FixedSigner()  # type: ignore[attr-defined]
    return auth


def _adapter(handler, *, auth: WiseAuth | None = None) -> WiseAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=wise_mod.WISE_API_BASE)
    return WiseAdapter(auth=auth or _auth(), http=client)


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------


def test_signer_round_trip_with_real_rsa_key():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    signer = WiseSigner(private_key_pem=pem)

    signature_b64 = signer.sign("abc123")

    import base64 as _b64

    signature_bytes = _b64.b64decode(signature_b64)
    # Verify against the matching public key — round-trip proves the
    # signer produced a valid PKCS1v15-SHA256 RSA signature.
    private_key.public_key().verify(
        signature_bytes,
        b"abc123",
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_signer_rejects_empty_pem():
    with pytest.raises(ConfigError, match="empty"):
        WiseSigner(private_key_pem=b"")


def test_signer_from_keychain_refuses_mock_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(secrets, "is_mock", lambda: True)
    with pytest.raises(ConfigError, match="MOCK_MODE"):
        WiseSigner.from_keychain()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_auth_rejects_blank_token():
    with pytest.raises(ConfigError, match="API token"):
        WiseAuth(api_token="   ", signer=_FixedSigner())  # type: ignore[arg-type]


def test_auth_authorization_header_bearer():
    auth = _auth()
    assert auth.authorization_header() == "Bearer test-token"


def test_auth_from_keychain_refuses_mock_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(secrets, "is_mock", lambda: True)
    with pytest.raises(ConfigError, match="MOCK_MODE"):
        WiseAuth.from_keychain()


# ---------------------------------------------------------------------------
# SCA dance
# ---------------------------------------------------------------------------


def test_request_performs_sca_dance_on_403():
    signer = _FixedSigner(signature="canned-sig")
    call_log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request)
        if len(call_log) == 1:
            return httpx.Response(
                403,
                headers={SCA_HEADER: "challenge-xyz"},
                json={"error": "sca_required"},
            )
        assert request.headers.get(SCA_HEADER) == "challenge-xyz"
        assert request.headers.get(SCA_SIGNATURE_HEADER) == "canned-sig"
        return httpx.Response(200, json=[])

    adapter = _adapter(handler, auth=_auth(signer))
    adapter.list_profiles()
    adapter.close()

    assert len(call_log) == 2
    assert signer.calls == ["challenge-xyz"]


def test_request_raises_when_signed_replay_also_fails():
    signer = _FixedSigner()

    def handler(request: httpx.Request) -> httpx.Response:
        if SCA_SIGNATURE_HEADER in request.headers:
            return httpx.Response(
                403,
                headers={SCA_HEADER: "challenge-xyz"},
                json={"error": "invalid_signature"},
            )
        return httpx.Response(
            403,
            headers={SCA_HEADER: "challenge-xyz"},
            json={"error": "sca_required"},
        )

    adapter = _adapter(handler, auth=_auth(signer))
    with pytest.raises(AuthExpiredError, match="signing failed"):
        adapter.list_profiles()
    adapter.close()


def test_request_treats_403_without_challenge_as_auth_expired():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    adapter = _adapter(handler)
    with pytest.raises(AuthExpiredError, match="403"):
        adapter.list_profiles()
    adapter.close()


# ---------------------------------------------------------------------------
# Profile + account parsing
# ---------------------------------------------------------------------------


def test_parse_profile_accepts_v2_shape():
    profile = _parse_profile(
        {"id": 42, "type": "BUSINESS", "fullName": "Granite Marketing"}
    )
    assert profile == WiseProfile(
        profile_id=42, profile_type="business", name="Granite Marketing"
    )


def test_parse_profile_falls_back_to_name_key():
    profile = _parse_profile({"id": "7", "type": "PERSONAL", "name": "Stephen"})
    assert profile.profile_id == 7
    assert profile.name == "Stephen"


def test_parse_profile_missing_id_raises():
    with pytest.raises(SchemaViolationError):
        _parse_profile({"type": "BUSINESS"})


def test_parse_account_requires_three_letter_currency():
    with pytest.raises(SchemaViolationError, match="currency"):
        _parse_account({"id": 1, "currency": "GBPP"}, profile_id=1)


def test_parse_account_upper_cases_currency():
    account = _parse_account({"id": 99, "currency": "eur"}, profile_id=42)
    assert account == WiseAccount(
        account_id=99, profile_id=42, currency="EUR", name="EUR-balance"
    )


# ---------------------------------------------------------------------------
# Transaction parsing
# ---------------------------------------------------------------------------


def test_parse_transaction_debit_flips_sign():
    acc = WiseAccount(account_id=1, profile_id=1, currency="GBP", name="GBP")
    raw = {
        "type": "DEBIT",
        "date": "2026-04-10T08:30:00.000Z",
        "amount": {"value": 25.00, "currency": "GBP"},
        "referenceNumber": "TR-12345",
        "details": {"description": "Stripe 2 Charge"},
        "status": "COMPLETED",
    }
    txn = _parse_transaction(raw, account=acc)
    assert txn is not None
    assert txn.amount == Decimal("-25.00")
    assert txn.reference == "TR-12345"
    assert txn.status == "settled"
    assert txn.account == f"{ACCOUNT_LABEL}-GBP"


def test_parse_transaction_credit_preserves_sign():
    acc = WiseAccount(account_id=1, profile_id=1, currency="USD", name="USD")
    raw = {
        "type": "CREDIT",
        "date": "2026-04-10",
        "amount": {"value": "100.50", "currency": "USD"},
        "details": {"description": "INCOMING FROM CLIENT"},
    }
    txn = _parse_transaction(raw, account=acc)
    assert txn is not None
    assert txn.amount == Decimal("100.50")
    assert txn.currency == "USD"


def test_parse_transaction_pending_status():
    acc = WiseAccount(account_id=1, profile_id=1, currency="GBP", name="GBP")
    raw = {
        "type": "DEBIT",
        "date": "2026-04-10",
        "amount": {"value": 1, "currency": "GBP"},
        "details": {"description": "PENDING SOMETHING"},
        "status": "PENDING",
    }
    txn = _parse_transaction(raw, account=acc)
    assert txn is not None
    assert txn.status == "pending"


def test_parse_transaction_returns_none_on_missing_description():
    acc = WiseAccount(account_id=1, profile_id=1, currency="GBP", name="GBP")
    raw = {"type": "DEBIT", "date": "2026-04-10", "amount": {"value": 1, "currency": "GBP"}}
    assert _parse_transaction(raw, account=acc) is None


def test_parse_transaction_stable_txn_id_uses_reference():
    acc = WiseAccount(account_id=1, profile_id=1, currency="GBP", name="GBP")
    raw = {
        "type": "DEBIT",
        "date": "2026-04-10",
        "amount": {"value": 1, "currency": "GBP"},
        "details": {"description": "TEST"},
        "referenceNumber": "REF-1",
    }
    first = _parse_transaction(raw, account=acc)
    second = _parse_transaction(raw, account=acc)
    assert first is not None and second is not None
    assert first.txn_id == second.txn_id
    assert first.txn_id != "REF-1"  # hashed, not raw


def test_parse_transaction_falls_back_to_id_when_no_reference():
    acc = WiseAccount(account_id=1, profile_id=1, currency="GBP", name="GBP")
    raw = {
        "type": "DEBIT",
        "date": "2026-04-10",
        "amount": {"value": 1, "currency": "GBP"},
        "details": {"description": "TEST"},
        "id": "activity-abc",
    }
    txn = _parse_transaction(raw, account=acc)
    assert txn is not None
    assert txn.provider_auth_id == "activity-abc"


# ---------------------------------------------------------------------------
# HTTP status translation
# ---------------------------------------------------------------------------


def _resp(status: int, body=None, headers=None):
    return httpx.Response(
        status,
        content=json.dumps(body or {}).encode("utf-8"),
        headers=headers or {"Content-Type": "application/json"},
    )


def test_raise_for_wise_status_200_passthrough():
    _raise_for_wise_status(_resp(200, {"ok": True}))


def test_raise_for_wise_status_401_is_auth_expired():
    with pytest.raises(AuthExpiredError, match="401"):
        _raise_for_wise_status(_resp(401, {"error": "invalid_token"}))


def test_raise_for_wise_status_429_is_rate_limited_with_retry_after():
    with pytest.raises(RateLimitedError) as exc:
        _raise_for_wise_status(_resp(429, {"error": "throttled"}, {"Retry-After": "30"}))
    assert exc.value.details.get("retry_after") == "30"


def test_raise_for_wise_status_5xx_is_rate_limited():
    with pytest.raises(RateLimitedError):
        _raise_for_wise_status(_resp(502))


def test_raise_for_wise_status_other_4xx_is_schema_violation():
    with pytest.raises(SchemaViolationError):
        _raise_for_wise_status(_resp(418))


# ---------------------------------------------------------------------------
# fetch_since end-to-end
# ---------------------------------------------------------------------------


def test_fetch_since_walks_profiles_accounts_and_statements():
    pages = {
        "/v2/profiles": [
            {"id": 1, "type": "BUSINESS", "fullName": "Granite"},
        ],
        "/v4/profiles/1/balances": [
            {"id": 10, "currency": "GBP", "name": "Main"},
        ],
    }
    statement_path = "/v1/profiles/1/borderless-accounts/10/statement.json"

    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path == statement_path:
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "type": "DEBIT",
                            "date": "2026-04-09",
                            "amount": {"value": 12.34, "currency": "GBP"},
                            "details": {"description": "COFFEE SHOP"},
                            "referenceNumber": "TR-1",
                        },
                        {
                            "type": "CREDIT",
                            "date": "2026-04-10",
                            "amount": {"value": 500, "currency": "GBP"},
                            "details": {"description": "CLIENT PAYMENT"},
                            "referenceNumber": "TR-2",
                        },
                    ]
                },
            )
        if url_path in pages:
            return httpx.Response(200, json=pages[url_path])
        raise AssertionError(f"unexpected call to {url_path}")

    adapter = _adapter(handler)
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    batches = list(adapter.fetch_since(None, now=now))
    adapter.close()

    assert len(batches) == 1
    amounts = sorted(t.amount for t in batches[0])
    assert amounts == [Decimal("-12.34"), Decimal("500")]
    assert adapter.next_watermark is not None
    # Watermark is the run's "now" — next run clamps to now - window_days.
    assert adapter.next_watermark.startswith("2026-04-11")


def test_fetch_since_clamps_watermark_to_window_floor():
    very_old = datetime(2025, 1, 1, tzinfo=UTC).isoformat()

    calls: dict[str, int] = {"statement": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/statement.json"):
            calls["statement"] += 1
            params = dict(request.url.params)
            start = params["intervalStart"]
            # Must be within `window_days` of now, NOT at 2025-01-01.
            parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
            assert parsed > datetime(2025, 12, 1, tzinfo=UTC)
            return httpx.Response(200, json={"transactions": []})
        if request.url.path == "/v2/profiles":
            return httpx.Response(200, json=[{"id": 1, "type": "BUSINESS"}])
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json=[{"id": 10, "currency": "GBP"}])
        raise AssertionError(f"unexpected path {request.url.path}")

    adapter = _adapter(handler)
    list(
        adapter.fetch_since(very_old, now=datetime(2026, 4, 11, tzinfo=UTC))
    )
    adapter.close()
    assert calls["statement"] == 1


def test_fetch_since_rejects_invalid_watermark():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    adapter = _adapter(handler)
    with pytest.raises(SchemaViolationError):
        list(adapter.fetch_since("not-a-date"))
    adapter.close()


# ---------------------------------------------------------------------------
# Helpers around iso handling
# ---------------------------------------------------------------------------


def test_iso_z_rejects_naive():
    with pytest.raises(DataQualityError):
        _iso_z(datetime(2026, 4, 10))


def test_iso_z_coerces_to_z_suffix():
    stamped = _iso_z(datetime(2026, 4, 10, 12, 0, tzinfo=UTC))
    assert stamped.endswith("Z")


def test_parse_iso_accepts_date_only():
    assert _parse_iso("2026-04-10").date().isoformat() == "2026-04-10"


def test_parse_iso_accepts_ms_precision():
    parsed = _parse_iso("2026-04-10T08:30:00.000Z")
    assert parsed.year == 2026 and parsed.hour == 8


# ---------------------------------------------------------------------------
# Module-level contract checks
# ---------------------------------------------------------------------------


def test_constants_match_plan():
    assert SOURCE_ID == "wise"
    assert ACCOUNT_LABEL == "wise"
    assert DEFAULT_BATCH_SIZE == 50
    assert DEFAULT_WINDOW_DAYS == 60


def test_raw_transaction_as_row_schema():
    txn = RawTransaction(
        txn_id="id-1",
        account="wise-GBP",
        booking_date=datetime(2026, 4, 10, tzinfo=UTC).date(),
        description_raw="desc",
        description_canonical="DESC",
        currency="GBP",
        amount=Decimal("10.00"),
        reference="TR-1",
        category_hint=None,
        status="settled",
        provider_auth_id="TR-1",
    )
    row = txn.as_row()
    assert row["account"] == "wise-GBP"
    assert row["status"] == "settled"
    assert row["source"] == SOURCE_ID


def test_module_surface_contains_key_exports():
    assert hasattr(wise_mod, "WiseAdapter")
    assert hasattr(wise_mod, "WiseAuth")
    assert hasattr(wise_mod, "WiseSigner")
    assert hasattr(wise_mod, "SCA_HEADER")


def test_fetch_since_returns_empty_generator_when_no_profiles():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/profiles":
            return httpx.Response(200, json=[])
        raise AssertionError(f"should not query {request.url.path}")

    adapter = _adapter(handler)
    batches = list(adapter.fetch_since(None, now=datetime(2026, 4, 11, tzinfo=UTC)))
    adapter.close()
    assert batches == []
    assert adapter.next_watermark is not None


def test_sliding_window_defaults_to_60_days():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/statement.json"):
            captured.update(dict(request.url.params))
            return httpx.Response(200, json={"transactions": []})
        if request.url.path == "/v2/profiles":
            return httpx.Response(200, json=[{"id": 1, "type": "BUSINESS"}])
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json=[{"id": 10, "currency": "GBP"}])
        raise AssertionError(request.url.path)

    now = datetime(2026, 4, 11, tzinfo=UTC)
    adapter = _adapter(handler)
    list(adapter.fetch_since(None, now=now))
    adapter.close()

    start = datetime.fromisoformat(captured["intervalStart"].replace("Z", "+00:00"))
    delta = now - start
    assert abs(delta - timedelta(days=DEFAULT_WINDOW_DAYS)) < timedelta(seconds=1)
