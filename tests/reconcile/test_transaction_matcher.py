"""Tests for transaction-first matcher."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from execution.reconcile.match import MatchState, TransactionCandidate
from execution.reconcile.transaction_matcher import (
    EmailCandidate,
    EmailMatchType,
    TransactionMatchResult,
    _classify_email_attachment,
    _extract_domain,
    _find_best_email_match,
    _vendor_match_score,
    match_transaction,
)


class TestVendorMatchScore:
    """Test email domain to transaction description matching."""

    def test_exact_match(self) -> None:
        score = _vendor_match_score("anthropic.com", "ANTHROPIC API")
        assert score >= Decimal("0.80")

    def test_partial_match(self) -> None:
        score = _vendor_match_score("starbucks.com", "STARBUCKS COFFEE LONDON")
        assert score >= Decimal("0.50")

    def test_no_match(self) -> None:
        score = _vendor_match_score("randomsite.com", "ANTHROPIC API")
        assert score < Decimal("0.50")

    def test_handles_subdomains(self) -> None:
        score = _vendor_match_score("billing.zoom.us", "ZOOM")
        # Should still match reasonably
        assert score >= Decimal("0.30")


class TestExtractDomain:
    """Test email domain extraction."""

    def test_simple_email(self) -> None:
        assert _extract_domain("billing@anthropic.com") == "anthropic.com"

    def test_subdomain(self) -> None:
        assert _extract_domain("noreply@billing.zoom.us") == "billing.zoom.us"

    def test_no_at_sign(self) -> None:
        assert _extract_domain("invalid-email") is None

    def test_normalizes_case(self) -> None:
        assert _extract_domain("User@ANTHROPIC.COM") == "anthropic.com"


class TestFindBestEmailMatch:
    """Test email candidate selection."""

    def test_finds_matching_email(self) -> None:
        txn = TransactionCandidate(
            txn_id="txn1",
            description_canonical="ANTHROPIC API",
            booking_date=date(2025, 11, 15),
            currency="GBP",
            amount=Decimal("-49.99"),
            amount_gbp=Decimal("-49.99"),
        )
        candidates = [
            EmailCandidate(
                msg_id="email1",
                from_addr="billing@anthropic.com",
                received_at=date(2025, 11, 14),
                has_pdf_attachment=True,
                has_download_link=False,
            ),
            EmailCandidate(
                msg_id="email2",
                from_addr="newsletter@marketing.com",
                received_at=date(2025, 11, 15),
                has_pdf_attachment=False,
                has_download_link=False,
            ),
        ]

        result = _find_best_email_match(txn, candidates)
        assert result is not None
        assert result.msg_id == "email1"

    def test_prefers_closer_date(self) -> None:
        txn = TransactionCandidate(
            txn_id="txn1",
            description_canonical="ANTHROPIC API",
            booking_date=date(2025, 11, 15),
            currency="GBP",
            amount=Decimal("-49.99"),
            amount_gbp=Decimal("-49.99"),
        )
        candidates = [
            EmailCandidate(
                msg_id="email1",
                from_addr="billing@anthropic.com",
                received_at=date(2025, 11, 10),  # 5 days before
                has_pdf_attachment=True,
                has_download_link=False,
            ),
            EmailCandidate(
                msg_id="email2",
                from_addr="billing@anthropic.com",
                received_at=date(2025, 11, 14),  # 1 day before
                has_pdf_attachment=True,
                has_download_link=False,
            ),
        ]

        result = _find_best_email_match(txn, candidates)
        assert result is not None
        assert result.msg_id == "email2"

    def test_no_match_below_threshold(self) -> None:
        txn = TransactionCandidate(
            txn_id="txn1",
            description_canonical="ANTHROPIC API",
            booking_date=date(2025, 11, 15),
            currency="GBP",
            amount=Decimal("-49.99"),
            amount_gbp=Decimal("-49.99"),
        )
        candidates = [
            EmailCandidate(
                msg_id="email1",
                from_addr="sales@totallyunrelated.com",
                received_at=date(2025, 11, 14),
                has_pdf_attachment=True,
                has_download_link=False,
            ),
        ]

        result = _find_best_email_match(txn, candidates)
        assert result is None


class TestClassifyEmailAttachment:
    """Test email attachment classification."""

    def test_pdf_attachment_is_inline(self) -> None:
        email = EmailCandidate(
            msg_id="e1",
            from_addr="billing@anthropic.com",
            received_at=date(2025, 11, 15),
            has_pdf_attachment=True,
            has_download_link=False,
        )
        assert _classify_email_attachment(email) == EmailMatchType.INLINE_INVOICE

    def test_download_link_is_third_party(self) -> None:
        email = EmailCandidate(
            msg_id="e1",
            from_addr="billing@zoom.us",
            received_at=date(2025, 11, 15),
            has_pdf_attachment=False,
            has_download_link=True,
        )
        assert _classify_email_attachment(email) == EmailMatchType.THIRD_PARTY_LINK

    def test_unknown_attachment_needs_evaluation(self) -> None:
        """Unknown attachment type should return NEEDS_EVALUATION, not default to INLINE_INVOICE."""
        email = EmailCandidate(
            msg_id="e1",
            from_addr="billing@example.com",
            received_at=date(2025, 11, 15),
            has_pdf_attachment=False,
            has_download_link=False,
        )
        assert _classify_email_attachment(email) == EmailMatchType.NEEDS_EVALUATION


class TestMatchTransaction:
    """Test full transaction matching."""

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
                outcome TEXT
            );

            CREATE TABLE reconciliation_rows (
                row_id TEXT PRIMARY KEY,
                txn_id TEXT,
                invoice_id TEXT
            );

            INSERT INTO vendors (vendor_id, canonical_name, domain)
            VALUES ('v1', 'Anthropic', 'anthropic.com');

            INSERT INTO invoices (invoice_id, vendor_id, invoice_date, currency, amount_gross, amount_gross_gbp)
            VALUES ('inv1', 'v1', '2025-11-14', 'GBP', '49.99', '49.99');
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_matches_invoice_auto(self, test_db) -> None:
        txn = TransactionCandidate(
            txn_id="txn1",
            description_canonical="ANTHROPIC API",
            booking_date=date(2025, 11, 15),
            currency="GBP",
            amount=Decimal("-49.99"),
            amount_gbp=Decimal("-49.99"),
        )

        result = match_transaction(test_db, txn)

        assert result.invoice_id == "inv1"
        assert result.state in (MatchState.AUTO_MATCHED, MatchState.SUGGESTED)
        assert result.invoice_score is not None
        assert result.invoice_score >= Decimal("0.70")

    def test_no_match_returns_unmatched(self, test_db) -> None:
        txn = TransactionCandidate(
            txn_id="txn1",
            description_canonical="RANDOM MERCHANT",
            booking_date=date(2025, 6, 15),  # Far from invoice date
            currency="GBP",
            amount=Decimal("-999.99"),  # Different amount
            amount_gbp=Decimal("-999.99"),
        )

        result = match_transaction(test_db, txn)

        assert result.state == MatchState.UNMATCHED
        assert result.invoice_id is None

    def test_usd_transaction_matches_with_fx_tolerance(self, test_db) -> None:
        """Test AE6: USD transaction matches invoice with FX tolerance."""
        # Add a USD invoice
        test_db.execute("""
            INSERT INTO invoices (invoice_id, vendor_id, invoice_date, currency, amount_gross, amount_gross_gbp)
            VALUES ('inv_usd', 'v1', '2025-11-14', 'USD', '50.00', '40.00')
        """)
        test_db.commit()

        txn = TransactionCandidate(
            txn_id="txn1",
            description_canonical="ANTHROPIC API",
            booking_date=date(2025, 11, 15),
            currency="USD",
            amount=Decimal("-50.00"),
            amount_gbp=Decimal("-41.00"),  # Slightly different due to FX
        )

        result = match_transaction(test_db, txn)

        # Should match with FX tolerance
        assert result.invoice_id is not None
        assert result.state in (MatchState.AUTO_MATCHED, MatchState.SUGGESTED)


class TestTransactionMatchResult:
    """Test TransactionMatchResult dataclass."""

    def test_defaults(self) -> None:
        result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
        )
        assert result.invoice_id is None
        assert result.email_msg_id is None
        assert result.needs_manual_download is False

    def test_with_invoice_match(self) -> None:
        result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.AUTO_MATCHED,
            invoice_id="inv1",
            invoice_score=Decimal("0.95"),
            reason="invoice match",
        )
        assert result.invoice_id == "inv1"
        assert result.invoice_score == Decimal("0.95")

    def test_with_email_needs_download(self) -> None:
        result = TransactionMatchResult(
            txn_id="txn1",
            state=MatchState.UNMATCHED,
            email_msg_id="email1",
            email_match_type=EmailMatchType.THIRD_PARTY_LINK,
            needs_manual_download=True,
        )
        assert result.email_msg_id == "email1"
        assert result.needs_manual_download is True
