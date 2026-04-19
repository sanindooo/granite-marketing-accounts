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

Supports few-shot learning from user feedback: dismissed emails become
negative examples, successfully processed invoices become positive examples.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from execution.shared.budget import LLMCall
from execution.shared.errors import SchemaViolationError
from execution.shared.llm_client import LLMClient

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


@dataclass(frozen=True, slots=True)
class FeedbackExample:
    """A feedback example for few-shot learning."""

    subject: str
    sender: str
    body_snippet: str
    classification: Classification
    is_positive: bool


def load_feedback_examples(
    conn: sqlite3.Connection,
    *,
    max_positive: int = 5,
    max_negative: int = 5,
) -> list[FeedbackExample]:
    """Load recent feedback examples for few-shot prompting.

    Positive examples: emails successfully processed as invoice/receipt.
    Negative examples: emails dismissed as not_invoice.

    Args:
        conn: Database connection.
        max_positive: Maximum positive examples to include.
        max_negative: Maximum negative examples to include.

    Returns:
        List of FeedbackExample instances, mixed positive and negative.
    """
    examples: list[FeedbackExample] = []

    # Negative examples from email_feedback (user dismissed as not_invoice)
    try:
        negative_rows = conn.execute(
            """
            SELECT ef.from_addr, ef.subject, e.outcome
            FROM email_feedback ef
            JOIN emails e ON ef.msg_id = e.msg_id
            WHERE ef.feedback_value = 'not_invoice'
            ORDER BY ef.created_at DESC
            LIMIT ?
            """,
            (max_negative,),
        ).fetchall()

        for row in negative_rows:
            examples.append(
                FeedbackExample(
                    subject=row[1] or "",
                    sender=row[0] or "",
                    body_snippet="",
                    classification="neither",
                    is_positive=False,
                )
            )
    except Exception:  # noqa: S110
        # Table may not exist yet during initial setup
        pass

    # Positive examples from emails (successfully processed as invoice/receipt)
    try:
        positive_rows = conn.execute(
            """
            SELECT from_addr, subject, outcome
            FROM emails
            WHERE outcome IN ('invoice', 'receipt')
              AND dismissed_at IS NULL
            ORDER BY processed_at DESC
            LIMIT ?
            """,
            (max_positive,),
        ).fetchall()

        for row in positive_rows:
            classification: Classification = row[2] if row[2] in ("invoice", "receipt") else "invoice"
            examples.append(
                FeedbackExample(
                    subject=row[1] or "",
                    sender=row[0] or "",
                    body_snippet="",
                    classification=classification,
                    is_positive=True,
                )
            )
    except Exception:  # noqa: S110
        # Table may not exist yet during initial setup
        pass

    return examples


def _format_feedback_examples(examples: list[FeedbackExample]) -> str:
    """Format feedback examples into a few-shot section."""
    if not examples:
        return ""

    lines = [
        "\n## Recent examples from your inbox\n",
        "These are real emails you've processed. Use them to calibrate:\n",
    ]

    for i, ex in enumerate(examples, 1):
        label = ex.classification
        safe_subject = ex.subject.replace(UNTRUSTED_CLOSE, "")[:100]
        safe_sender = ex.sender.replace(UNTRUSTED_CLOSE, "")[:60]
        lines.append(f"\n### Example {i} — {label}\n")
        lines.append(f"From: {safe_sender}\n")
        lines.append(f"Subject: {safe_subject}\n")

    return "".join(lines)


def build_user_content(
    email: EmailInput,
    *,
    max_body_chars: int = 4000,
    feedback_examples: list[FeedbackExample] | None = None,
) -> str:
    """Assemble the classifier's user-role content with delimiter defense.

    The close-tag is stripped from the body *before* wrapping so an attacker
    cannot write ``</untrusted_email>`` inside the body and inject
    new instructions.

    Args:
        email: The email to classify.
        max_body_chars: Maximum body length before truncation.
        feedback_examples: Optional feedback examples for few-shot learning.
    """
    safe_body = email.body_truncated(max_body_chars).replace(UNTRUSTED_CLOSE, "")
    safe_subject = email.subject.replace(UNTRUSTED_CLOSE, "")
    safe_sender = email.sender.replace(UNTRUSTED_CLOSE, "")

    examples_section = _format_feedback_examples(feedback_examples or [])

    return (
        "Classify the following email. Respond with one JSON document that "
        "matches the schema. Do not include Markdown fences, prose, or "
        "commentary.\n"
        f"{examples_section}\n"
        f"{UNTRUSTED_OPEN}\n"
        f"Subject: {safe_subject}\n"
        f"From: {safe_sender}\n"
        "Body:\n"
        f"{safe_body}\n"
        f"{UNTRUSTED_CLOSE}\n"
    )


def classify_email(
    client: LLMClient,
    prompt: LoadedPrompt,
    email: EmailInput,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_body_chars: int = 4000,
    feedback_examples: list[FeedbackExample] | None = None,
) -> tuple[ClassifierResult, LLMCall]:
    """Classify ``email``. Returns the parsed result and the call record.

    Raises :class:`SchemaViolationError` on a malformed or schema-invalid
    response. The caller decides whether to retry, escalate to Sonnet, or
    park the email in the Exceptions tab.

    Args:
        client: LLM client for API calls.
        prompt: Loaded classifier prompt.
        email: Email to classify.
        max_tokens: Maximum tokens for response.
        max_body_chars: Maximum body length before truncation.
        feedback_examples: Optional feedback examples for few-shot learning.
    """
    user_content = build_user_content(
        email,
        max_body_chars=max_body_chars,
        feedback_examples=feedback_examples,
    )
    text, call = client.complete(
        loaded_prompt=prompt,
        user_content=user_content,
        max_tokens=max_tokens,
        stage="classify",
    )
    return _parse_response(text), call


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last line if it's just ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _parse_response(text: str) -> ClassifierResult:
    """Parse + validate the classifier response."""
    text = _strip_markdown_fences(text)
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
    "FeedbackExample",
    "build_user_content",
    "classify_email",
    "load_feedback_examples",
]
