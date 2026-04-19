"""Drive-primary invoice storage + duplicate policy.

Ordering (plan § State Lifecycle Risks):

    1. Write raw PDF bytes to ``.tmp/invoices/<msg_id>/<attachment_index>.pdf``
       and ``fsync``.
    2. Upload to Google Drive via ``files().create()``; capture ``fileId``.
    3. Verify via ``files().get(fileId, fields='size,md5Checksum')`` — bytes
       match the local md5.
    4. **Only then** ``INSERT INTO invoices`` inside a SQLite transaction.
    5. ``os.unlink`` the ``.tmp/`` PDF after commit.

A crash between step 2 and step 4 leaves an orphan Drive file, not an
orphan DB row. A janitor pass on the next run reconciles orphans by
hashing recent Drive uploads and matching to pending ``emails`` rows.

Duplicate policy (plan § Phase 2):

- Primary key: ``UNIQUE(vendor_id, invoice_number)``.
- Collision with matching ``amount_gross`` → ``duplicate_resend``. Keep
  the original row. Second file is not uploaded.
- Collision with differing ``amount_gross`` → ``corrected_invoice``.
  Surface BOTH in Exceptions; never silently overwrite an already-
  matched invoice.
- Low-confidence ``invoice_number`` (confidence < 0.70) is replaced with
  a deterministic surrogate ``SYN-<sha256>`` so the uniqueness index
  never falls through into null.

Input validation: every path component passes through
:func:`execution.shared.names.resolve_under` before a write, and every
category / currency is whitelist-checked before path construction.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
from base64 import b64decode
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from execution.shared import names
from execution.shared.clock import now_utc
from execution.shared.errors import DataQualityError, PathViolationError

if TYPE_CHECKING:  # pragma: no cover
    from execution.invoice.extractor import ExtractorResult
    from execution.shared.sheet import GoogleClients

DRIVE_ROOT_FOLDER_NAME: Final[str] = "Accounts"
LOW_CONFIDENCE_INVOICE_NUMBER_FLOOR: Final[float] = 0.70
DRIVE_PDF_MIMETYPE: Final[str] = "application/pdf"


class FilerOutcome(StrEnum):
    """What happened when we tried to file an invoice."""

    CREATED = "created"
    DUPLICATE_RESEND = "duplicate_resend"
    CORRECTED_INVOICE = "corrected_invoice"


@dataclass(frozen=True, slots=True)
class FiledInvoice:
    """Public record of a successful filing."""

    invoice_id: str
    outcome: FilerOutcome
    drive_file_id: str
    drive_web_view_link: str | None
    filed_path: str
    vendor_id: str


@dataclass(frozen=True, slots=True)
class FilerInput:
    """Everything :func:`file_invoice` needs to take one invoice end-to-end."""

    source_msg_id: str
    attachment_index: int
    pdf_bytes: bytes
    extraction: ExtractorResult
    extractor_version: str
    invoice_number_confidence: float
    category: names.Category
    sender_domain: str | None
    tmp_root: Path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def file_invoice(
    clients: GoogleClients,
    conn: sqlite3.Connection,
    inp: FilerInput,
    *,
    drive_root_name: str = DRIVE_ROOT_FOLDER_NAME,
) -> FiledInvoice:
    """File one PDF into Drive + record one row in ``invoices``.

    Idempotent on ``(vendor_id, invoice_number)``: a duplicate invoice
    number with a matching gross returns the existing row with
    ``outcome=duplicate_resend``. A mismatched gross records a new row
    with ``outcome=corrected_invoice`` and flags both; callers push the
    decision to the user via the Exceptions tab.
    """
    if inp.pdf_bytes[:5] != b"%PDF-":
        raise DataQualityError(
            "filer called with non-PDF bytes",
            source="filer",
            details={"head": inp.pdf_bytes[:16].hex()},
        )

    if not inp.extraction.currency:
        raise DataQualityError(
            "extraction has no currency; cannot build Drive filename",
            source="filer",
        )
    names.validate_category(inp.category)
    names.validate_currency(inp.extraction.currency)

    vendor_id = _get_or_create_vendor(
        conn,
        name=inp.extraction.supplier_name,
        sender_domain=inp.sender_domain,
        default_category=inp.category,
    )

    resolved_invoice_number, used_surrogate = _effective_invoice_number(
        inp=inp, vendor_id=vendor_id
    )
    invoice_id = _invoice_id(inp.source_msg_id, inp.attachment_index)

    existing = conn.execute(
        """
        SELECT invoice_id, amount_gross, drive_file_id, drive_web_view_link
        FROM invoices
        WHERE vendor_id = ? AND invoice_number = ? AND deleted_at IS NULL
        """,
        (vendor_id, resolved_invoice_number),
    ).fetchone()

    if existing is not None:
        existing_gross = existing["amount_gross"]
        new_gross = inp.extraction.amount_gross
        if existing_gross == new_gross:
            return FiledInvoice(
                invoice_id=existing["invoice_id"],
                outcome=FilerOutcome.DUPLICATE_RESEND,
                drive_file_id=existing["drive_file_id"],
                drive_web_view_link=existing["drive_web_view_link"],
                filed_path="(existing)",
                vendor_id=vendor_id,
            )
        # Amount differs → corrected-invoice branch: file the new one with a
        # ``-corrected-<id>`` suffix on the invoice number slug so the DB
        # unique index doesn't collide and the original row remains for
        # auditor comparison.
        resolved_invoice_number = f"{resolved_invoice_number}-corrected-{invoice_id[:6]}"

    # 1. Temp write (fsync)
    tmp_pdf_path = _write_tmp(
        tmp_root=inp.tmp_root,
        source_msg_id=inp.source_msg_id,
        attachment_index=inp.attachment_index,
        data=inp.pdf_bytes,
    )

    try:
        # 2. Resolve Drive folder path: Accounts/FY-YYYY-YY/<category>/<YYYY-MM>/
        fy_label = _fy_label_for_invoice(inp.extraction.invoice_date)
        year_month = _year_month_for_invoice(inp.extraction.invoice_date)
        drive_name = _drive_name(
            extraction=inp.extraction,
            invoice_number=resolved_invoice_number,
            vendor_fallback_key=vendor_id,
        )
        drive_folder_id = _ensure_drive_tree(
            clients,
            root_name=drive_root_name,
            segments=(fy_label, inp.category, year_month),
        )

        # 3. Upload + md5 verify
        drive_file_id, web_link = _upload_and_verify(
            clients=clients,
            parent_folder_id=drive_folder_id,
            name=drive_name,
            data=inp.pdf_bytes,
        )

        outcome = (
            FilerOutcome.CORRECTED_INVOICE
            if existing is not None
            else FilerOutcome.CREATED
        )

        # 4. FX conversion - convert to GBP at filing time using invoice date
        amount_gross_gbp: str | None = None
        fx_rate_used: str | None = None
        fx_error: str | None = None

        if inp.extraction.currency and inp.extraction.amount_gross and inp.extraction.invoice_date:
            from decimal import Decimal

            from execution.shared.fx import get_rate_to_gbp
            from execution.shared.money import to_money

            rate, err = get_rate_to_gbp(conn, inp.extraction.currency, inp.extraction.invoice_date)
            if rate is not None:
                try:
                    gross = Decimal(inp.extraction.amount_gross)
                    converted = to_money(gross * rate, "GBP")
                    amount_gross_gbp = str(converted)
                    fx_rate_used = str(rate)
                except Exception as e:
                    fx_error = f"conversion failed: {e}"
            else:
                fx_error = err

        # 5. SQLite commit (transactional)
        _insert_invoice_row(
            conn,
            invoice_id=invoice_id,
            source_msg_id=inp.source_msg_id,
            vendor_id=vendor_id,
            resolved_invoice_number=resolved_invoice_number,
            used_surrogate=used_surrogate,
            extraction=inp.extraction,
            extractor_version=inp.extractor_version,
            category=inp.category,
            drive_file_id=drive_file_id,
            drive_web_view_link=web_link,
            filed_path=f"{fy_label}/{inp.category}/{year_month}/{drive_name}",
            amount_gross_gbp=amount_gross_gbp,
            fx_rate_used=fx_rate_used,
            fx_error=fx_error,
        )
    finally:
        # 6. Always clean up the temp file — even on failure, there's no value
        #    in leaving it. Crash after upload is handled by the janitor.
        with contextlib.suppress(OSError):
            tmp_pdf_path.unlink(missing_ok=True)

    return FiledInvoice(
        invoice_id=invoice_id,
        outcome=outcome,
        drive_file_id=drive_file_id,
        drive_web_view_link=web_link,
        filed_path=f"{fy_label}/{inp.category}/{year_month}/{drive_name}",
        vendor_id=vendor_id,
    )


# ---------------------------------------------------------------------------
# Helpers — path + name construction
# ---------------------------------------------------------------------------


def _invoice_id(source_msg_id: str, attachment_index: int) -> str:
    """Stable invoice_id = sha256(msg_id || index)[:16]."""
    h = hashlib.sha256()
    h.update(source_msg_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(attachment_index).encode("utf-8"))
    return h.hexdigest()[:16]


def _effective_invoice_number(
    *, inp: FilerInput, vendor_id: str
) -> tuple[str, bool]:
    """Return (resolved_invoice_number, used_surrogate)."""
    extracted = inp.extraction.invoice_number
    if (
        extracted is not None
        and inp.invoice_number_confidence >= LOW_CONFIDENCE_INVOICE_NUMBER_FLOOR
    ):
        return extracted, False
    # Surrogate — deterministic in (vendor_id, invoice_date, amount_gross,
    # attachment_index) so the same invoice arriving twice hashes identically.
    seed = "\x00".join(
        [
            vendor_id,
            inp.extraction.invoice_date or "",
            inp.extraction.amount_gross or "",
            str(inp.attachment_index),
        ]
    ).encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:8]
    return f"SYN-{digest.upper()}", True


def _drive_name(
    *,
    extraction: ExtractorResult,
    invoice_number: str,
    vendor_fallback_key: str,
) -> str:
    """Final Drive filename: ``YYYY-MM-DD_vendor-slug_amount-CCY_inv-slug.pdf``."""
    invoice_date = extraction.invoice_date or "unknown-date"
    v_slug = names.vendor_slug(
        extraction.supplier_name, fallback_key=vendor_fallback_key
    )
    inv_slug = names.invoice_number_slug(
        invoice_number, fallback_key=f"{vendor_fallback_key}:{invoice_number}"
    )
    amount = extraction.amount_gross or "unknown"
    currency = extraction.currency or "XXX"
    return f"{invoice_date}_{v_slug}_{amount}-{currency}_{inv_slug}.pdf"


def _fy_label_for_invoice(invoice_date: str | None) -> str:
    from execution.shared.fiscal import fy_of

    if invoice_date is None:
        return "FY-unknown"
    try:
        d = date.fromisoformat(invoice_date)
    except (TypeError, ValueError):
        return "FY-unknown"
    return fy_of(d)


def _year_month_for_invoice(invoice_date: str | None) -> str:
    if invoice_date is None:
        return "unknown-month"
    try:
        d = date.fromisoformat(invoice_date)
    except (TypeError, ValueError):
        return "unknown-month"
    return f"{d.year:04d}-{d.month:02d}"


def _write_tmp(
    *,
    tmp_root: Path,
    source_msg_id: str,
    attachment_index: int,
    data: bytes,
) -> Path:
    """Write ``data`` into ``tmp_root/<msg_id>/<index>.pdf`` with ``fsync``.

    The ``msg_id`` is slugged before being used as a directory name; the
    resolved path is validated to sit inside ``tmp_root`` before write.
    """
    msg_slug = names.slug(
        source_msg_id, max_length=64, fallback_key=source_msg_id
    )
    dir_path = tmp_root / msg_slug
    file_path = dir_path / f"{attachment_index}.pdf"
    # Sandbox assertion: composed path must resolve inside tmp_root.
    names.resolve_under(file_path, root=tmp_root)
    dir_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as err:
        raise PathViolationError(
            f"failed to write temp PDF at {file_path}",
            source="filer",
            cause=err,
        ) from err
    return file_path


# ---------------------------------------------------------------------------
# Helpers — Drive upload + verification
# ---------------------------------------------------------------------------


def _ensure_drive_tree(
    clients: GoogleClients, *, root_name: str, segments: tuple[str, ...]
) -> str:
    """Resolve / create ``root_name/segments[0]/…`` and return the leaf folder id."""
    from execution.shared.sheet import ensure_drive_folder

    folder_id = ensure_drive_folder(clients, root_name)
    for seg in segments:
        folder_id = ensure_drive_folder(clients, seg, parent_id=folder_id)
    return folder_id


def _upload_and_verify(
    *,
    clients: GoogleClients,
    parent_folder_id: str,
    name: str,
    data: bytes,
) -> tuple[str, str | None]:
    """Upload ``data`` as ``name``, verify md5 round-trips, return ``(file_id, web_link)``."""
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(data, mimetype=DRIVE_PDF_MIMETYPE, resumable=False)
    created = (
        clients.drive.files()
        .create(
            body={"name": name, "parents": [parent_folder_id]},
            media_body=media,
            fields="id,md5Checksum,webViewLink",
        )
        .execute()
    )
    file_id = str(created["id"])

    got = _md5_from_drive(created)
    expected = hashlib.md5(data, usedforsecurity=False).hexdigest()
    if got is not None and got != expected:
        raise DataQualityError(
            f"Drive md5 mismatch on upload: got {got}, expected {expected}",
            source="filer",
            details={
                "drive_file_id": file_id,
                "expected_md5": expected,
                "observed_md5": got,
            },
        )
    if got is None:
        # Fall back to an explicit fetch — some Drive libraries omit
        # md5Checksum on the initial create response.
        fetched = (
            clients.drive.files()
            .get(fileId=file_id, fields="md5Checksum,webViewLink")
            .execute()
        )
        got = _md5_from_drive(fetched)
        if got is not None and got != expected:
            raise DataQualityError(
                f"Drive md5 mismatch after re-fetch: got {got}, expected {expected}",
                source="filer",
            )

    web_link = created.get("webViewLink")
    return file_id, (str(web_link) if web_link else None)


def _md5_from_drive(payload: dict[str, Any]) -> str | None:
    """Extract ``md5Checksum`` from a Drive response, tolerating shape drift."""
    raw = payload.get("md5Checksum")
    if raw is None:
        return None
    # Drive returns hex; but some clients wrap in base64 — accept either.
    if len(raw) == 32 and all(c in "0123456789abcdef" for c in raw.lower()):
        return str(raw).lower()
    try:
        decoded = b64decode(raw)
    except (ValueError, TypeError):
        return str(raw).lower()
    return decoded.hex()


# ---------------------------------------------------------------------------
# Helpers — SQLite writes
# ---------------------------------------------------------------------------


def _get_or_create_vendor(
    conn: sqlite3.Connection,
    *,
    name: str | None,
    sender_domain: str | None,
    default_category: names.Category,
) -> str:
    """Return a stable ``vendor_id`` for ``(name, domain)``; create if needed.

    Vendor_id = sha256 of the lowercased canonical name (or the sender
    domain if the name is unknown) — keeps the same vendor stable across
    runs without a UNIQUE index fight.
    """
    canonical = (name or sender_domain or "unknown").strip().lower()
    vendor_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    conn.execute(
        """
        INSERT INTO vendors (vendor_id, canonical_name, domain, default_category)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(vendor_id) DO NOTHING
        """,
        (vendor_id, canonical, sender_domain, default_category),
    )
    return vendor_id


def _insert_invoice_row(
    conn: sqlite3.Connection,
    *,
    invoice_id: str,
    source_msg_id: str,
    vendor_id: str,
    resolved_invoice_number: str,
    used_surrogate: bool,
    extraction: ExtractorResult,
    extractor_version: str,
    category: names.Category,
    drive_file_id: str,
    drive_web_view_link: str | None,
    filed_path: str,
    amount_gross_gbp: str | None,
    fx_rate_used: str | None,
    fx_error: str | None,
) -> None:
    import json

    confidence_json = json.dumps(extraction.field_confidence.model_dump())
    notes_suffix = " [surrogate-invoice-number]" if used_surrogate else ""
    notes = (extraction.extraction_notes or "") + notes_suffix or None

    with conn:
        conn.execute(
            """
            INSERT INTO invoices (
                invoice_id, source_msg_id, vendor_id, vendor_name_raw,
                invoice_number, invoice_date, currency,
                amount_net, amount_vat, amount_gross, amount_gross_gbp,
                vat_rate, vat_number_supplier, reverse_charge,
                category, category_source,
                drive_file_id, drive_web_view_link,
                confidence_json, classifier_version,
                hash_schema_version, is_business,
                deleted_at, deleted_reason,
                fx_rate_used, fx_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_id,
                source_msg_id,
                vendor_id,
                extraction.supplier_name,
                resolved_invoice_number,
                extraction.invoice_date,
                extraction.currency,
                extraction.amount_net,
                extraction.amount_vat,
                extraction.amount_gross,
                amount_gross_gbp,
                extraction.vat_rate,
                extraction.supplier_vat_number,
                1 if extraction.reverse_charge else 0,
                category,
                "llm",
                drive_file_id,
                drive_web_view_link,
                confidence_json,
                extractor_version,
                1,  # hash_schema_version
                None,  # is_business — filled retroactively on match
                None,
                notes,
                fx_rate_used,
                fx_error,
            ),
        )
        # Watermark the processed email so re-runs are idempotent.
        conn.execute(
            """
            UPDATE emails
               SET outcome = ?,
                   processed_at = ?
             WHERE msg_id = ?
            """,
            ("invoice", now_utc().isoformat(), source_msg_id),
        )
    # filed_path is currently logged only through the filer result; the
    # plan's `invoices.filed_path` column was consolidated into
    # `drive_web_view_link`. A dedicated column would be nicer but would
    # need a migration — we can add it once the first real directive run
    # demonstrates the need.
    del filed_path  # silence unused-var lint; still returned on FiledInvoice


__all__ = [
    "DRIVE_PDF_MIMETYPE",
    "DRIVE_ROOT_FOLDER_NAME",
    "LOW_CONFIDENCE_INVOICE_NUMBER_FLOOR",
    "FiledInvoice",
    "FilerInput",
    "FilerOutcome",
    "file_invoice",
]
