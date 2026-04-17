"""Frozen value types that cross module boundaries.

These are the stable internal domain model. Adapters convert raw provider
payloads into these at ingest; the reconciler and sheet writer work off
these exclusively. Every ``Decimal`` has been through ``money.to_money``;
every ``datetime`` is tz-aware UTC.

Pydantic models for external boundaries (Claude responses, sheet cell
reads, bank API JSON) live in the modules that own those boundaries —
``shared.claude_client``, ``output.sheet``, ``adapters.*``. They do runtime
coercion + validation; these dataclasses trust their inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Category = Literal[
    "software",
    "travel",
    "meals",
    "hardware",
    "professional",
    "advertising",
    "utilities",
    "other",
]

TxnType = Literal["purchase", "income", "transfer", "refund"]
TxnStatus = Literal["pending", "settled", "reversed", "email_preview"]
EmailOutcome = Literal[
    "invoice",
    "receipt",
    "statement",
    "neither",
    "error",
    "pending",
    "duplicate_resend",
]

ReconState = Literal[
    "new",
    "auto_matched",
    "suggested",
    "unmatched",
    "user_verified",
    "user_overridden",
    "user_personal",
    "user_ignore",
    "voided",
]

LinkKind = Literal[
    "full",
    "partial",
    "split_invoice",
    "split_txn",
    "transfer_pair",
]


@dataclass(frozen=True, slots=True)
class Email:
    """Email row in the idempotency store."""

    msg_id: str  # provider-stable: X-GM-MSGID / MS Graph id / IMAP UID+validity
    source_adapter: str
    message_id_header: str | None
    received_at: datetime
    from_addr: str
    subject: str
    processed_at: datetime | None = None
    classifier_version: str | None = None
    outcome: EmailOutcome | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class Invoice:
    """Extracted invoice, possibly before the paying transaction is known."""

    invoice_id: str  # sha256(msg_id + attachment_index) [+ hash_schema_version]
    source_msg_id: str
    vendor_id: str
    vendor_name_raw: str
    invoice_number: str  # always populated; synthesized surrogate if needed
    invoice_date: date
    currency: str
    amount_net: Decimal | None
    amount_vat: Decimal | None
    amount_gross: Decimal
    vat_rate: Decimal | None
    vat_number_supplier: str | None
    reverse_charge: bool
    category: Category
    category_source: Literal["llm", "user", "rule", "hint"]
    drive_file_id: str | None
    drive_web_view_link: str | None
    confidence: dict[str, float] = field(default_factory=dict)
    classifier_version: str = "unknown"
    hash_schema_version: int = 1
    is_business: bool | None = None  # filled retroactively by the matcher
    deleted_at: datetime | None = None
    deleted_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Transaction:
    """Normalised bank transaction."""

    txn_id: str
    account: Literal["amex", "wise", "monzo"]
    txn_type: TxnType
    booking_date: date
    description_raw: str
    description_canonical: str
    currency: str
    amount: Decimal  # native
    amount_gbp: Decimal  # converted (equal to amount for GBP)
    fx_rate: Decimal | None
    status: TxnStatus
    provider_auth_id: str | None
    source: Literal["csv", "api", "email_parse"]
    category: str | None = None  # e.g. "bank_fee"; None for generic purchases
    hash_schema_version: int = 1
    deleted_at: datetime | None = None
    deleted_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationRow:
    """A reconciliation row — the logical group that owns human-edit state.

    Physical invoice↔transaction pairings live in ``reconciliation_links``;
    this row carries the state machine and any user notes. For a simple
    1:1 match, one row + one link.
    """

    row_id: str
    invoice_id: str | None
    txn_id: str | None
    fiscal_year: str
    state: ReconState
    match_score: Decimal
    match_reason: str
    user_note: str
    cross_fy_flag: bool
    override_history: str  # append-only JSONL
    updated_at: datetime
    last_run_id: str


@dataclass(frozen=True, slots=True)
class ReconciliationLink:
    """Physical join row: one invoice, one txn, optional partial allocation."""

    row_id: str
    invoice_id: str | None
    txn_id: str | None
    allocated_amount_gbp: Decimal
    link_kind: LinkKind


@dataclass(frozen=True, slots=True)
class FxRate:
    """Cached FX rate (1 unit of ``from_ccy`` in ``to_ccy``)."""

    date: date
    from_ccy: str
    to_ccy: str
    rate: Decimal
    source: Literal["ecb", "frankfurter", "mock"] = "ecb"


@dataclass(frozen=True, slots=True)
class RunStatus:
    """Per-invocation stats row written to the ``runs`` table."""

    run_id: str
    started_at: datetime
    ended_at: datetime | None
    status: Literal["running", "ok", "partial", "failed"]
    stats_json: str  # JSON blob; see plan §Observability Contracts
    cost_gbp: Decimal
