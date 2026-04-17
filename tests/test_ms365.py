"""Tests for execution.adapters.ms365."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from execution.adapters import ms365
from execution.adapters.ms365 import (
    AUTHORITY_BASE,
    DEFAULT_AUTHORITY,
    DEFAULT_BATCH_SIZE,
    INBOX_DELTA_URL,
    SOURCE_ID,
    Ms365Adapter,
    Ms365Auth,
    RawEmail,
    _parse_graph_datetime,
    _parse_graph_message,
    _raise_for_graph_status,
    resolve_authority,
)
from execution.shared import secrets
from execution.shared.errors import (
    AuthExpiredError,
    ConfigError,
    RateLimitedError,
    SchemaViolationError,
)

# ---------------------------------------------------------------------------
# Parser primitives
# ---------------------------------------------------------------------------


class TestParseGraphDatetime:
    def test_handles_z_suffix(self):
        d = _parse_graph_datetime("2026-04-10T08:30:00Z")
        assert d == datetime(2026, 4, 10, 8, 30, 0, tzinfo=UTC)

    def test_handles_explicit_offset(self):
        d = _parse_graph_datetime("2026-04-10T08:30:00+00:00")
        assert d.tzinfo is not None


class TestParseGraphMessage:
    def test_extracts_canonical_fields(self):
        raw = {
            "id": "AAA=",
            "subject": "Your invoice",
            "from": {
                "emailAddress": {
                    "address": "billing@stripe.com",
                    "name": "Stripe",
                }
            },
            "receivedDateTime": "2026-04-10T08:30:00Z",
            "hasAttachments": True,
            "internetMessageId": "<cafe@stripe.com>",
            "bodyPreview": "Thanks for your business",
        }
        email = _parse_graph_message(raw)
        assert email is not None
        assert email.msg_id == "AAA="
        assert email.from_addr == "billing@stripe.com"
        assert email.has_attachments is True
        assert email.internet_message_id == "<cafe@stripe.com>"
        assert email.subject == "Your invoice"

    def test_returns_none_for_removed_entries(self):
        assert _parse_graph_message({"@removed": {"reason": "deleted"}}) is None

    def test_returns_none_when_missing_id(self):
        assert _parse_graph_message({"subject": "x"}) is None

    def test_returns_none_when_missing_received(self):
        assert _parse_graph_message({"id": "BBB=", "subject": "x"}) is None

    def test_handles_missing_from(self):
        raw = {
            "id": "CCC=",
            "subject": "orphan",
            "receivedDateTime": "2026-04-10T08:30:00Z",
        }
        email = _parse_graph_message(raw)
        assert email is not None
        assert email.from_addr == ""


class TestRaiseForGraphStatus:
    def _resp(self, status: int, body: dict | None = None, headers: dict | None = None):
        return httpx.Response(
            status,
            content=json.dumps(body or {}).encode("utf-8"),
            headers=headers or {"Content-Type": "application/json"},
        )

    def test_200_passes(self):
        _raise_for_graph_status(self._resp(200, {"value": []}))

    def test_401_raises_auth_expired(self):
        with pytest.raises(AuthExpiredError, match="401"):
            _raise_for_graph_status(self._resp(401, {"error": "expired"}))

    def test_403_raises_auth_expired(self):
        with pytest.raises(AuthExpiredError, match="403"):
            _raise_for_graph_status(self._resp(403, {"error": "consent_required"}))

    def test_429_raises_rate_limited_with_retry_after(self):
        with pytest.raises(RateLimitedError) as exc:
            _raise_for_graph_status(
                self._resp(429, {"error": "throttled"}, {"Retry-After": "30"})
            )
        assert exc.value.details.get("retry_after") == "30"

    def test_503_raises_rate_limited(self):
        with pytest.raises(RateLimitedError):
            _raise_for_graph_status(self._resp(503, {"error": "busy"}))

    def test_unexpected_status_raises_schema(self):
        with pytest.raises(SchemaViolationError):
            _raise_for_graph_status(self._resp(418))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_from_keychain_refuses_mock_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(secrets, "is_mock", lambda: True)
    with pytest.raises(ConfigError, match="MOCK_MODE"):
        Ms365Auth.from_keychain()


class TestResolveAuthority:
    def test_falls_back_to_common_when_tenant_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(secrets, "get", lambda *_a, **_k: None)
        assert resolve_authority() == DEFAULT_AUTHORITY

    def test_builds_single_tenant_authority_from_guid(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        tenant = "11111111-2222-3333-4444-555555555555"
        monkeypatch.setattr(secrets, "get", lambda *_a, **_k: tenant)
        assert resolve_authority() == f"{AUTHORITY_BASE}/{tenant}"

    def test_builds_single_tenant_authority_from_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            secrets, "get", lambda *_a, **_k: "granitemarketing.onmicrosoft.com"
        )
        assert (
            resolve_authority()
            == f"{AUTHORITY_BASE}/granitemarketing.onmicrosoft.com"
        )

    def test_refuses_tenant_with_slash(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(secrets, "get", lambda *_a, **_k: "evil/tenant")
        with pytest.raises(ConfigError, match="tenant_id"):
            resolve_authority()

    def test_refuses_tenant_with_whitespace(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(secrets, "get", lambda *_a, **_k: "bad tenant")
        with pytest.raises(ConfigError, match="tenant_id"):
            resolve_authority()


def test_auth_constructor_stores_authority():
    auth = Ms365Auth(client_id="cid", authority="https://login.microsoftonline.com/t1")
    assert auth.authority == "https://login.microsoftonline.com/t1"


def test_auth_defaults_to_common_authority():
    auth = Ms365Auth(client_id="cid")
    assert auth.authority == DEFAULT_AUTHORITY


class _FakeMsalApp:
    def __init__(self, *, accounts=None, silent_result=None, device_flow=None, flow_result=None):
        self._accounts = accounts or []
        self._silent = silent_result
        self._device_flow = device_flow or {}
        self._flow_result = flow_result or {}

        class _Cache:
            has_state_changed = False

            @staticmethod
            def serialize():
                return "{}"

        self.token_cache = _Cache()

    def get_accounts(self):
        return self._accounts

    def acquire_token_silent(self, scopes, account):
        del scopes, account
        return self._silent

    def initiate_device_flow(self, scopes):
        del scopes
        return self._device_flow

    def acquire_token_by_device_flow(self, flow):
        del flow
        return self._flow_result


def test_auth_access_token_happy_path():
    auth = Ms365Auth(
        client_id="test-id",
        msal_app=_FakeMsalApp(
            accounts=[{"home_account_id": "x"}],
            silent_result={"access_token": "tok-1"},
        ),
    )
    assert auth.access_token() == "tok-1"


def test_auth_raises_when_silent_refresh_fails():
    auth = Ms365Auth(
        client_id="test-id",
        msal_app=_FakeMsalApp(
            accounts=[{"home_account_id": "x"}],
            silent_result={"error": "invalid_grant"},
        ),
    )
    with pytest.raises(AuthExpiredError, match="reauth"):
        auth.access_token()


def test_auth_raises_when_no_accounts_cached():
    auth = Ms365Auth(
        client_id="test-id",
        msal_app=_FakeMsalApp(accounts=[]),
    )
    with pytest.raises(AuthExpiredError):
        auth.access_token()


def test_auth_device_flow_requires_initiate():
    auth = Ms365Auth(
        client_id="test-id",
        msal_app=_FakeMsalApp(),
    )
    with pytest.raises(ConfigError, match="initiate_device_flow"):
        auth.complete_device_flow()


def test_auth_device_flow_happy_path():
    app = _FakeMsalApp(
        device_flow={"user_code": "ABCD", "message": "go here"},
        flow_result={"access_token": "tok-2"},
    )
    auth = Ms365Auth(client_id="test-id", msal_app=app)
    flow = auth.initiate_device_flow()
    assert flow["user_code"] == "ABCD"
    auth.complete_device_flow()  # does not raise


def test_auth_device_flow_failed_start():
    auth = Ms365Auth(
        client_id="test-id",
        msal_app=_FakeMsalApp(device_flow={"error": "bad_client"}),
    )
    with pytest.raises(ConfigError, match="device flow"):
        auth.initiate_device_flow()


def test_auth_device_flow_token_exchange_failed():
    auth = Ms365Auth(
        client_id="test-id",
        msal_app=_FakeMsalApp(
            device_flow={"user_code": "ABCD"},
            flow_result={"error": "expired_token"},
        ),
    )
    auth.initiate_device_flow()
    with pytest.raises(AuthExpiredError):
        auth.complete_device_flow()


# ---------------------------------------------------------------------------
# Adapter fetch_since
# ---------------------------------------------------------------------------


class _StaticAuth:
    """Minimal Ms365Auth stand-in that returns a canned token."""

    def access_token(self) -> str:
        return "test-token"


def _mock_transport(responses_by_url: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        # Keep the URL matching forgiving: allow exact or prefix match.
        url = str(request.url)
        if url in responses_by_url:
            return responses_by_url[url]
        # Also try without the query string
        url_no_q = url.split("?", 1)[0]
        if url_no_q in responses_by_url:
            return responses_by_url[url_no_q]
        raise AssertionError(f"unexpected request to {url}")

    return httpx.MockTransport(handler)


def _email_payload(msg_id: str) -> dict[str, Any]:
    return {
        "id": msg_id,
        "subject": f"Subject {msg_id}",
        "from": {
            "emailAddress": {
                "address": f"sender-{msg_id}@example.com",
                "name": "Sender",
            }
        },
        "receivedDateTime": "2026-04-10T08:30:00Z",
        "hasAttachments": False,
        "internetMessageId": f"<{msg_id}@example.com>",
        "bodyPreview": "hello",
    }


def test_fetch_since_initial_sync_returns_messages():
    delta_url = "https://graph.microsoft.com/v1.0/me/deltaLink1"
    responses = {
        INBOX_DELTA_URL: httpx.Response(
            200,
            content=json.dumps(
                {
                    "value": [_email_payload(f"msg-{i}") for i in range(3)],
                    "@odata.deltaLink": delta_url,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    }
    transport = _mock_transport(responses)
    adapter = Ms365Adapter(
        auth=_StaticAuth(),  # type: ignore[arg-type]
        http=httpx.Client(transport=transport),
    )
    batches = list(adapter.fetch_since(None))
    adapter.close()
    assert len(batches) == 1
    assert len(batches[0]) == 3
    assert all(isinstance(e, RawEmail) for e in batches[0])
    assert adapter.next_watermark == delta_url


def test_fetch_since_paginates_and_buffers_to_batch_size():
    next_url = "https://graph.microsoft.com/v1.0/me/next"
    delta_url = "https://graph.microsoft.com/v1.0/me/delta2"
    page1 = [_email_payload(f"a-{i}") for i in range(DEFAULT_BATCH_SIZE)]
    page2 = [_email_payload(f"b-{i}") for i in range(10)]
    responses = {
        INBOX_DELTA_URL: httpx.Response(
            200,
            content=json.dumps(
                {"value": page1, "@odata.nextLink": next_url}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        ),
        next_url: httpx.Response(
            200,
            content=json.dumps(
                {"value": page2, "@odata.deltaLink": delta_url}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        ),
    }
    transport = _mock_transport(responses)
    adapter = Ms365Adapter(
        auth=_StaticAuth(),  # type: ignore[arg-type]
        http=httpx.Client(transport=transport),
    )
    batches = list(adapter.fetch_since(None))
    adapter.close()
    # First batch fills to batch_size (50), second carries the final 10
    assert len(batches) == 2
    assert len(batches[0]) == DEFAULT_BATCH_SIZE
    assert len(batches[1]) == 10
    assert adapter.next_watermark == delta_url


def test_fetch_since_uses_watermark_url_verbatim():
    watermark = "https://graph.microsoft.com/v1.0/me/customDelta"
    new_delta = "https://graph.microsoft.com/v1.0/me/customDelta2"
    responses = {
        watermark: httpx.Response(
            200,
            content=json.dumps(
                {"value": [_email_payload("c-1")], "@odata.deltaLink": new_delta}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    }
    transport = _mock_transport(responses)
    adapter = Ms365Adapter(
        auth=_StaticAuth(),  # type: ignore[arg-type]
        http=httpx.Client(transport=transport),
    )
    batches = list(adapter.fetch_since(watermark))
    adapter.close()
    assert len(batches) == 1
    assert batches[0][0].msg_id == "c-1"
    assert adapter.next_watermark == new_delta


def test_fetch_since_translates_401_to_auth_expired():
    responses = {
        INBOX_DELTA_URL: httpx.Response(
            401,
            content=b'{"error": "token_expired"}',
            headers={"Content-Type": "application/json"},
        )
    }
    transport = _mock_transport(responses)
    adapter = Ms365Adapter(
        auth=_StaticAuth(),  # type: ignore[arg-type]
        http=httpx.Client(transport=transport),
    )
    with pytest.raises(AuthExpiredError):
        list(adapter.fetch_since(None))
    adapter.close()


def test_fetch_since_skips_removed_entries():
    responses = {
        INBOX_DELTA_URL: httpx.Response(
            200,
            content=json.dumps(
                {
                    "value": [
                        _email_payload("good-1"),
                        {"@removed": {"reason": "deleted"}},
                        _email_payload("good-2"),
                    ],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/dx",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    }
    transport = _mock_transport(responses)
    adapter = Ms365Adapter(
        auth=_StaticAuth(),  # type: ignore[arg-type]
        http=httpx.Client(transport=transport),
    )
    batches = list(adapter.fetch_since(None))
    adapter.close()
    assert [e.msg_id for e in batches[0]] == ["good-1", "good-2"]


def test_source_id_matches_plan():
    assert SOURCE_ID == "ms365"


def test_raw_email_as_email_row_shape():
    email = RawEmail(
        msg_id="ABC=",
        internet_message_id="<msg@example.com>",
        subject="Invoice",
        from_addr="billing@stripe.com",
        received_at=datetime(2026, 4, 10, 8, 30, tzinfo=UTC),
        has_attachments=True,
        body_preview="body",
    )
    row = email.as_email_row()
    assert row["msg_id"] == "ABC="
    assert row["source_adapter"] == "ms365"
    assert row["message_id_header"] == "<msg@example.com>"
    assert row["from_addr"] == "billing@stripe.com"
    assert row["subject"] == "Invoice"


def test_ms365_module_surfaces_source_id():
    assert ms365.SOURCE_ID == "ms365"
