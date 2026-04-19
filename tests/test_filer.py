"""Tests for execution.invoice.filer."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from execution.invoice.extractor import ExtractorResult
from execution.invoice.filer import (
    FilerInput,
    FilerOutcome,
    file_invoice,
)
from execution.shared import db as db_mod
from execution.shared.errors import DataQualityError

# ---------------------------------------------------------------------------
# Minimal ExtractorResult factory
# ---------------------------------------------------------------------------


def _extraction(**overrides: Any) -> ExtractorResult:
    payload: dict[str, Any] = {
        "supplier_name": "Atlassian Pty Ltd",
        "supplier_address": "341 George St, Sydney NSW 2000",
        "supplier_vat_number": "GB123456789",
        "customer_name": "Granite Marketing Ltd",
        "customer_address": None,
        "invoice_number": "INV-2026-0412",
        "invoice_date": "2026-04-01",
        "supply_date": None,
        "description": "Jira Premium + Confluence Premium",
        "currency": "GBP",
        "amount_net": "400.00",
        "amount_vat": "80.00",
        "amount_gross": "480.00",
        "vat_rate": "0.20",
        "reverse_charge": False,
        "arithmetic_ok": True,
        "line_items": [],
        "field_confidence": {
            "supplier_name": 0.98,
            "supplier_address": 0.95,
            "supplier_vat_number": 0.99,
            "customer_name": 0.97,
            "customer_address": 0.0,
            "invoice_number": 0.99,
            "invoice_date": 0.99,
            "supply_date": 0.0,
            "description": 0.90,
            "currency": 0.99,
            "amount_net": 0.99,
            "amount_vat": 0.99,
            "amount_gross": 0.99,
            "vat_rate": 0.97,
        },
        "overall_confidence": 0.99,
        "extraction_notes": None,
    }
    payload.update(overrides)
    return ExtractorResult.model_validate(payload)


# ---------------------------------------------------------------------------
# Fake Drive client
# ---------------------------------------------------------------------------


@dataclass
class _FakeRequest:
    payload: dict[str, Any]

    def execute(self) -> dict[str, Any]:
        return self.payload


@dataclass
class _FakeFiles:
    parent: _FakeDrive  # type: ignore[name-defined]

    def list(self, *, q: str, fields: str, pageSize: int) -> _FakeRequest:
        del fields, pageSize
        existing = self.parent.folders.get(q)
        if existing:
            return _FakeRequest({"files": [{"id": existing}]})
        return _FakeRequest({"files": []})

    def create(
        self,
        *,
        body: dict[str, Any],
        fields: str,
        media_body: Any | None = None,
    ) -> _FakeRequest:
        del fields
        if media_body is None:
            # Folder create
            folder_id = f"folder-{body['name']}-{len(self.parent.folders)}"
            # Also index by the canonical list query used in ensure_drive_folder
            name = body["name"]
            parents = body.get("parents", [])
            q_no_parent = (
                f"mimeType='application/vnd.google-apps.folder' "
                f"and name='{name}' and trashed=false"
            )
            q = (
                q_no_parent + f" and '{parents[0]}' in parents"
                if parents
                else q_no_parent
            )
            self.parent.folders[q] = folder_id
            return _FakeRequest({"id": folder_id})
        # File create (PDF upload)
        data = media_body.getbytes(0, media_body.size())
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        file_id = f"pdf-{len(self.parent.uploads)}"
        self.parent.uploads.append(
            {"id": file_id, "name": body["name"], "data": data, "md5": md5}
        )
        return _FakeRequest(
            {
                "id": file_id,
                "md5Checksum": md5,
                "webViewLink": f"https://drive.google.com/file/d/{file_id}",
            }
        )

    def get(self, *, fileId: str, fields: str) -> _FakeRequest:
        del fields
        for up in self.parent.uploads:
            if up["id"] == fileId:
                return _FakeRequest(
                    {
                        "md5Checksum": up["md5"],
                        "webViewLink": f"https://drive.google.com/file/d/{fileId}",
                    }
                )
        return _FakeRequest({})


@dataclass
class _FakeDrive:
    folders: dict[str, str] = field(default_factory=dict)
    uploads: list[dict[str, Any]] = field(default_factory=list)

    def files(self) -> _FakeFiles:
        return _FakeFiles(self)


class _FakeGoogle:
    def __init__(self) -> None:
        self._drive = _FakeDrive()

    @property
    def drive(self) -> _FakeDrive:
        return self._drive

    @property
    def sheets(self) -> Any:  # pragma: no cover — filer never calls sheets
        raise AssertionError("filer should not touch sheets")


@pytest.fixture
def tmp_db() -> sqlite3.Connection:
    conn = db_mod.connect(":memory:")
    db_mod.apply_migrations(conn)
    # Seed one email row so the filer's UPDATE watermark has something to hit.
    conn.execute(
        """
        INSERT INTO emails (
            msg_id, source_adapter, received_at, from_addr, subject, outcome
        ) VALUES (?, 'ms365', ?, 'billing@atlassian.com', 'Your invoice', 'pending')
        """,
        ("msg-1", "2026-04-02T10:00:00+00:00"),
    )
    return conn


def _input(tmp_path: Path, **overrides: Any) -> FilerInput:
    defaults: dict[str, Any] = {
        "source_msg_id": "msg-1",
        "attachment_index": 0,
        "pdf_bytes": b"%PDF-1.7\nhello",
        "extraction": _extraction(),
        "extractor_version": "abcd1234",
        "invoice_number_confidence": 0.99,
        "category": "software_saas",
        "sender_domain": "atlassian.com",
        "tmp_root": tmp_path,
    }
    defaults.update(overrides)
    return FilerInput(**defaults)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_file_invoice_creates_row_and_uploads(tmp_db, tmp_path):
    fake = _FakeGoogle()
    filed = file_invoice(fake, tmp_db, _input(tmp_path))
    assert filed.outcome is FilerOutcome.CREATED
    assert filed.drive_file_id == "pdf-0"
    assert filed.drive_web_view_link.startswith("https://drive.google.com/")
    assert filed.filed_path.startswith("FY-2026-27/software_saas/2026-04/")

    row = tmp_db.execute(
        "SELECT * FROM invoices WHERE invoice_id = ?", (filed.invoice_id,)
    ).fetchone()
    assert row is not None
    assert row["vendor_id"] == filed.vendor_id
    assert row["invoice_number"] == "INV-2026-0412"
    assert row["drive_file_id"] == "pdf-0"
    assert row["currency"] == "GBP"
    assert row["amount_gross"] == "480.00"


def test_file_invoice_creates_vendor_row(tmp_db, tmp_path):
    fake = _FakeGoogle()
    file_invoice(fake, tmp_db, _input(tmp_path))
    vendors = tmp_db.execute("SELECT * FROM vendors").fetchall()
    assert len(vendors) == 1
    assert vendors[0]["canonical_name"] == "atlassian pty ltd"
    assert vendors[0]["domain"] == "atlassian.com"
    assert vendors[0]["default_category"] == "software_saas"


def test_file_invoice_cleans_up_tmp_after_commit(tmp_db, tmp_path):
    fake = _FakeGoogle()
    file_invoice(fake, tmp_db, _input(tmp_path))
    # No leftover PDFs in the tmp tree
    pdfs = list(tmp_path.rglob("*.pdf"))
    assert pdfs == []


def test_file_invoice_watermarks_email(tmp_db, tmp_path):
    fake = _FakeGoogle()
    file_invoice(fake, tmp_db, _input(tmp_path))
    row = tmp_db.execute(
        "SELECT outcome, processed_at FROM emails WHERE msg_id = 'msg-1'"
    ).fetchone()
    assert row["outcome"] == "invoice"
    assert row["processed_at"] is not None


# ---------------------------------------------------------------------------
# Duplicate policy
# ---------------------------------------------------------------------------


def test_file_invoice_same_gross_returns_duplicate_resend(tmp_db, tmp_path):
    fake = _FakeGoogle()
    first = file_invoice(fake, tmp_db, _input(tmp_path))
    # Same invoice (same msg_id + index) arriving a second time; equal gross.
    second_inp = _input(tmp_path, pdf_bytes=b"%PDF-1.7\nhello-again")
    second = file_invoice(fake, tmp_db, second_inp)
    assert second.outcome is FilerOutcome.DUPLICATE_RESEND
    assert second.invoice_id == first.invoice_id
    assert second.drive_file_id == first.drive_file_id
    # No second PDF upload
    assert len(fake.drive.uploads) == 1


def test_file_invoice_mismatched_gross_routes_to_corrected(tmp_db, tmp_path):
    fake = _FakeGoogle()
    first = file_invoice(fake, tmp_db, _input(tmp_path))
    bumped = _extraction(amount_gross="500.00")
    second = file_invoice(
        fake,
        tmp_db,
        _input(tmp_path, extraction=bumped, attachment_index=1),
    )
    assert second.outcome is FilerOutcome.CORRECTED_INVOICE
    assert second.invoice_id != first.invoice_id
    rows = tmp_db.execute(
        "SELECT invoice_number FROM invoices ORDER BY invoice_number"
    ).fetchall()
    numbers = [r["invoice_number"] for r in rows]
    assert "INV-2026-0412" in numbers
    assert any(n.startswith("INV-2026-0412-corrected-") for n in numbers)


# ---------------------------------------------------------------------------
# Surrogate invoice number for low-confidence extractions
# ---------------------------------------------------------------------------


def test_low_confidence_invoice_number_is_synthesized(tmp_db, tmp_path):
    fake = _FakeGoogle()
    filed = file_invoice(
        fake,
        tmp_db,
        _input(tmp_path, invoice_number_confidence=0.40),
    )
    row = tmp_db.execute(
        "SELECT invoice_number FROM invoices WHERE invoice_id = ?",
        (filed.invoice_id,),
    ).fetchone()
    assert row["invoice_number"].startswith("SYN-")


def test_null_invoice_number_is_synthesized(tmp_db, tmp_path):
    fake = _FakeGoogle()
    extraction = _extraction(invoice_number=None)
    extraction_conf = extraction.model_copy()
    filed = file_invoice(
        fake,
        tmp_db,
        _input(tmp_path, extraction=extraction_conf, invoice_number_confidence=0.0),
    )
    row = tmp_db.execute(
        "SELECT invoice_number FROM invoices WHERE invoice_id = ?",
        (filed.invoice_id,),
    ).fetchone()
    assert row["invoice_number"].startswith("SYN-")


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_non_pdf_bytes_rejected(tmp_db, tmp_path):
    fake = _FakeGoogle()
    with pytest.raises(DataQualityError, match="non-PDF"):
        file_invoice(
            fake,
            tmp_db,
            _input(tmp_path, pdf_bytes=b"<html>"),
        )


def test_missing_currency_rejected(tmp_db, tmp_path):
    fake = _FakeGoogle()
    with pytest.raises(DataQualityError, match="currency"):
        file_invoice(
            fake,
            tmp_db,
            _input(tmp_path, extraction=_extraction(currency=None)),
        )


def test_unknown_currency_rejected(tmp_db, tmp_path):
    fake = _FakeGoogle()
    with pytest.raises(ValueError, match="currency"):
        file_invoice(
            fake,
            tmp_db,
            _input(tmp_path, extraction=_extraction(currency="XXX")),
        )


def test_unknown_category_rejected(tmp_db, tmp_path):
    fake = _FakeGoogle()
    with pytest.raises(ValueError, match="category"):
        file_invoice(fake, tmp_db, _input(tmp_path, category="bogus"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# md5 verification
# ---------------------------------------------------------------------------


def test_md5_mismatch_raises(tmp_db, tmp_path, monkeypatch):
    fake = _FakeGoogle()

    from execution.invoice import filer as filer_mod

    def tamper(payload):
        return "deadbeef" * 4  # wrong md5

    monkeypatch.setattr(filer_mod, "_md5_from_drive", tamper)
    with pytest.raises(DataQualityError, match="md5 mismatch"):
        file_invoice(fake, tmp_db, _input(tmp_path))
