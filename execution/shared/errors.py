"""PipelineError hierarchy + agent-native stdout helpers.

Every adapter and stage raises a ``PipelineError`` subclass. The exception's
``retryable`` flag drives tenacity retry decisions so retry logic never
branches on error-code strings.

Every script emits exactly one JSON document on stdout before exiting, via
``emit_success()`` on the happy path and ``emit_error()`` on failure. This is
the agent-native output contract from ``CLAUDE.md``.
"""

from __future__ import annotations

import json
import sys
from typing import Any


class PipelineError(Exception):
    """Base for every error raised inside the pipeline.

    Subclasses carry enough metadata that a caller can decide to retry,
    surface to the user for re-auth, or file an ``Exceptions`` sheet row —
    without inspecting the message string.
    """

    source: str = "unknown"
    category: str = "unknown"
    retryable: bool = False
    error_code: str = "pipeline_error"

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        user_message: str | None = None,
        cause: BaseException | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if source is not None:
            self.source = source
        self.user_message = user_message or message
        self.details = details or {}
        if cause is not None:
            self.__cause__ = cause

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": self.error_code,
            "category": self.category,
            "source": self.source,
            "retryable": self.retryable,
            "message": str(self),
            "user_message": self.user_message,
            "details": self.details,
        }


class AuthExpiredError(PipelineError):
    """OAuth / API token expired or revoked; user must re-authorize."""

    category = "auth"
    retryable = False
    error_code = "needs_reauth"


class RateLimitedError(PipelineError):
    """429 / 529 style backpressure; retry after backoff."""

    category = "rate_limit"
    retryable = True
    error_code = "rate_limited"


class SchemaViolationError(PipelineError):
    """External response didn't match our expected schema."""

    category = "schema"
    retryable = False
    error_code = "schema_violation"


class DataQualityError(PipelineError):
    """Parseable response but the data itself is implausible."""

    category = "data_quality"
    retryable = False
    error_code = "data_quality"


class ConfigError(PipelineError):
    """Misconfiguration — missing Keychain entry, missing env, bad backend."""

    category = "config"
    retryable = False
    error_code = "config_error"


class BudgetExceededError(PipelineError):
    """A per-run Claude (or similar) cost ceiling was reached."""

    category = "budget"
    retryable = False
    error_code = "budget_exceeded"


class PathViolationError(PipelineError):
    """A write path resolved outside its allowed sandbox."""

    category = "security"
    retryable = False
    error_code = "path_violation"


class SSRFValidationError(PipelineError):
    """A URL was rejected by SSRF validation."""

    category = "security"
    retryable = False
    error_code = "ssrf_rejected"


def emit_success(payload: dict[str, Any]) -> None:
    """Write a single success JSON document to stdout."""
    doc = {"status": "success", **payload}
    sys.stdout.write(json.dumps(doc, default=_json_default))
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_error(err: PipelineError | Exception, *, exit_code: int = 1) -> None:
    """Write a single error JSON document to stdout and exit non-zero."""
    if isinstance(err, PipelineError):
        doc = err.to_payload()
    else:
        doc = {
            "status": "error",
            "error_code": "unhandled_exception",
            "category": "unknown",
            "source": "unknown",
            "retryable": False,
            "message": str(err),
            "user_message": str(err),
            "details": {"exception_type": type(err).__name__},
        }
    sys.stdout.write(json.dumps(doc, default=_json_default))
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def _json_default(obj: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
