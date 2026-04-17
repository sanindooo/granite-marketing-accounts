"""PipelineError hierarchy + agent-native stdout helpers."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from execution.shared.errors import (
    AuthExpiredError,
    BudgetExceededError,
    ConfigError,
    DataQualityError,
    PathViolationError,
    PipelineError,
    RateLimitedError,
    SchemaViolationError,
    SSRFValidationError,
    emit_error,
    emit_success,
)


class TestHierarchy:
    def test_subclasses_inherit_pipeline_error(self) -> None:
        for cls in (
            AuthExpiredError,
            RateLimitedError,
            SchemaViolationError,
            DataQualityError,
            ConfigError,
            BudgetExceededError,
            PathViolationError,
            SSRFValidationError,
        ):
            assert issubclass(cls, PipelineError)

    def test_retryable_flags(self) -> None:
        assert RateLimitedError("x").retryable is True
        assert AuthExpiredError("x").retryable is False
        assert SchemaViolationError("x").retryable is False
        assert DataQualityError("x").retryable is False
        assert ConfigError("x").retryable is False
        assert BudgetExceededError("x").retryable is False
        assert PathViolationError("x").retryable is False
        assert SSRFValidationError("x").retryable is False

    def test_category_labels(self) -> None:
        assert RateLimitedError("x").category == "rate_limit"
        assert AuthExpiredError("x").category == "auth"
        assert SchemaViolationError("x").category == "schema"
        assert DataQualityError("x").category == "data_quality"
        assert ConfigError("x").category == "config"
        assert BudgetExceededError("x").category == "budget"
        assert PathViolationError("x").category == "security"
        assert SSRFValidationError("x").category == "security"


class TestPayload:
    def test_payload_shape(self) -> None:
        err = RateLimitedError(
            "429 from api",
            source="ms365",
            user_message="try again in a minute",
            details={"retry_after": 60},
        )
        payload = err.to_payload()
        assert payload["status"] == "error"
        assert payload["error_code"] == "rate_limited"
        assert payload["category"] == "rate_limit"
        assert payload["source"] == "ms365"
        assert payload["retryable"] is True
        assert payload["message"] == "429 from api"
        assert payload["user_message"] == "try again in a minute"
        assert payload["details"] == {"retry_after": 60}

    def test_user_message_defaults_to_message(self) -> None:
        err = ConfigError("missing keyring entry")
        assert err.to_payload()["user_message"] == "missing keyring entry"

    def test_cause_recorded(self) -> None:
        original = ValueError("bad input")
        err = SchemaViolationError("parse failed", cause=original)
        assert err.__cause__ is original


class TestEmitHelpers:
    def test_emit_success_writes_single_json_line(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit_success({"count": 3, "watermark": "2026-04-17"})
        output = buf.getvalue()
        assert output.endswith("\n")
        doc = json.loads(output)
        assert doc["status"] == "success"
        assert doc["count"] == 3
        assert doc["watermark"] == "2026-04-17"

    def test_emit_error_for_pipeline_error_exits_nonzero(self) -> None:
        buf = io.StringIO()
        err = AuthExpiredError("token revoked", source="gmail")
        with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            emit_error(err)
        assert exc.value.code == 1
        doc = json.loads(buf.getvalue())
        assert doc["status"] == "error"
        assert doc["error_code"] == "needs_reauth"
        assert doc["source"] == "gmail"

    def test_emit_error_for_bare_exception(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            emit_error(RuntimeError("boom"))
        doc = json.loads(buf.getvalue())
        assert doc["status"] == "error"
        assert doc["error_code"] == "unhandled_exception"
        assert doc["details"]["exception_type"] == "RuntimeError"
