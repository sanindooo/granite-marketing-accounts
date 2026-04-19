"""Tests for execution.adapters.monzo."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from execution.adapters import monzo as monzo_mod
from execution.adapters.monzo import (
    ACCOUNT_LABEL,
    DEFAULT_BATCH_SIZE,
    DEFAULT_REDIRECT_URI,
    DEFAULT_WINDOW_DAYS,
    MONZO_ACCOUNTS_PATH,
    MONZO_AUTH_URL,
    MONZO_TOKEN_PATH,
    MONZO_TRANSACTIONS_PATH,
    SOURCE_ID,
    CallbackResult,
    MonzoAccount,
    MonzoAdapter,
    MonzoAuth,
    RawTransaction,
    TokenCache,
    _extract_error_code,
    _iso_z,
    _parse_account,
    _parse_iso,
    _parse_transaction,
    _raise_for_monzo_status,
    find_free_port,
    new_state_token,
)
from execution.shared import secrets
from execution.shared.errors import (
    AuthExpiredError,
    ConfigError,
    RateLimitedError,
    SchemaViolationError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixed_clock(value: datetime):
    return lambda: value


def _new_auth(
    *,
    handler,
    clock_now: datetime,
) -> MonzoAuth:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(
        transport=transport, base_url=monzo_mod.MONZO_API_BASE
    )
    return MonzoAuth(
        client_id="cid",
        client_secret="csec",
        http=client,
        clock=_fixed_clock(clock_now),
    )


def _new_adapter(handler, auth: MonzoAuth | None = None) -> MonzoAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(
        transport=transport, base_url=monzo_mod.MONZO_API_BASE
    )
    a = auth or MonzoAuth(
        client_id="cid",
        client_secret="csec",
        http=client,
        clock=_fixed_clock(datetime(2026, 4, 11, tzinfo=UTC)),
    )
    return MonzoAdapter(auth=a, http=client)


# ---------------------------------------------------------------------------
# TokenCache serialisation
# ---------------------------------------------------------------------------


class TestTokenCache:
    def test_json_round_trip(self):
        now = datetime(2026, 4, 10, tzinfo=UTC)
        cache = TokenCache(
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(seconds=3600),
            first_auth_at=now,
            last_refresh_at=now,
            user_id="u-1",
        )
        reloaded = TokenCache.from_json(cache.to_json())
        assert reloaded == cache

    def test_malformed_json_raises_config(self):
        with pytest.raises(ConfigError):
            TokenCache.from_json("{}")

    def test_is_expiring_soon(self):
        now = datetime(2026, 4, 10, tzinfo=UTC)
        cache = TokenCache(
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(seconds=30),
            first_auth_at=now,
            last_refresh_at=now,
        )
        assert cache.is_expiring_soon(now=now, within=timedelta(seconds=60))
        assert not cache.is_expiring_soon(now=now, within=timedelta(seconds=10))


# ---------------------------------------------------------------------------
# build_authorize_url + CSRF state
# ---------------------------------------------------------------------------


def test_build_authorize_url_contains_required_params():
    auth = MonzoAuth(client_id="cid", client_secret="csec")
    url = auth.build_authorize_url(state="state-xyz")
    assert url.startswith(MONZO_AUTH_URL)
    assert "client_id=cid" in url
    assert "state=state-xyz" in url
    # redirect_uri URL-encoded:
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fcallback" in url
    assert "response_type=code" in url


def test_new_state_token_yields_distinct_values():
    assert new_state_token() != new_state_token()


def test_monzo_auth_requires_both_secrets():
    with pytest.raises(ConfigError):
        MonzoAuth(client_id="", client_secret="csec")
    with pytest.raises(ConfigError):
        MonzoAuth(client_id="cid", client_secret="")


def test_from_keychain_refuses_mock_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(secrets, "is_mock", lambda: True)
    with pytest.raises(ConfigError, match="MOCK_MODE"):
        MonzoAuth.from_keychain()


# ---------------------------------------------------------------------------
# Code exchange + refresh
# ---------------------------------------------------------------------------


def test_exchange_code_stores_token_cache(monkeypatch: pytest.MonkeyPatch):
    captured: list[dict] = []
    clock_now = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == MONZO_TOKEN_PATH
        body = dict(httpx.QueryParams(request.content.decode()))
        captured.append(body)
        assert body["grant_type"] == "authorization_code"
        assert body["client_secret"] == "csec"
        assert body["code"] == "the-code"
        return httpx.Response(
            200,
            json={
                "access_token": "ACC",
                "refresh_token": "REF",
                "expires_in": 3600,
                "user_id": "user_123",
            },
        )

    saved: list[TokenCache] = []
    monkeypatch.setattr(monzo_mod, "save_token_cache", saved.append)
    auth = _new_auth(handler=handler, clock_now=clock_now)
    cache = auth.exchange_code(code="the-code")

    assert cache.access_token == "ACC"
    assert cache.refresh_token == "REF"
    assert cache.first_auth_at == clock_now
    assert cache.access_expires_at == clock_now + timedelta(seconds=3600)
    assert saved == [cache]
    assert captured[0]["redirect_uri"] == DEFAULT_REDIRECT_URI


def test_exchange_code_auth_failure_raises(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"code": "bad_request.bad_param.invalid_code"}
        )

    monkeypatch.setattr(monzo_mod, "save_token_cache", lambda _cache: None)
    auth = _new_auth(handler=handler, clock_now=datetime(2026, 4, 10, tzinfo=UTC))
    with pytest.raises(AuthExpiredError) as exc:
        auth.exchange_code(code="bad")
    assert exc.value.details.get("error_code") == "bad_request.bad_param.invalid_code"


def test_refresh_updates_cache(monkeypatch: pytest.MonkeyPatch):
    clock_now = datetime(2026, 4, 10, 12, 30, tzinfo=UTC)
    seed = TokenCache(
        access_token="old-access",
        refresh_token="old-refresh",
        access_expires_at=clock_now - timedelta(seconds=10),
        first_auth_at=clock_now - timedelta(days=5),
        last_refresh_at=clock_now - timedelta(hours=1),
        user_id="u1",
    )

    saved: list[TokenCache] = []
    monkeypatch.setattr(monzo_mod, "save_token_cache", saved.append)
    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: seed)

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-refresh"
        return httpx.Response(
            200,
            json={
                "access_token": "NEW-ACC",
                "refresh_token": "NEW-REF",
                "expires_in": 3600,
            },
        )

    auth = _new_auth(handler=handler, clock_now=clock_now)
    cache = auth.refresh()
    assert cache.access_token == "NEW-ACC"
    assert cache.refresh_token == "NEW-REF"
    assert cache.first_auth_at == seed.first_auth_at  # preserved
    assert cache.user_id == "u1"
    assert saved == [cache]


def test_refresh_raises_when_cache_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: None)

    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("refresh should not call HTTP when cache empty")

    auth = _new_auth(handler=handler, clock_now=datetime(2026, 4, 10, tzinfo=UTC))
    with pytest.raises(AuthExpiredError, match="empty"):
        auth.refresh()


def test_access_token_refreshes_when_expiring(monkeypatch: pytest.MonkeyPatch):
    clock_now = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    seed = TokenCache(
        access_token="old",
        refresh_token="ref",
        access_expires_at=clock_now + timedelta(seconds=10),
        first_auth_at=clock_now,
        last_refresh_at=clock_now,
    )
    saved: list[TokenCache] = []
    monkeypatch.setattr(monzo_mod, "save_token_cache", saved.append)
    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: seed)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "NEW", "refresh_token": "ref", "expires_in": 3600}
        )

    auth = _new_auth(handler=handler, clock_now=clock_now)
    assert auth.access_token() == "NEW"


def test_access_token_returns_cached_when_fresh(monkeypatch: pytest.MonkeyPatch):
    clock_now = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    seed = TokenCache(
        access_token="fresh",
        refresh_token="ref",
        access_expires_at=clock_now + timedelta(hours=1),
        first_auth_at=clock_now,
        last_refresh_at=clock_now,
    )
    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: seed)

    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not call HTTP when token still fresh")

    auth = _new_auth(handler=handler, clock_now=clock_now)
    assert auth.access_token() == "fresh"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_parse_account_skips_missing_id():
    with pytest.raises(SchemaViolationError):
        _parse_account({})


def test_parse_account_defaults_currency_to_gbp():
    account = _parse_account({"id": "acc-1", "type": "uk_retail"})
    assert account == MonzoAccount(
        account_id="acc-1",
        description="acc-1",
        currency="GBP",
        account_type="uk_retail",
    )


def test_parse_transaction_converts_minor_units_and_settles():
    account = MonzoAccount(
        account_id="acc-1",
        description="Personal",
        currency="GBP",
        account_type="uk_retail",
    )
    raw = {
        "id": "tx_00009",
        "amount": -350,
        "currency": "GBP",
        "created": "2026-04-10T08:30:00Z",
        "settled": "2026-04-11T08:30:00Z",
        "merchant": {"name": "Starbucks London", "scheme_reference": "SCH-1"},
        "description": "STARBUCKS GB",
        "category": "eating_out",
    }
    txn = _parse_transaction(raw, account=account)
    assert txn is not None
    assert txn.amount == Decimal("-3.50")
    assert txn.status == "settled"
    assert txn.category_hint == "eating_out"
    # Monzo's top-level `id` is the stable pending→settled handle; we
    # hash it into txn_id so reruns remain idempotent.
    assert txn.account == f"{ACCOUNT_LABEL}-GBP"


def test_parse_transaction_marks_pending_when_settled_empty():
    account = MonzoAccount(
        account_id="acc-1",
        description="Personal",
        currency="GBP",
        account_type="uk_retail",
    )
    raw = {
        "id": "tx_1",
        "amount": 200,
        "currency": "GBP",
        "created": "2026-04-10T08:30:00Z",
        "settled": "",
        "description": "INCOMING",
    }
    txn = _parse_transaction(raw, account=account)
    assert txn is not None
    assert txn.status == "pending"
    assert txn.amount == Decimal("2.00")


def test_parse_transaction_drops_declined_row():
    account = MonzoAccount(
        account_id="acc-1",
        description="Personal",
        currency="GBP",
        account_type="uk_retail",
    )
    raw = {
        "id": "tx_decl",
        "amount": -100,
        "currency": "GBP",
        "created": "2026-04-10T08:30:00Z",
        "decline_reason": "INSUFFICIENT_FUNDS",
    }
    assert _parse_transaction(raw, account=account) is None


def test_parse_transaction_returns_none_without_description():
    account = MonzoAccount(
        account_id="acc-1",
        description="Personal",
        currency="GBP",
        account_type="uk_retail",
    )
    raw = {
        "id": "tx_nodesc",
        "amount": 100,
        "currency": "GBP",
        "created": "2026-04-10T08:30:00Z",
    }
    assert _parse_transaction(raw, account=account) is None


def test_parse_transaction_stable_txn_id_is_hash():
    account = MonzoAccount(
        account_id="acc-1",
        description="x",
        currency="GBP",
        account_type="uk_retail",
    )
    raw = {
        "id": "tx_1",
        "amount": 100,
        "currency": "GBP",
        "created": "2026-04-10T08:30:00Z",
        "description": "Coffee",
    }
    first = _parse_transaction(raw, account=account)
    second = _parse_transaction(raw, account=account)
    assert first is not None and second is not None
    assert first.txn_id == second.txn_id
    assert first.txn_id != "tx_1"  # hashed


# ---------------------------------------------------------------------------
# HTTP status translation
# ---------------------------------------------------------------------------


def _resp(status: int, body=None, headers=None):
    return httpx.Response(
        status,
        content=json.dumps(body or {}).encode("utf-8"),
        headers=headers or {"Content-Type": "application/json"},
    )


def test_raise_for_monzo_status_200_passthrough():
    _raise_for_monzo_status(_resp(200, {"ok": True}))


def test_raise_for_monzo_status_401_is_auth_expired():
    with pytest.raises(AuthExpiredError):
        _raise_for_monzo_status(_resp(401, {"code": "unauthorized"}))


def test_raise_for_monzo_status_429_with_retry_after():
    with pytest.raises(RateLimitedError) as exc:
        _raise_for_monzo_status(_resp(429, {}, {"Retry-After": "30"}))
    assert exc.value.details.get("retry_after") == "30"


def test_raise_for_monzo_status_other_4xx_is_schema():
    with pytest.raises(SchemaViolationError):
        _raise_for_monzo_status(_resp(418))


def test_extract_error_code_handles_string_body():
    assert _extract_error_code("plain text") is None


# ---------------------------------------------------------------------------
# Adapter fetch_since
# ---------------------------------------------------------------------------


def test_fetch_since_walks_accounts_and_transactions(monkeypatch: pytest.MonkeyPatch):
    clock_now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    seed = TokenCache(
        access_token="tok",
        refresh_token="ref",
        access_expires_at=clock_now + timedelta(hours=1),
        first_auth_at=clock_now - timedelta(days=5),
        last_refresh_at=clock_now,
    )
    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: seed)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MONZO_ACCOUNTS_PATH:
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {"id": "acc_1", "type": "uk_retail", "currency": "GBP"},
                        {
                            "id": "acc_closed",
                            "type": "uk_retail",
                            "currency": "GBP",
                            "closed": True,
                        },
                    ]
                },
            )
        if request.url.path == MONZO_TRANSACTIONS_PATH:
            assert request.url.params["account_id"] == "acc_1"
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "id": "tx_1",
                            "amount": -250,
                            "currency": "GBP",
                            "created": "2026-04-10T08:30:00Z",
                            "settled": "2026-04-11T08:30:00Z",
                            "merchant": {"name": "Coffee Co"},
                        },
                        {
                            "id": "tx_2",
                            "amount": 1000,
                            "currency": "GBP",
                            "created": "2026-04-10T10:00:00Z",
                            "settled": "2026-04-11T10:00:00Z",
                            "description": "INCOMING FROM X",
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    adapter = _new_adapter(
        handler,
        auth=MonzoAuth(
            client_id="cid",
            client_secret="csec",
            http=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url=monzo_mod.MONZO_API_BASE,
            ),
            clock=_fixed_clock(clock_now),
        ),
    )
    batches = list(adapter.fetch_since(None, now=clock_now))
    adapter.close()

    assert len(batches) == 1
    amounts = sorted(t.amount for t in batches[0])
    assert amounts == [Decimal("-2.50"), Decimal("10.00")]
    assert adapter.next_watermark is not None
    assert adapter.next_watermark.startswith("2026-04-11")


def test_fetch_since_clamps_watermark_to_window(monkeypatch: pytest.MonkeyPatch):
    clock_now = datetime(2026, 4, 11, tzinfo=UTC)
    seed = TokenCache(
        access_token="tok",
        refresh_token="ref",
        access_expires_at=clock_now + timedelta(hours=1),
        first_auth_at=clock_now,
        last_refresh_at=clock_now,
    )
    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: seed)

    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MONZO_ACCOUNTS_PATH:
            return httpx.Response(
                200,
                json={"accounts": [{"id": "acc_1", "type": "uk_retail"}]},
            )
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"transactions": []})

    adapter = _new_adapter(
        handler,
        auth=MonzoAuth(
            client_id="cid",
            client_secret="csec",
            http=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url=monzo_mod.MONZO_API_BASE,
            ),
            clock=_fixed_clock(clock_now),
        ),
    )
    list(adapter.fetch_since("2024-01-01T00:00:00Z", now=clock_now))
    adapter.close()

    since = datetime.fromisoformat(captured_params["since"].replace("Z", "+00:00"))
    assert clock_now - since <= timedelta(days=DEFAULT_WINDOW_DAYS, seconds=1)


def test_fetch_since_rejects_invalid_watermark(monkeypatch: pytest.MonkeyPatch):
    clock_now = datetime(2026, 4, 11, tzinfo=UTC)
    seed = TokenCache(
        access_token="tok",
        refresh_token="ref",
        access_expires_at=clock_now + timedelta(hours=1),
        first_auth_at=clock_now,
        last_refresh_at=clock_now,
    )
    monkeypatch.setattr(monzo_mod, "load_token_cache", lambda: seed)

    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not reach HTTP")

    adapter = _new_adapter(
        handler,
        auth=MonzoAuth(
            client_id="cid",
            client_secret="csec",
            http=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url=monzo_mod.MONZO_API_BASE,
            ),
            clock=_fixed_clock(clock_now),
        ),
    )
    with pytest.raises(SchemaViolationError):
        list(adapter.fetch_since("not-iso"))
    adapter.close()


def test_adapter_retries_401_once_via_refresh(monkeypatch: pytest.MonkeyPatch):
    clock_now = datetime(2026, 4, 11, tzinfo=UTC)
    seed = TokenCache(
        access_token="tok-1",
        refresh_token="ref",
        access_expires_at=clock_now + timedelta(hours=1),
        first_auth_at=clock_now,
        last_refresh_at=clock_now,
    )
    current_cache = [seed]

    def load():
        return current_cache[0]

    def save(cache):
        current_cache[0] = cache

    monkeypatch.setattr(monzo_mod, "load_token_cache", load)
    monkeypatch.setattr(monzo_mod, "save_token_cache", save)

    call_log: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append((request.url.path, request.headers.get("Authorization", "")))
        if request.url.path == MONZO_TOKEN_PATH:
            return httpx.Response(
                200,
                json={
                    "access_token": "tok-2",
                    "refresh_token": "ref",
                    "expires_in": 3600,
                },
            )
        if request.url.path == MONZO_ACCOUNTS_PATH:
            if "tok-1" in request.headers.get("Authorization", ""):
                return httpx.Response(401, json={"code": "unauthorized"})
            return httpx.Response(
                200,
                json={"accounts": [{"id": "acc_1", "type": "uk_retail"}]},
            )
        raise AssertionError(request.url.path)

    adapter = _new_adapter(
        handler,
        auth=MonzoAuth(
            client_id="cid",
            client_secret="csec",
            http=httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url=monzo_mod.MONZO_API_BASE,
            ),
            clock=_fixed_clock(clock_now),
        ),
    )
    accounts = adapter.list_accounts()
    adapter.close()

    assert len(accounts) == 1
    paths = [c[0] for c in call_log]
    assert paths.count(MONZO_ACCOUNTS_PATH) == 2  # 401 → refresh → retry
    assert paths.count(MONZO_TOKEN_PATH) == 1


# ---------------------------------------------------------------------------
# Callback server sanity
# ---------------------------------------------------------------------------


def test_find_free_port_returns_usable_port():
    port = find_free_port()
    assert 1024 < port < 65_536


def test_callback_result_fields_shape():
    result = CallbackResult(code="c", state="s")
    assert result.code == "c"
    assert result.state == "s"


# ---------------------------------------------------------------------------
# iso helpers
# ---------------------------------------------------------------------------


def test_iso_z_rejects_naive_datetime():
    with pytest.raises(SchemaViolationError):
        _iso_z(datetime(2026, 4, 10))


def test_iso_z_ends_with_z():
    assert _iso_z(datetime(2026, 4, 10, tzinfo=UTC)).endswith("Z")


def test_parse_iso_handles_z_suffix():
    parsed = _parse_iso("2026-04-10T08:30:00Z")
    assert parsed.tzinfo is UTC and parsed.hour == 8


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


def test_module_constants():
    assert SOURCE_ID == "monzo"
    assert ACCOUNT_LABEL == "monzo"
    assert DEFAULT_BATCH_SIZE == 50
    assert DEFAULT_WINDOW_DAYS == 60


def test_raw_transaction_as_row_includes_status():
    txn = RawTransaction(
        txn_id="t",
        account="monzo-GBP",
        booking_date=datetime(2026, 4, 10, tzinfo=UTC).date(),
        description_raw="x",
        description_canonical="X",
        currency="GBP",
        amount=Decimal("1"),
        reference="r",
        category_hint=None,
        status="pending",
        provider_auth_id="p",
    )
    row = txn.as_row()
    assert row["status"] == "pending"
    assert row["source"] == SOURCE_ID
