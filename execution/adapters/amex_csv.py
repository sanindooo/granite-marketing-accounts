"""Amex UK CSV statement adapter.

Amex's UK consumer/business cards don't have a first-party developer API
usable for a sole-trader Ltd, and the aggregator routes (GoCardless BAD,
TrueLayer, Plaid UK) are either closed to new sign-ups or cost more than
the tool is worth. The pragmatic fallback — and the one the plan carries
forward — is to drive the pipeline off the official monthly CSV export.

This adapter reads CSVs from a drop folder (``~/Downloads/Amex`` by
default; configurable). For each file it:

1. Streams the file through :func:`csv.DictReader` and asserts the
   first row's column set matches the canonical schema. A mismatch
   raises :class:`SchemaViolationError` — do not best-effort parse an
   unknown CSV shape, write an Exceptions-tab entry instead.
2. Computes a stable ``txn_id``. The CSV's ``Reference`` column is
   preferred (Amex ships a per-charge reference that is stable across
   re-downloads). When absent we fall back to a deterministic hash of
   ``(account, booking_date, canonical_description, amount,
   row_ordinal_within_day)`` — the ``row_ordinal`` disambiguates two
   identical £3.50 coffees on the same day.
3. Canonicalises the description (``description_canonical``) by
   uppercasing, collapsing whitespace, dropping trailing city/state
   tokens, and dropping trailing ``\\b[A-Z0-9]{8,12}$`` reference
   suffixes — so a merchant whose description drifts across months
   still hashes to the same txn_id. The canonical form is stored on
   the row so future changes to the canoniser re-key via
   ``hash_schema_version``.
4. Emits :class:`RawTransaction` batches of 50 for the orchestrator
   to commit per-batch.

Safety rails (plan § Execution Script Standards):

- Size cap 10 MB, row cap 10,000. Beyond either, abort.
- Every string field control-char-scrubbed before use.
- The CSV path must resolve inside the configured drop folder — no
  symlink escapes or ``..`` traversal.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from execution.shared.errors import (
    DataQualityError,
    PathViolationError,
    SchemaViolationError,
)
from execution.shared.money import to_money

SOURCE_ID: Final[str] = "amex_csv"
ACCOUNT: Final[str] = "amex"

MAX_FILE_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MB
MAX_ROWS: Final[int] = 10_000
DEFAULT_BATCH_SIZE: Final[int] = 50

# Canonical Amex UK CSV schema. Exact match required — any drift should
# surface as a SchemaViolationError so the user (or future us) reviews it
# rather than accepting a silently-misparsed statement.
CANONICAL_COLUMNS: Final[tuple[str, ...]] = (
    "Date",
    "Description",
    "Amount",
    "Extended Details",
    "Appears On Your Statement As",
    "Address",
    "Town/City",
    "Postcode",
    "Country",
    "Reference",
    "Category",
)

# Description canonicalisation — module-level regex per CLAUDE.md
# "Performance → Regex at module level".
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
# Amex frequently appends a UK locator like ``LONDON GB 02034`` or a
# trailing reference ``ABCD1234EF``. Strip both — they drift across
# months without meaningful change to the underlying merchant.
_TRAILING_COUNTRY: Final[re.Pattern[str]] = re.compile(
    r"\s+GB\s*\d*$", re.IGNORECASE
)
_TRAILING_REF: Final[re.Pattern[str]] = re.compile(
    r"\s+[A-Z0-9]{8,12}$"
)
# A short whitelist of UK cities often included verbatim in the description.
# Drop them so ``STARBUCKS LONDON`` and ``STARBUCKS MANCHESTER`` for the same
# merchant on different days don't collide through arbitrary variation.
_UK_CITY_SUFFIX: Final[re.Pattern[str]] = re.compile(
    r"\s+(LONDON|MANCHESTER|BRISTOL|EDINBURGH|GLASGOW|BIRMINGHAM|LEEDS|"
    r"LIVERPOOL|CARDIFF|BELFAST|OXFORD|CAMBRIDGE|BRIGHTON|READING|YORK)"
    r"(\s.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RawTransaction:
    """A validated row ready to hand off to the reconciliation ledger."""

    txn_id: str
    account: str
    booking_date: date
    description_raw: str
    description_canonical: str
    currency: str
    amount: Decimal
    reference: str | None
    category_hint: str | None
    source: str = SOURCE_ID

    def as_row(self) -> dict[str, Any]:
        """Columns used by :mod:`execution.reconcile.ledger`."""
        return {
            "txn_id": self.txn_id,
            "account": self.account,
            "booking_date": self.booking_date.isoformat(),
            "description_raw": self.description_raw,
            "description_canonical": self.description_canonical,
            "currency": self.currency,
            "amount": format(self.amount, "f"),
            "provider_auth_id": self.reference,
            "category": self.category_hint,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class AmexCsvBatchStats:
    """Summary emitted by the orchestrator for Run Status."""

    file: str
    rows_read: int
    rows_emitted: int
    skipped_header: bool


def fetch_from_file(
    csv_path: Path,
    *,
    drop_root: Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[list[RawTransaction]]:
    """Yield batches of :class:`RawTransaction` from one CSV file.

    ``drop_root`` is the allowed parent directory; the CSV path must
    resolve under it. Raises :class:`SchemaViolationError` on bad
    schema and :class:`DataQualityError` on bad rows.
    """
    from execution.shared.names import resolve_under

    resolve_under(csv_path, root=drop_root)

    size = csv_path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SchemaViolationError(
            f"Amex CSV too large: {size} > {MAX_FILE_BYTES}",
            source=SOURCE_ID,
            details={"path": str(csv_path), "size_bytes": size},
        )

    buffered: list[RawTransaction] = []
    row_ordinal_by_date: dict[date, int] = {}

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _assert_canonical_schema(reader.fieldnames or [])

        for idx, raw_row in enumerate(reader):
            if idx >= MAX_ROWS:
                raise SchemaViolationError(
                    f"Amex CSV exceeds row cap {MAX_ROWS}",
                    source=SOURCE_ID,
                    details={"path": str(csv_path)},
                )
            parsed = _parse_row(
                raw_row=raw_row,
                row_ordinal_by_date=row_ordinal_by_date,
            )
            if parsed is None:
                continue
            buffered.append(parsed)
            if len(buffered) >= batch_size:
                yield buffered
                buffered = []

    if buffered:
        yield buffered


def _assert_canonical_schema(fieldnames: list[str]) -> None:
    """Reject CSVs whose header doesn't match the canonical schema."""
    got = tuple(f.strip() for f in fieldnames if f)
    if got != CANONICAL_COLUMNS:
        raise SchemaViolationError(
            "Amex CSV header does not match canonical schema",
            source=SOURCE_ID,
            details={
                "expected": list(CANONICAL_COLUMNS),
                "observed": list(got),
            },
        )


def _parse_row(
    *,
    raw_row: dict[str, str],
    row_ordinal_by_date: dict[date, int],
) -> RawTransaction | None:
    """Turn one CSV row into a :class:`RawTransaction`."""
    date_str = (raw_row.get("Date") or "").strip()
    if not date_str:
        return None
    booking_date = _parse_date(date_str)
    description_raw = _clean_text(raw_row.get("Description") or "")
    if not description_raw:
        return None
    amount_str = (raw_row.get("Amount") or "").strip()
    if not amount_str:
        return None
    amount = _parse_amount(amount_str)
    reference = _clean_text(raw_row.get("Reference") or "") or None
    category_hint = _clean_text(raw_row.get("Category") or "") or None

    canonical = canonicalise_description(description_raw)

    ordinal = row_ordinal_by_date.get(booking_date, 0)
    row_ordinal_by_date[booking_date] = ordinal + 1

    txn_id = compute_txn_id(
        reference=reference,
        account=ACCOUNT,
        booking_date=booking_date,
        canonical_description=canonical,
        amount=amount,
        row_ordinal=ordinal,
    )

    return RawTransaction(
        txn_id=txn_id,
        account=ACCOUNT,
        booking_date=booking_date,
        description_raw=description_raw,
        description_canonical=canonical,
        currency="GBP",  # Amex UK CSV exports GBP statements only
        amount=to_money(amount, "GBP"),
        reference=reference,
        category_hint=category_hint,
    )


def canonicalise_description(raw: str) -> str:
    """Strip city/country/reference noise from a merchant description.

    Frozen on the row as ``description_canonical`` so changes to the
    canoniser are detectable via ``hash_schema_version`` rather than
    silently re-keying historical rows.
    """
    text = _CONTROL_CHARS.sub("", raw)
    text = text.upper().strip()
    text = _WHITESPACE.sub(" ", text)
    text = _TRAILING_COUNTRY.sub("", text)
    text = _TRAILING_REF.sub("", text)
    text = _UK_CITY_SUFFIX.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def compute_txn_id(
    *,
    reference: str | None,
    account: str,
    booking_date: date,
    canonical_description: str,
    amount: Decimal,
    row_ordinal: int,
) -> str:
    """Stable transaction id. Provider-native ``reference`` wins."""
    import hashlib

    if reference:
        payload = f"{account}\x00{reference}".encode()
        return hashlib.sha256(payload).hexdigest()[:16]
    payload = (
        f"{account}\x00"
        f"{booking_date.isoformat()}\x00"
        f"{canonical_description}\x00"
        f"{format(amount, 'f')}\x00"
        f"{row_ordinal}"
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _parse_date(value: str) -> date:
    """Amex UK exports DD/MM/YYYY."""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise DataQualityError(
        f"Amex CSV row has unparseable date {value!r}",
        source=SOURCE_ID,
    )


def _parse_amount(value: str) -> Decimal:
    """Amex UK CSV: ``1,234.56`` for a debit, negative for a credit."""
    cleaned = value.replace(",", "").replace("£", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as err:
        raise DataQualityError(
            f"Amex CSV row has unparseable amount {value!r}",
            source=SOURCE_ID,
            cause=err,
        ) from err


def _clean_text(text: str) -> str:
    """Strip control chars, collapse whitespace, and trim."""
    out = _CONTROL_CHARS.sub("", text or "")
    return _WHITESPACE.sub(" ", out).strip()


def discover_csv_files(drop_root: Path) -> list[Path]:
    """Return all ``*.csv`` files inside ``drop_root`` (non-recursive), sorted."""
    if not drop_root.exists():
        raise PathViolationError(
            f"Amex drop folder does not exist: {drop_root}",
            source=SOURCE_ID,
        )
    return sorted(p for p in drop_root.glob("*.csv") if p.is_file())


__all__ = [
    "ACCOUNT",
    "CANONICAL_COLUMNS",
    "DEFAULT_BATCH_SIZE",
    "MAX_FILE_BYTES",
    "MAX_ROWS",
    "SOURCE_ID",
    "AmexCsvBatchStats",
    "RawTransaction",
    "canonicalise_description",
    "compute_txn_id",
    "discover_csv_files",
    "fetch_from_file",
]
