"""Secrets wrapper around the system keyring.

Every OAuth refresh token, API key, and Wise RSA private key passes through
these helpers and nowhere else. Values live in the macOS Keychain; ``.env``
holds only the service names used as keyring lookup keys.

On macOS, ``keyring`` can silently fall back to an encrypted-file backend
(``keyrings.alt``) when the Keychain is unavailable. That fallback is
dramatically weaker than the Keychain and the plan rejects it outright.
``ensure_backend()`` pins us to ``keyring.backends.macOS.Keyring`` and
raises ``ConfigError`` if anything else loaded.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from typing import Final

import keyring
from keyring.backend import KeyringBackend

from execution.shared.errors import ConfigError

SERVICE_PREFIX: Final[str] = "granite-accounts"

# MOCK_MODE is a process-wide escape hatch for tests: when set, reads return
# an in-memory dict and writes are rejected so a test can't accidentally
# mutate the real user's Keychain.
_MOCK_MODE: bool = False
_MOCK_STORE: dict[tuple[str, str], str] = {}


def set_mock_mode(enabled: bool) -> None:
    """Enable/disable in-memory secrets store (tests only)."""
    global _MOCK_MODE
    _MOCK_MODE = enabled
    if not enabled:
        _MOCK_STORE.clear()


def is_mock() -> bool:
    return _MOCK_MODE or os.environ.get("GRANITE_MOCK") == "1"


def put(namespace: str, key: str, value: str) -> None:
    """Store ``value`` under ``granite-accounts/<namespace>`` + ``key``."""
    _validate_namespace(namespace)
    if is_mock():
        _MOCK_STORE[(namespace, key)] = value
        return
    ensure_backend()
    service = _service_name(namespace)
    keyring.set_password(service, key, value)


def get(namespace: str, key: str) -> str | None:
    """Fetch a secret. Returns ``None`` when unset."""
    _validate_namespace(namespace)
    if is_mock():
        return _MOCK_STORE.get((namespace, key))
    ensure_backend()
    service = _service_name(namespace)
    return keyring.get_password(service, key)


def require(namespace: str, key: str) -> str:
    """Fetch a secret or raise ``ConfigError`` if missing."""
    value = get(namespace, key)
    if value is None:
        raise ConfigError(
            f"missing keyring entry {_service_name(namespace)}/{key}",
            source=namespace,
            user_message=(
                f"Run `granite ops reauth {namespace}` or follow "
                f"directives/setup.md to populate Keychain entry for '{key}'."
            ),
        )
    return value


def delete(namespace: str, key: str) -> None:
    _validate_namespace(namespace)
    if is_mock():
        _MOCK_STORE.pop((namespace, key), None)
        return
    ensure_backend()
    service = _service_name(namespace)
    # Deleting a non-existent entry is idempotent for our purposes.
    with suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(service, key)


def _validate_namespace(namespace: str) -> None:
    if not namespace or "/" in namespace or not isinstance(namespace, str):
        raise ValueError(f"bad secrets namespace: {namespace!r}")


def ensure_backend() -> KeyringBackend:
    """Return the active keyring backend, pinned on macOS.

    On darwin we require ``keyring.backends.macOS.Keyring`` — the Keychain
    backend. On other platforms we accept whatever keyring chose; Phase 1A
    runs on macOS but tests may run in CI on Linux.
    """
    backend = keyring.get_keyring()
    if sys.platform == "darwin":
        # Class name check avoids importing a macOS-only module on Linux CI.
        module = getattr(type(backend), "__module__", "")
        if module != "keyring.backends.macOS":
            raise ConfigError(
                f"refusing to run: keyring backend is {module!r}, "
                "expected 'keyring.backends.macOS'. A fallback backend is a "
                "security downgrade and will not be used.",
                source="secrets",
                user_message=(
                    "Secrets handling requires the macOS Keychain backend. "
                    "Uninstall `keyrings.alt` and re-run."
                ),
            )
    return backend


def _service_name(namespace: str) -> str:
    _validate_namespace(namespace)
    return f"{SERVICE_PREFIX}/{namespace}"
