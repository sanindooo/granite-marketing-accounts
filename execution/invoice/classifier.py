"""Stage-1 email classifier — Haiku 4.5 with strict JSON output.

Wraps untrusted email content in ``<untrusted_email>…</untrusted_email>``
delimiters and strips the close tag from the body first so an attacker
cannot inject a new system prompt by writing the tag inside their own text.

Returns a :class:`ClassifierResult` validated through Pydantic against the
JSON Schema that lives alongside the prompt text
(``execution/invoice/prompts/classifier.schema.json``). A JSON-decode or
validation failure raises :class:`SchemaViolationError` so the caller can
decide whether to retry or escalate — the two-stage pipeline never silently
swallows a malformed response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from execution.shared.claude_client import HAIKU, ClaudeCall, ClaudeClient
from execution.shared.errors import SchemaViolationError

if TYPE_CHECKING:  # pragma: no cover
    from execution.shared.prompts import LoadedPrompt

Classification = Literal["invoice", "receipt", "statement", "neither"]

UNTRUSTED_OPEN: Final[str] = "<untrusted_email>"
UNTRUSTED_CLOSE: Final[str] = "</untrusted_email>"
DEFAULT_MAX_TOKENS: Final[int] = 512


class ClassifierSignals(BaseModel):
    """Boolean signals the classifier observes. Used downstream, not by us."""

    model_config = ConfigDict(extra="forbid")

    has_attachment_mentioned: bool
    sender_domain_known_vendor: bool
    contains_amount: bool
    looks_like_marketing: bool


class ClassifierResult(BaseModel):
    """Validated classifier output."""

    model_config = ConfigDict(extra="forbid")

    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=400)
    signals: ClassifierSignals


@dataclass(frozen=True, slots=True)
class EmailInput:
    """The subset of an email a classifier decision needs."""

    subject: str
    sender: str
    body: str

    def body_truncated(self, max_chars: int = 4000) -> str:
        if len(self.body) <= max_chars:
            return self.body
        return self.body[:max_chars] + "\n…[truncated]"


def build_user_content(email: EmailInput, *, max_body_chars: int = 4000) -> str:
    """Assemble the classifier's user-role content with delimiter defense.

    The close-tag is stripped from the body *before* wrapping so an attacker
    cannot write ``</untrusted_email>`` inside the body and inject
    new instructions.
    """
    safe_body = email.body_truncated(max_body_chars).replace(UNTRUSTED_CLOSE, "")
    safe_subject = email.subject.replace(UNTRUSTED_CLOSE, "")
    safe_sender = email.sender.replace(UNTRUSTED_CLOSE, "")
    return (
        "Classify the following email. Respond with one JSON document that "
        "matches the schema. Do not include Markdown fences, prose, or "
        "commentary.\n\n"
        f"{UNTRUSTED_OPEN}\n"
        f"Subject: {safe_subject}\n"
        f"From: {safe_sender}\n"
        "Body:\n"
        f"{safe_body}\n"
        f"{UNTRUSTED_CLOSE}\n"
    )


def classify_email(
    client: ClaudeClient,
    prompt: LoadedPrompt,
    email: EmailInput,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_body_chars: int = 4000,
) -> tuple[ClassifierResult, ClaudeCall]:
    """Classify ``email``. Returns the parsed result and the call record.

    Raises :class:`SchemaViolationError` on a malformed or schema-invalid
    response. The caller decides whether to retry, escalate to Sonnet, or
    park the email in the Exceptions tab.
    """
    user_content = build_user_content(email, max_body_chars=max_body_chars)
    text, call = client.call_with_cached_prompt(
        loaded_prompt=prompt,
        user_content=user_content,
        max_tokens=max_tokens,
        stage="classify",
        model=HAIKU,
    )
    return _parse_response(text), call


def _parse_response(text: str) -> ClassifierResult:
    """Parse + validate the classifier response."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise SchemaViolationError(
            f"classifier returned non-JSON: {text[:200]!r}",
            source="claude",
            details={"stage": "classify", "head": text[:200]},
            cause=err,
        ) from err
    try:
        return ClassifierResult.model_validate(data)
    except ValidationError as err:
        raise SchemaViolationError(
            f"classifier JSON failed Pydantic validation: {err}",
            source="claude",
            details={"stage": "classify", "errors": err.errors()},
            cause=err,
        ) from err


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "Classification",
    "ClassifierResult",
    "ClassifierSignals",
    "EmailInput",
    "build_user_content",
    "classify_email",
]
