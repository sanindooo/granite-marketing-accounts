"""Tests for auto-process trigger."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from execution.reconcile.auto_process import (
    AutoProcessResult,
    _find_invoice_for_email,
    _link_transaction_to_invoice,
    auto_process_matched_email,
)
from execution.reconcile.match import MatchState
from execution.reconcile.transaction_matcher import (
    EmailMatchType,
    TransactionMatchResult,
)


class TestAutoProcessMatchedEmail:
    """Test auto-process trigger for matched emails."""

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
                invoice_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount_gross TEXT NOT NULL,
                amount_gross_gbp TEXT,
                deleted_at TEXT
            );

            CREATE TABLE emails (
                msg_id TEXT PRIMARY KEY,
                from_addr TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT,
                outcome TEXT,
                source_adapter TEXT DEFAULT 'ms365'
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
                updated_at TEXT,
                last_run_id TEXT
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

            INSERT INTO vendors (vendor_id, canonical_name, domain)
            VALUES ('v1', 'Anthropic', 'anthropic.com');

            INSERT INTO emails (msg_id, from_addr, received_at)
            VALUES ('email1', 'billing@anthropic.com', '2025-11-14T10:00:00Z');

            INSERT INTO transactions (
                txn_id, account, txn_type, booking_date,
                description_raw, description_canonical,
                currency, amount, amount_gbp
            )
            VALUES (
                'txn1', 'amex', 'purchase', '2025-11-15',
                'ANTHROPIC API', 'ANTHROPIC API',
                'GBP', '-49.99', '-49.99'
            );
        """)
        conn.commit()
        yield conn
        conn.close()

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies for processing."""
        return {
            "adapter": MagicMock(),
            "llm_client": MagicMock(),
            "google": MagicMock(),
            "classifier_prompt": MagicMock(),
            "extractor_prompt": MagicMock(),
        }

    def test_no_email_match_returns_error(self, test_db, mock_deps, tmp_path) -> None:
        """Result without email match should return error."""
        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert not result.success
        assert result.error == "no email match in result"

    def test_third_party_link_requires_manual_download(self, test_db, mock_deps, tmp_path) -> None:
        """Third-party link emails should not be auto-processed."""
        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="email1",
            email_match_type=EmailMatchType.THIRD_PARTY_LINK,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert not result.success
        assert "manual download" in result.error

    def test_email_not_found_returns_error(self, test_db, mock_deps, tmp_path) -> None:
        """Missing email should return error."""
        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="nonexistent",
            email_match_type=EmailMatchType.INLINE_INVOICE,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert not result.success
        assert "not found" in result.error

    def test_already_processed_links_existing_invoice(self, test_db, mock_deps, tmp_path) -> None:
        """Already processed email with invoice should link to transaction."""
        # Mark email as processed and add an invoice
        test_db.execute(
            "UPDATE emails SET processed_at = '2025-11-14T12:00:00Z', outcome = 'invoice' WHERE msg_id = 'email1'"
        )
        test_db.execute("""
            INSERT INTO invoices (invoice_id, vendor_id, source_msg_id, invoice_date, currency, amount_gross, amount_gross_gbp)
            VALUES ('inv1', 'v1', 'email1', '2025-11-14', 'GBP', '49.99', '49.99')
        """)
        test_db.commit()

        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="email1",
            email_match_type=EmailMatchType.INLINE_INVOICE,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert result.success
        assert result.invoice_id == "inv1"
        assert result.already_processed

        # Verify reconciliation row was created
        row = test_db.execute(
            "SELECT * FROM reconciliation_rows WHERE invoice_id = 'inv1' AND txn_id = 'txn1'"
        ).fetchone()
        assert row is not None
        assert row["state"] == "auto_matched"

    def test_already_processed_no_invoice_returns_error(self, test_db, mock_deps, tmp_path) -> None:
        """Already processed email without invoice should return error."""
        test_db.execute(
            "UPDATE emails SET processed_at = '2025-11-14T12:00:00Z', outcome = 'neither' WHERE msg_id = 'email1'"
        )
        test_db.commit()

        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="email1",
            email_match_type=EmailMatchType.INLINE_INVOICE,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert not result.success
        assert result.already_processed
        assert "no invoice found" in result.error

    @patch("execution.invoice.processor.process_pending_emails")
    def test_successful_processing_links_invoice(
        self, mock_process, test_db, mock_deps, tmp_path
    ) -> None:
        """Successful processing should create invoice and link to transaction."""
        from execution.invoice.processor import ProcessStats

        # Mock successful processing
        mock_process.return_value = ProcessStats(
            processed=1,
            classified_invoice=1,
            filed=1,
        )

        # Add invoice that would be created by processing
        test_db.execute("""
            INSERT INTO invoices (invoice_id, vendor_id, source_msg_id, invoice_date, currency, amount_gross, amount_gross_gbp)
            VALUES ('inv1', 'v1', 'email1', '2025-11-14', 'GBP', '49.99', '49.99')
        """)
        test_db.commit()

        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="email1",
            email_match_type=EmailMatchType.INLINE_INVOICE,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert result.success
        assert result.invoice_id == "inv1"

        # Verify processing was called with correct msg_id
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs.get("msg_ids") == ["email1"]

        # Verify reconciliation row
        row = test_db.execute(
            "SELECT * FROM reconciliation_rows WHERE invoice_id = 'inv1' AND txn_id = 'txn1'"
        ).fetchone()
        assert row is not None
        assert row["state"] == "auto_matched"

    @patch("execution.invoice.processor.process_pending_emails")
    def test_processing_error_flags_transaction(
        self, mock_process, test_db, mock_deps, tmp_path
    ) -> None:
        """Processing error should flag transaction for manual review."""
        from execution.invoice.processor import ProcessStats

        mock_process.return_value = ProcessStats(
            processed=1,
            errors=1,
            error_details=[{"msg_id": "email1", "error": "corrupt PDF"}],
        )

        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="email1",
            email_match_type=EmailMatchType.INLINE_INVOICE,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert not result.success
        assert "corrupt PDF" in result.error

        # Verify transaction was flagged
        txn = test_db.execute(
            "SELECT needs_manual_download FROM transactions WHERE txn_id = 'txn1'"
        ).fetchone()
        assert txn["needs_manual_download"] == 1

    @patch("execution.invoice.processor.process_pending_emails")
    def test_processing_exception_flags_transaction(
        self, mock_process, test_db, mock_deps, tmp_path
    ) -> None:
        """Processing exception should flag transaction for manual review."""
        mock_process.side_effect = Exception("API timeout")

        match_result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="email1",
            email_match_type=EmailMatchType.INLINE_INVOICE,
        )

        result = auto_process_matched_email(
            test_db,
            match_result,
            tmp_root=tmp_path,
            **mock_deps,
        )

        assert not result.success
        assert "API timeout" in result.error

        # Verify transaction was flagged
        txn = test_db.execute(
            "SELECT needs_manual_download FROM transactions WHERE txn_id = 'txn1'"
        ).fetchone()
        assert txn["needs_manual_download"] == 1


class TestFindInvoiceForEmail:
    """Test finding invoice by source email."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create a test database."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE invoices (
                invoice_id TEXT PRIMARY KEY,
                vendor_id TEXT NOT NULL,
                source_msg_id TEXT,
                invoice_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount_gross TEXT NOT NULL,
                deleted_at TEXT
            );
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_finds_invoice(self, test_db) -> None:
        test_db.execute("""
            INSERT INTO invoices (invoice_id, vendor_id, source_msg_id, invoice_date, currency, amount_gross)
            VALUES ('inv1', 'v1', 'email1', '2025-11-14', 'GBP', '49.99')
        """)
        test_db.commit()

        result = _find_invoice_for_email(test_db, "email1")
        assert result == "inv1"

    def test_returns_none_when_not_found(self, test_db) -> None:
        result = _find_invoice_for_email(test_db, "nonexistent")
        assert result is None

    def test_ignores_deleted_invoices(self, test_db) -> None:
        test_db.execute("""
            INSERT INTO invoices (invoice_id, vendor_id, source_msg_id, invoice_date, currency, amount_gross, deleted_at)
            VALUES ('inv1', 'v1', 'email1', '2025-11-14', 'GBP', '49.99', '2025-11-15T00:00:00Z')
        """)
        test_db.commit()

        result = _find_invoice_for_email(test_db, "email1")
        assert result is None


class TestLinkTransactionToInvoice:
    """Test creating reconciliation row."""

    @pytest.fixture
    def test_db(self, tmp_path):
        """Create a test database."""
        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        conn.executescript("""
            CREATE TABLE invoices (
                invoice_id TEXT PRIMARY KEY,
                vendor_id TEXT NOT NULL,
                invoice_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount_gross TEXT NOT NULL,
                deleted_at TEXT
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

            INSERT INTO invoices (invoice_id, vendor_id, invoice_date, currency, amount_gross)
            VALUES ('inv1', 'v1', '2025-11-14', 'GBP', '49.99');
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_creates_reconciliation_row(self, test_db) -> None:
        now = datetime(2025, 11, 15, 12, 0, 0, tzinfo=UTC)

        _link_transaction_to_invoice(
            test_db,
            txn_id="txn1",
            invoice_id="inv1",
            now=now,
            reason="test link",
        )

        row = test_db.execute(
            "SELECT * FROM reconciliation_rows WHERE invoice_id = 'inv1'"
        ).fetchone()
        assert row is not None
        assert row["txn_id"] == "txn1"
        assert row["state"] == "auto_matched"
        assert row["match_score"] == "1.00"
        assert row["fiscal_year"] == "FY-2025-26"

    def test_creates_reconciliation_link(self, test_db) -> None:
        now = datetime(2025, 11, 15, 12, 0, 0, tzinfo=UTC)

        _link_transaction_to_invoice(
            test_db,
            txn_id="txn1",
            invoice_id="inv1",
            now=now,
            reason="test link",
        )

        link = test_db.execute(
            "SELECT * FROM reconciliation_links WHERE invoice_id = 'inv1' AND txn_id = 'txn1'"
        ).fetchone()
        assert link is not None
        assert link["allocated_amount_gbp"] == "49.99"
        assert link["link_kind"] == "full"


class TestAutoProcessResult:
    """Test AutoProcessResult dataclass."""

    def test_defaults(self) -> None:
        result = AutoProcessResult(success=True)
        assert result.invoice_id is None
        assert result.error is None
        assert result.already_processed is False

    def test_with_invoice(self) -> None:
        result = AutoProcessResult(success=True, invoice_id="inv1")
        assert result.invoice_id == "inv1"

    def test_with_error(self) -> None:
        result = AutoProcessResult(success=False, error="something went wrong")
        assert result.error == "something went wrong"
