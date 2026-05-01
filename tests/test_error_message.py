"""Tests for error_message redaction + capping helper."""

from __future__ import annotations

from execution.shared.error_message import (
    ERROR_MESSAGE_CAP,
    prepare_error_message,
    redact_error_message,
)


def test_prepare_passes_none_through() -> None:
    assert prepare_error_message(None) is None


def test_prepare_redacts_and_caps() -> None:
    long = "x" * (ERROR_MESSAGE_CAP + 1000)
    out = prepare_error_message(long)
    assert out is not None
    assert len(out) == ERROR_MESSAGE_CAP


def test_redacts_bearer_token() -> None:
    msg = "Authorization: Bearer abc.def.ghi-_jkl/mno=pqr 401 from upstream"
    redacted = redact_error_message(msg)
    assert "abc.def.ghi" not in redacted
    assert "Bearer <redacted>" in redacted


def test_redacts_stripe_hosted_invoice_token() -> None:
    msg = "GET https://invoice.stripe.com/i/acct_X_in_Y_secret_Z 503"
    redacted = redact_error_message(msg)
    assert "secret_Z" not in redacted
    assert "invoice.stripe.com/i/<token>" in redacted


def test_redacts_signed_url_query_params() -> None:
    msg = "url=https://x.example/file?token=DEADBEEF&signature=AAA1 4xx"
    redacted = redact_error_message(msg)
    assert "DEADBEEF" not in redacted
    assert "AAA1" not in redacted
    assert "token=<redacted>" in redacted
    assert "signature=<redacted>" in redacted


def test_redacts_email_addresses() -> None:
    msg = "RecipientNotFound for steve@example.co.uk on smtp send"
    redacted = redact_error_message(msg)
    assert "steve@example.co.uk" not in redacted
    assert "<email>" in redacted


def test_safe_text_passes_through() -> None:
    msg = "ConnectionTimeout: upstream unresponsive after 30s"
    assert redact_error_message(msg) == msg
