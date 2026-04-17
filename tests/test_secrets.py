"""Secrets wrapper — tests use mock mode exclusively so the real Keychain
is never touched."""

from __future__ import annotations

import pytest

from execution.shared import secrets
from execution.shared.errors import ConfigError


def test_put_and_get_roundtrip(mock_secrets: None) -> None:
    secrets.put("anthropic", "api_key", "sk-ant-test")
    assert secrets.get("anthropic", "api_key") == "sk-ant-test"


def test_missing_key_returns_none(mock_secrets: None) -> None:
    assert secrets.get("anthropic", "missing") is None


def test_require_raises_on_missing(mock_secrets: None) -> None:
    with pytest.raises(ConfigError, match="missing keyring entry"):
        secrets.require("anthropic", "api_key")


def test_delete_is_idempotent(mock_secrets: None) -> None:
    secrets.delete("anthropic", "not_there")  # does not raise
    secrets.put("anthropic", "api_key", "x")
    secrets.delete("anthropic", "api_key")
    assert secrets.get("anthropic", "api_key") is None


def test_bad_namespace_rejected(mock_secrets: None) -> None:
    with pytest.raises(ValueError):
        secrets.put("bad/ns", "k", "v")
