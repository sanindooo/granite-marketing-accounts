"""Tests for bulk PDF upload."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from execution.statement.bulk_upload import (
    BulkUploadStats,
    UploadResult,
    _get_flagged_transactions,
    _match_invoice_to_flagged,
    bulk_upload_pdfs,
    process_bulk_upload,
)


def _make_field_confidence():
    """Create FieldConfidence with all fields set to high confidence."""
    from execution.invoice.extractor import FieldConfidence

    return FieldConfidence(
        supplier_name=0.9,
        supplier_address=0.9,
        supplier_vat_number=0.9,
        customer_name=0.9,
        customer_address=0.9,
        invoice_number=0.9,
        invoice_date=0.9,
        supply_date=0.9,
        description=0.9,
        currency=0.9,
        amount_net=0.9,
        amount_vat=0.9,
        amount_gross=0.9,
        vat_rate=0.9,
    )


class TestBulkUploadPdfs:
    """Test bulk PDF upload streaming interface."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create a test database with necessary tables."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE vendors (
                vendor_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                domain TEXT
            );

            CREATE TABLE invoices (
                invoice_id TEXT PRIMARY KEY,
                vendor_id TEXT NOT NULL,
                source_msg_id TEXT,
                invoice_number TEXT,
                invoice_date TEXT,
                currency TEXT NOT NULL,
                amount_gross TEXT NOT NULL,
                amount_gross_gbp TEXT,
                deleted_at TEXT
            );

            CREATE TABLE transactions (
                txn_id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                txn_type TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                description_raw TEXT NOT NULL,
                description_canonical TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_gbp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'settled',
                deleted_at TEXT,
                needs_manual_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                txn_id TEXT,
                fiscal_year TEXT,
                state TEXT,
                match_score TEXT,
                match_reason TEXT,
                override_history TEXT,
                updated_at TEXT
            );

            CREATE TABLE reconciliation_links (
                row_id TEXT NOT NULL,
                invoice_id TEXT,
                txn_id TEXT,
                allocated_amount_gbp TEXT,
                link_kind TEXT
            );
            CREATE UNIQUE INDEX ux_links_triple
                ON reconciliation_links(row_id, COALESCE(invoice_id, ''), COALESCE(txn_id, ''));

            -- Flagged transaction for manual download
            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp, needs_manual_download
            )
            VALUES (
                'txn_flagged', 'amex', 'purchase', '2025-11-15',
                'FIGMA INC', 'FIGMA INC',
                'USD', '-35.00', '-28.00', 1
            );
        """)
        conn.commit()
        yield conn
        conn.close()

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        return {
            "llm_client": MagicMock(),
            "google": MagicMock(),
            "extractor_prompt": MagicMock(version="v1.0"),
        }

    @pytest.fixture
    def sample_pdf(self, tmp_path):
        """Create a minimal PDF for testing."""
        # Create a real PDF using reportlab if available, otherwise use minimal bytes
        pdf_path = tmp_path / "invoice.pdf"

        # Minimal valid PDF structure
        pdf_content = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >> endobj
4 0 obj << /Length 44 >> stream
BT /F1 12 Tf 100 700 Td (Invoice: FIGMA) Tj ET
endstream endobj
xref
0 5
trailer << /Size 5 /Root 1 0 R >>
startxref
0
%%EOF"""
        pdf_path.write_bytes(pdf_content)
        return pdf_path

    def test_file_not_found_returns_error(self, test_db, mock_deps, tmp_path):
        """Non-existent file should return error result."""
        nonexistent = tmp_path / "nonexistent.pdf"

        results = list(
            bulk_upload_pdfs(
                test_db,
                [nonexistent],
                tmp_root=tmp_path,
                **mock_deps,
            )
        )

        assert len(results) == 1
        _, _, _, result = results[0]
        assert not result.success
        assert "not found" in result.error.lower()

    @patch("execution.statement.bulk_upload._extract_pdf_text")
    @patch("execution.statement.bulk_upload.extract_invoice")
    @patch("execution.statement.bulk_upload.file_invoice")
    def test_successful_upload_and_match(
        self, mock_file, mock_extract, mock_text, test_db, mock_deps, tmp_path
    ):
        """Successfully uploaded PDF should match flagged transaction."""
        from execution.invoice.extractor import ExtractorResult
        from execution.invoice.filer import FiledInvoice, FilerOutcome

        # Create test PDF
        pdf_path = tmp_path / "figma_invoice.pdf"
        pdf_path.write_bytes(b"fake pdf content")

        # Mock extraction
        mock_text.return_value = "Invoice from Figma Inc. Amount: $35.00"
        mock_extract.return_value = MagicMock(
            result=ExtractorResult(
                supplier_name="Figma Inc",
                supplier_address=None,
                supplier_vat_number=None,
                customer_name=None,
                customer_address=None,
                invoice_number="INV-001",
                invoice_date="2025-11-14",
                supply_date=None,
                description="Figma subscription",
                currency="USD",
                amount_net="35.00",
                amount_vat="0.00",
                amount_gross="35.00",
                vat_rate=None,
                reverse_charge=False,
                arithmetic_ok=True,
                line_items=[],
                field_confidence=_make_field_confidence(),
                overall_confidence=0.9,
                extraction_notes=None,
            )
        )
        mock_file.return_value = FiledInvoice(
            invoice_id="inv_figma",
            outcome=FilerOutcome.CREATED,
            drive_file_id="drive123",
            drive_web_view_link="https://drive.google.com/...",
            filed_path="/Accounts/...",
            vendor_id="v_figma",
        )

        results = list(
            bulk_upload_pdfs(
                test_db,
                [pdf_path],
                tmp_root=tmp_path,
                **mock_deps,
            )
        )

        assert len(results) == 1
        _, _, _, result = results[0]
        assert result.success
        assert result.invoice_id == "inv_figma"
        assert result.matched_txn_id == "txn_flagged"

        # Verify transaction flag was cleared
        txn = test_db.execute(
            "SELECT needs_manual_download FROM transactions WHERE txn_id = 'txn_flagged'"
        ).fetchone()
        assert txn["needs_manual_download"] == 0


class TestGetFlaggedTransactions:
    """Test fetching transactions flagged for manual download."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create test database."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE transactions (
                txn_id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                txn_type TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                description_raw TEXT NOT NULL,
                description_canonical TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_gbp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'settled',
                deleted_at TEXT,
                needs_manual_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                txn_id TEXT,
                fiscal_year TEXT,
                state TEXT
            );

            -- Flagged transaction
            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp, needs_manual_download
            )
            VALUES (
                'txn_flagged', 'amex', 'purchase', '2025-11-15',
                'FIGMA', 'FIGMA', 'USD', '-35.00', '-28.00', 1
            );

            -- Normal transaction (not flagged)
            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp, needs_manual_download
            )
            VALUES (
                'txn_normal', 'amex', 'purchase', '2025-11-16',
                'COFFEE', 'COFFEE', 'GBP', '-5.00', '-5.00', 0
            );

            -- Already reconciled (has auto_matched row)
            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp, needs_manual_download
            )
            VALUES (
                'txn_reconciled', 'amex', 'purchase', '2025-11-17',
                'ANTHROPIC', 'ANTHROPIC', 'GBP', '-49.99', '-49.99', 0
            );

            INSERT INTO reconciliation_rows (row_id, invoice_id, txn_id, fiscal_year, state)
            VALUES ('row1', 'inv1', 'txn_reconciled', 'FY-2025-26', 'auto_matched');
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_returns_flagged_transactions(self, test_db):
        """Should return transactions with needs_manual_download=1."""
        flagged = _get_flagged_transactions(test_db)

        txn_ids = [t.txn_id for t in flagged]
        assert "txn_flagged" in txn_ids
        assert "txn_reconciled" not in txn_ids

    def test_returns_unmatched_transactions(self, test_db):
        """Should return transactions without reconciliation rows."""
        flagged = _get_flagged_transactions(test_db)

        txn_ids = [t.txn_id for t in flagged]
        # txn_normal has no recon row, so it should be flagged
        assert "txn_normal" in txn_ids

    def test_fiscal_year_filter(self, test_db):
        """Should filter by fiscal year when provided."""
        # Add transaction outside FY
        test_db.execute("""
            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp, needs_manual_download
            )
            VALUES (
                'txn_old', 'amex', 'purchase', '2024-01-15',
                'OLD', 'OLD', 'GBP', '-10.00', '-10.00', 1
            )
        """)
        test_db.commit()

        flagged = _get_flagged_transactions(test_db, fiscal_year="FY-2025-26")

        txn_ids = [t.txn_id for t in flagged]
        assert "txn_flagged" in txn_ids
        assert "txn_old" not in txn_ids


class TestMatchInvoiceToFlagged:
    """Test matching newly filed invoices to flagged transactions."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create test database."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE transactions (
                txn_id TEXT PRIMARY KEY,
                booking_date TEXT NOT NULL,
                description_canonical TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_gbp TEXT NOT NULL,
                needs_manual_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                txn_id TEXT,
                fiscal_year TEXT,
                state TEXT,
                match_score TEXT,
                match_reason TEXT,
                override_history TEXT,
                updated_at TEXT
            );

            CREATE TABLE reconciliation_links (
                row_id TEXT NOT NULL,
                invoice_id TEXT,
                txn_id TEXT,
                allocated_amount_gbp TEXT,
                link_kind TEXT
            );
            CREATE UNIQUE INDEX ux_links_triple
                ON reconciliation_links(row_id, COALESCE(invoice_id, ''), COALESCE(txn_id, ''));

            INSERT INTO transactions (
                txn_id, booking_date, description_canonical,
                currency, amount, amount_gbp, needs_manual_download
            )
            VALUES (
                'txn1', '2025-11-15', 'FIGMA INC', 'USD', '-35.00', '-28.00', 1
            );
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_matches_similar_transaction(self, test_db):
        """Invoice with matching vendor/amount should link to transaction."""
        from execution.invoice.extractor import ExtractorResult
        from execution.reconcile.match import TransactionCandidate

        extraction = ExtractorResult(
            supplier_name="Figma Inc",
            supplier_address=None,
            supplier_vat_number=None,
            customer_name=None,
            customer_address=None,
            invoice_number="INV-001",
            invoice_date="2025-11-14",
            supply_date=None,
            description="Figma subscription",
            currency="USD",
            amount_net="35.00",
            amount_vat="0.00",
            amount_gross="35.00",
            vat_rate=None,
            reverse_charge=False,
            arithmetic_ok=True,
            line_items=[],
            field_confidence=_make_field_confidence(),
            overall_confidence=0.9,
            extraction_notes=None,
        )

        flagged = [
            TransactionCandidate(
                txn_id="txn1",
                description_canonical="FIGMA INC",
                booking_date=date(2025, 11, 15),
                currency="USD",
                amount=Decimal("35.00"),
                amount_gbp=Decimal("28.00"),
            )
        ]

        now = datetime(2025, 11, 16, 12, 0, 0, tzinfo=UTC)

        matched = _match_invoice_to_flagged(
            conn=test_db,
            invoice_id="inv1",
            extraction=extraction,
            flagged_txns=flagged,
            now=now,
        )

        assert matched == "txn1"

        # Verify reconciliation row created
        row = test_db.execute(
            "SELECT * FROM reconciliation_rows WHERE invoice_id = 'inv1'"
        ).fetchone()
        assert row is not None
        assert row["txn_id"] == "txn1"
        assert row["state"] == "user_verified"

    def test_no_match_for_different_amount(self, test_db):
        """Invoice with very different amount should not match."""
        from execution.invoice.extractor import ExtractorResult
        from execution.reconcile.match import TransactionCandidate

        extraction = ExtractorResult(
            supplier_name="Figma Inc",
            supplier_address=None,
            supplier_vat_number=None,
            customer_name=None,
            customer_address=None,
            invoice_number="INV-001",
            invoice_date="2025-11-14",
            supply_date=None,
            description="Figma subscription",
            currency="USD",
            amount_net="999.00",  # Very different amount
            amount_vat="0.00",
            amount_gross="999.00",
            vat_rate=None,
            reverse_charge=False,
            arithmetic_ok=True,
            line_items=[],
            field_confidence=_make_field_confidence(),
            overall_confidence=0.9,
            extraction_notes=None,
        )

        flagged = [
            TransactionCandidate(
                txn_id="txn1",
                description_canonical="FIGMA INC",
                booking_date=date(2025, 11, 15),
                currency="USD",
                amount=Decimal("35.00"),
                amount_gbp=Decimal("28.00"),
            )
        ]

        now = datetime(2025, 11, 16, 12, 0, 0, tzinfo=UTC)

        matched = _match_invoice_to_flagged(
            conn=test_db,
            invoice_id="inv1",
            extraction=extraction,
            flagged_txns=flagged,
            now=now,
        )

        assert matched is None


class TestProcessBulkUpload:
    """Test synchronous bulk upload wrapper."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create test database."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE transactions (
                txn_id TEXT PRIMARY KEY,
                booking_date TEXT NOT NULL,
                description_canonical TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_gbp TEXT NOT NULL,
                status TEXT DEFAULT 'settled',
                deleted_at TEXT,
                needs_manual_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                invoice_id TEXT,
                txn_id TEXT,
                fiscal_year TEXT,
                state TEXT
            );

            CREATE TABLE reconciliation_links (
                row_id TEXT NOT NULL,
                invoice_id TEXT,
                txn_id TEXT,
                allocated_amount_gbp TEXT,
                link_kind TEXT
            );
            CREATE UNIQUE INDEX ux_links_triple
                ON reconciliation_links(row_id, COALESCE(invoice_id, ''), COALESCE(txn_id, ''));
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_empty_list_returns_empty_stats(self, test_db, tmp_path):
        """Empty file list should return empty stats."""
        mock_deps = {
            "llm_client": MagicMock(),
            "google": MagicMock(),
            "extractor_prompt": MagicMock(),
        }

        stats = process_bulk_upload(
            test_db,
            [],
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert stats.total_files == 0
        assert stats.processed == 0


class TestUploadResult:
    """Test UploadResult dataclass."""

    def test_defaults(self):
        result = UploadResult(file_path="test.pdf", success=True)
        assert result.invoice_id is None
        assert result.matched_txn_id is None
        assert result.error is None
        assert not result.skipped_duplicate

    def test_with_match(self):
        result = UploadResult(
            file_path="test.pdf",
            success=True,
            invoice_id="inv1",
            matched_txn_id="txn1",
        )
        assert result.matched_txn_id == "txn1"


class TestBulkUploadStats:
    """Test BulkUploadStats dataclass."""

    def test_defaults(self):
        stats = BulkUploadStats()
        assert stats.total_files == 0
        assert stats.processed == 0
        assert stats.results == []

    def test_accumulates_results(self):
        stats = BulkUploadStats(total_files=2)
        stats.results.append(UploadResult(file_path="a.pdf", success=True))
        stats.results.append(UploadResult(file_path="b.pdf", success=False, error="failed"))

        assert len(stats.results) == 2
