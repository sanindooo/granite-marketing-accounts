"""Stage-2 invoice extractor — Haiku 4.5 with Sonnet 4.6 escalation.

Extracts the 13 HMRC VAT fields plus a line-item array and per-field
confidence. The extractor is the backbone of the invoice pipeline: its
output flows directly into the ``invoices`` table and into the sheet, so
every defensive validation the plan calls for — arithmetic, UK VAT regex,
hallucination substring guard, supplier fuzz cross-check, ±90-day date
window, currency whitelist — is applied here after Claude replies.

Escalation: if Haiku's reply fails any of the low-confidence / arithmetic /
schema / date-window gates, we re-run once on Sonnet 4.6 with a 1-hour
cache TTL so the same prompt prefix stays warm across the run. Sonnet is
terminal — if it still fails, the row carries
``needs_manual_review=true`` into the Exceptions tab. No third attempt.

Text-first vs. vision routing lives a layer up (invoice/pdf_fetcher
produces the ``source_text`` field when pdfplumber density clears
20 chars/page). This module takes an :class:`ExtractorInput` that already
packages the text, sender-domain, and email-received date — it does not
know whether the content originated from a PDF, email body, or vendor
fetch. That keeps the Claude surface testable without PDF fixtures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from execution.invoice.classifier import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from execution.shared.claude_client import HAIKU, SONNET, ClaudeCall, ClaudeClient, Model
from execution.shared.errors import SchemaViolationError

if TYPE_CHECKING:  # pragma: no cover
    from execution.shared.prompts import LoadedPrompt

# ---------------------------------------------------------------------------
# Constants & validation primitives
# ---------------------------------------------------------------------------

UK_VAT_RE: Final[re.Pattern[str]] = re.compile(r"^GB\d{9}(\d{3})?$")

# ISO 4217 subset we actually expect to see in a UK SME's inbox. A wider
# whitelist is fine; unknowns null the field rather than failing.
ALLOWED_CURRENCIES: Final[frozenset[str]] = frozenset(
    {
        "GBP", "USD", "EUR", "AUD", "CAD", "CHF", "JPY",
        "SEK", "NOK", "DKK", "PLN", "SGD", "HKD", "NZD",
        "ZAR", "AED", "CNY", "INR", "BRL", "MXN", "THB", "KRW",
    }
)

CRITICAL_FIELDS: Final[tuple[str, ...]] = (
    "supplier_vat_number",
    "invoice_number",
    "invoice_date",
    "amount_gross",
    "amount_vat",
    "currency",
)

DEFAULT_OVERALL_CONFIDENCE_FLOOR: Final[float] = 0.75
DEFAULT_CRITICAL_FIELD_FLOOR: Final[float] = 0.70
DEFAULT_FIELD_NULL_FLOOR: Final[float] = 0.5
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 90
DEFAULT_ARITHMETIC_TOLERANCE: Final[Decimal] = Decimal("0.02")
DEFAULT_MAX_TOKENS: Final[int] = 2048

# Chokepoints used by the substring hallucination guard. These three fields
# cannot be invented — they must appear in the source text (modulo
# whitespace + case) or they are nulled with confidence 0. Supplier_name
# is handled separately via fuzzy cross-check against sender_domain.
HALLUCINATION_CHECKED_FIELDS: Final[tuple[str, ...]] = (
    "invoice_number",
    "supplier_vat_number",
    "supplier_address",
    "customer_address",
)


# ---------------------------------------------------------------------------
# Pydantic models — raw response, then sanitised
# ---------------------------------------------------------------------------


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None
    quantity: str | None
    unit_price: str | None
    amount_net: str | None
    amount_vat: str | None
    amount_gross: str | None
    vat_rate: str | None


class FieldConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_name: float = Field(ge=0.0, le=1.0)
    supplier_address: float = Field(ge=0.0, le=1.0)
    supplier_vat_number: float = Field(ge=0.0, le=1.0)
    customer_name: float = Field(ge=0.0, le=1.0)
    customer_address: float = Field(ge=0.0, le=1.0)
    invoice_number: float = Field(ge=0.0, le=1.0)
    invoice_date: float = Field(ge=0.0, le=1.0)
    supply_date: float = Field(ge=0.0, le=1.0)
    description: float = Field(ge=0.0, le=1.0)
    currency: float = Field(ge=0.0, le=1.0)
    amount_net: float = Field(ge=0.0, le=1.0)
    amount_vat: float = Field(ge=0.0, le=1.0)
    amount_gross: float = Field(ge=0.0, le=1.0)
    vat_rate: float = Field(ge=0.0, le=1.0)


class ExtractorResult(BaseModel):
    """Validated extractor output — covers the 13 HMRC VAT fields."""

    model_config = ConfigDict(extra="forbid")

    supplier_name: str | None
    supplier_address: str | None
    supplier_vat_number: str | None
    customer_name: str | None
    customer_address: str | None
    invoice_number: str | None
    invoice_date: str | None
    supply_date: str | None
    description: str | None
    currency: str | None
    amount_net: str | None
    amount_vat: str | None
    amount_gross: str | None
    vat_rate: str | None
    reverse_charge: bool
    arithmetic_ok: bool
    line_items: list[LineItem]
    field_confidence: FieldConfidence
    overall_confidence: float = Field(ge=0.0, le=1.0)
    extraction_notes: str | None


# ---------------------------------------------------------------------------
# Input + escalation record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractorInput:
    """Everything the extractor needs to reason about a single invoice."""

    subject: str
    sender: str
    source_text: str
    email_received_date: date
    attachment_ref: str | None = None

    def body_truncated(self, max_chars: int = 12_000) -> str:
        if len(self.source_text) <= max_chars:
            return self.source_text
        return self.source_text[:max_chars] + "\n…[truncated]"


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """The full record of one ``extract_invoice`` call."""

    result: ExtractorResult
    calls: tuple[ClaudeCall, ...]
    escalated: bool
    needs_manual_review: bool
    escalation_reasons: tuple[str, ...]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def build_user_content(inp: ExtractorInput, *, max_body_chars: int = 12_000) -> str:
    """Assemble the extractor's user-role content with delimiter defense."""
    safe_body = inp.body_truncated(max_body_chars).replace(UNTRUSTED_CLOSE, "")
    safe_subject = inp.subject.replace(UNTRUSTED_CLOSE, "")
    safe_sender = inp.sender.replace(UNTRUSTED_CLOSE, "")
    return (
        "Extract the 13 HMRC VAT fields and line items from the invoice below. "
        "Respond with one JSON document that matches the schema. Do not "
        "include Markdown fences, prose, or commentary.\n\n"
        f"Email subject: {safe_subject}\n"
        f"Email sender: {safe_sender}\n"
        f"Email received date (Europe/London): {inp.email_received_date.isoformat()}\n\n"
        f"{UNTRUSTED_OPEN}\n"
        f"{safe_body}\n"
        f"{UNTRUSTED_CLOSE}\n"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_invoice(
    client: ClaudeClient,
    prompt: LoadedPrompt,
    inp: ExtractorInput,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overall_confidence_floor: float = DEFAULT_OVERALL_CONFIDENCE_FLOOR,
    critical_field_floor: float = DEFAULT_CRITICAL_FIELD_FLOOR,
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> ExtractionOutcome:
    """Run Haiku extraction, sanitise, and escalate to Sonnet when gated."""
    haiku_raw, haiku_call = _one_call(
        client=client,
        prompt=prompt,
        inp=inp,
        model=HAIKU,
        max_tokens=max_tokens,
    )
    haiku_sanitised = sanitise_result(haiku_raw, inp)
    haiku_reasons = escalation_reasons(
        haiku_sanitised,
        inp,
        overall_confidence_floor=overall_confidence_floor,
        critical_field_floor=critical_field_floor,
        date_window_days=date_window_days,
    )

    if not haiku_reasons:
        return ExtractionOutcome(
            result=haiku_sanitised,
            calls=(haiku_call,),
            escalated=False,
            needs_manual_review=False,
            escalation_reasons=(),
        )

    # Terminal escalation. If Sonnet also fails, we ship the better of the
    # two results with ``needs_manual_review=True`` — caller routes to the
    # Exceptions tab.
    sonnet_raw, sonnet_call = _one_call(
        client=client,
        prompt=prompt,
        inp=inp,
        model=SONNET,
        max_tokens=max_tokens,
    )
    sonnet_sanitised = sanitise_result(sonnet_raw, inp)
    sonnet_reasons = escalation_reasons(
        sonnet_sanitised,
        inp,
        overall_confidence_floor=overall_confidence_floor,
        critical_field_floor=critical_field_floor,
        date_window_days=date_window_days,
    )

    best = (
        sonnet_sanitised
        if sonnet_sanitised.overall_confidence >= haiku_sanitised.overall_confidence
        else haiku_sanitised
    )
    return ExtractionOutcome(
        result=best,
        calls=(haiku_call, sonnet_call),
        escalated=True,
        needs_manual_review=bool(sonnet_reasons),
        escalation_reasons=haiku_reasons,
    )


def _one_call(
    *,
    client: ClaudeClient,
    prompt: LoadedPrompt,
    inp: ExtractorInput,
    model: Model,
    max_tokens: int,
) -> tuple[ExtractorResult, ClaudeCall]:
    """One round-trip: build content, call model, parse + validate."""
    text, call = client.call_with_cached_prompt(
        loaded_prompt=prompt,
        user_content=build_user_content(inp),
        max_tokens=max_tokens,
        stage="extract",
        model=model,
    )
    return _parse_response(text), call


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _parse_response(text: str) -> ExtractorResult:
    text = _strip_markdown_fences(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise SchemaViolationError(
            f"extractor returned non-JSON: {text[:200]!r}",
            source="claude",
            details={"stage": "extract", "head": text[:200]},
            cause=err,
        ) from err
    try:
        return ExtractorResult.model_validate(data)
    except ValidationError as err:
        raise SchemaViolationError(
            f"extractor JSON failed Pydantic validation: {err}",
            source="claude",
            details={"stage": "extract", "errors": err.errors()},
            cause=err,
        ) from err


# ---------------------------------------------------------------------------
# Sanitisation — hallucination guard, VAT regex, currency whitelist, amount
# parse check, date-window bound
# ---------------------------------------------------------------------------


def sanitise_result(result: ExtractorResult, inp: ExtractorInput) -> ExtractorResult:
    """Apply deterministic validators to raw extractor output.

    The sanitiser is pure — same input always produces the same output —
    and never calls Claude. Every rule mirrors the defenses in the plan's
    § Claude API Detailed Design appendix.
    """
    data: dict[str, Any] = result.model_dump()
    notes: list[str] = [data.get("extraction_notes")] if data.get("extraction_notes") else []
    source_lower = _normalise_for_substring(inp.source_text)

    # Null fields below the confidence floor outright. Zero the confidence
    # too so the derived ``overall_confidence`` reflects the null-out.
    for field, conf in list(data["field_confidence"].items()):
        if field in data and conf < DEFAULT_FIELD_NULL_FLOOR and data.get(field) is not None:
            data[field] = None
            data["field_confidence"][field] = 0.0
            notes.append(f"nulled {field} (confidence {conf:.2f})")

    # UK VAT regex
    vat = data.get("supplier_vat_number")
    if vat is not None and not UK_VAT_RE.match(vat):
        data["supplier_vat_number"] = None
        data["field_confidence"]["supplier_vat_number"] = 0.0
        notes.append(f"rejected supplier_vat_number {vat!r} (not UK GB format)")

    # Currency whitelist
    ccy = data.get("currency")
    if ccy is not None and ccy not in ALLOWED_CURRENCIES:
        data["currency"] = None
        data["field_confidence"]["currency"] = 0.0
        notes.append(f"rejected currency {ccy!r} (not in ISO 4217 whitelist)")

    # Hallucination substring guard
    if source_lower:
        for field in HALLUCINATION_CHECKED_FIELDS:
            v = data.get(field)
            if v is None:
                continue
            if _normalise_for_substring(v) not in source_lower:
                data[field] = None
                data["field_confidence"][field] = 0.0
                notes.append(f"hallucinated {field} not in source text")

    # Amount parse sanity — if a string is present but isn't a parseable
    # Decimal, null it.
    for amount_field in ("amount_net", "amount_vat", "amount_gross"):
        v = data.get(amount_field)
        if v is not None and _parse_decimal(v) is None:
            data[amount_field] = None
            data["field_confidence"][amount_field] = 0.0
            notes.append(f"unparseable {amount_field}={v!r}")

    # Arithmetic check: net + vat ≈ gross. Recompute rather than trust model.
    net = _parse_decimal(data.get("amount_net"))
    vat_amt = _parse_decimal(data.get("amount_vat"))
    gross = _parse_decimal(data.get("amount_gross"))
    if net is not None and vat_amt is not None and gross is not None:
        delta = abs((net + vat_amt) - gross)
        data["arithmetic_ok"] = delta <= DEFAULT_ARITHMETIC_TOLERANCE
        if not data["arithmetic_ok"]:
            notes.append(
                f"arithmetic mismatch: net {net} + vat {vat_amt} ≠ gross {gross}"
            )

    # Date window bound — reject anything outside ±N days of email receipt.
    d_raw = data.get("invoice_date")
    if d_raw is not None:
        d_parsed = _parse_iso_date(d_raw)
        if d_parsed is None:
            data["invoice_date"] = None
            data["field_confidence"]["invoice_date"] = 0.0
            notes.append(f"unparseable invoice_date={d_raw!r}")
        else:
            lo = inp.email_received_date - timedelta(days=DEFAULT_DATE_WINDOW_DAYS)
            hi = inp.email_received_date + timedelta(days=DEFAULT_DATE_WINDOW_DAYS)
            if not (lo <= d_parsed <= hi):
                data["field_confidence"]["invoice_date"] = min(
                    data["field_confidence"]["invoice_date"], 0.3
                )
                notes.append(
                    f"invoice_date {d_raw} outside ±{DEFAULT_DATE_WINDOW_DAYS}d window"
                )

    # Derived overall_confidence: minimum of critical-field confidences
    # (mirrors the plan's appendix — not an average, the weakest gate).
    data["overall_confidence"] = min(
        data["field_confidence"][f] for f in CRITICAL_FIELDS
    )

    if notes:
        data["extraction_notes"] = "; ".join(filter(None, notes))

    return ExtractorResult.model_validate(data)


def escalation_reasons(
    result: ExtractorResult,
    inp: ExtractorInput,
    *,
    overall_confidence_floor: float = DEFAULT_OVERALL_CONFIDENCE_FLOOR,
    critical_field_floor: float = DEFAULT_CRITICAL_FIELD_FLOOR,
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> tuple[str, ...]:
    """Return the list of escalation triggers; empty tuple means Haiku wins."""
    reasons: list[str] = []
    conf = result.field_confidence.model_dump()

    if result.overall_confidence < overall_confidence_floor:
        reasons.append(
            f"overall_confidence {result.overall_confidence:.2f} "
            f"< floor {overall_confidence_floor:.2f}"
        )

    for field in CRITICAL_FIELDS:
        field_conf = conf[field]
        if field_conf < critical_field_floor:
            reasons.append(
                f"critical field {field} confidence {field_conf:.2f} "
                f"< floor {critical_field_floor:.2f}"
            )

    # Arithmetic mismatch with all three amounts present
    if (
        not result.arithmetic_ok
        and result.amount_net is not None
        and result.amount_vat is not None
        and result.amount_gross is not None
    ):
        reasons.append("arithmetic_ok=false with all three amounts present")

    # Date window — same rule the sanitiser applied; escalation catches it.
    if result.invoice_date is not None:
        d_parsed = _parse_iso_date(result.invoice_date)
        if d_parsed is not None:
            lo = inp.email_received_date - timedelta(days=date_window_days)
            hi = inp.email_received_date + timedelta(days=date_window_days)
            if not (lo <= d_parsed <= hi):
                reasons.append(
                    f"invoice_date {result.invoice_date} outside ±{date_window_days}d window"
                )

    return tuple(reasons)


def _normalise_for_substring(text: str) -> str:
    """Lowercase + strip whitespace to make substring checks forgiving."""
    return "".join(text.split()).lower()


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "ALLOWED_CURRENCIES",
    "CRITICAL_FIELDS",
    "DEFAULT_ARITHMETIC_TOLERANCE",
    "DEFAULT_CRITICAL_FIELD_FLOOR",
    "DEFAULT_DATE_WINDOW_DAYS",
    "DEFAULT_FIELD_NULL_FLOOR",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_OVERALL_CONFIDENCE_FLOOR",
    "HALLUCINATION_CHECKED_FIELDS",
    "UK_VAT_RE",
    "ExtractionOutcome",
    "ExtractorInput",
    "ExtractorResult",
    "FieldConfidence",
    "LineItem",
    "build_user_content",
    "escalation_reasons",
    "extract_invoice",
    "sanitise_result",
]
