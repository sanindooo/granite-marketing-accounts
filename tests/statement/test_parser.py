"""Tests for statement parser module."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from execution.statement.parser import (
    SUPPORTED_ACCOUNTS,
    ExtractionError,
    ParseResult,
    RawTransaction,
    UnsupportedAccountError,
    _clean_text,
    parse_statement,
)


class TestSupportedAccounts:
    """Test account type validation."""

    def test_supported_accounts_list(self) -> None:
        assert SUPPORTED_ACCOUNTS == ("amex", "wise", "tide", "monzo")

    def test_unsupported_account_raises(self, tmp_path: Path) -> None:
        pdf_file = tmp_path / "statement.pdf"
        _create_minimal_pdf(pdf_file)
        with pytest.raises(UnsupportedAccountError) as exc_info:
            parse_statement(pdf_file, "unknown")  # type: ignore
        assert "unknown" in str(exc_info.value)
        assert "amex" in str(exc_info.value)


class TestMonzoCsvParsing:
    """Test Monzo CSV direct parsing."""

    def test_parse_valid_monzo_csv(self, tmp_path: Path) -> None:
        csv_content = """Transaction ID,Date,Time,Type,Name,Emoji,Category,Amount,Currency,Local amount,Local currency,Notes and #tags,Address,Receipt,Description,Category split,Money Out,Money In,Balance
txn_001,15/11/2025,10:30:00,Card payment,ANTHROPIC,,General,-49.99,GBP,-49.99,GBP,,,,,-49.99,,950.01
txn_002,16/11/2025,14:00:00,Card payment,Starbucks,,Eating out,-4.50,GBP,-4.50,GBP,,,,,-4.50,,945.51
txn_003,17/11/2025,09:00:00,Faster payment,Client Ltd,,Income,500.00,GBP,500.00,GBP,,,,,,,1445.51
"""
        csv_file = tmp_path / "monzo_export.csv"
        csv_file.write_text(csv_content)

        result = parse_statement(csv_file, "monzo")

        assert result.account == "monzo"
        assert len(result.transactions) == 3
        assert result.extraction_method == "csv_direct"
        assert result.confidence == 1.0

        # Check first transaction
        txn1 = result.transactions[0]
        assert txn1.date == date(2025, 11, 15)
        assert txn1.description == "ANTHROPIC"
        assert txn1.amount == Decimal("-49.99")
        assert txn1.currency == "GBP"

        # Check income transaction (positive)
        txn3 = result.transactions[2]
        assert txn3.amount == Decimal("500.00")

    def test_parse_monzo_csv_with_balance(self, tmp_path: Path) -> None:
        csv_content = """Transaction ID,Date,Time,Type,Name,Emoji,Category,Amount,Currency,Local amount,Local currency,Notes and #tags,Address,Receipt,Description,Category split,Money Out,Money In,Balance
txn_001,15/11/2025,10:30:00,Card payment,Shop,,General,-25.00,GBP,-25.00,GBP,,,,,,-25.00,,1000.00
"""
        csv_file = tmp_path / "monzo.csv"
        csv_file.write_text(csv_content)

        result = parse_statement(csv_file, "monzo")
        assert result.transactions[0].balance == Decimal("1000.00")

    def test_parse_monzo_csv_missing_columns_raises(self, tmp_path: Path) -> None:
        csv_content = """ID,Date,Amount
1,2025-11-15,-50.00
"""
        csv_file = tmp_path / "bad_monzo.csv"
        csv_file.write_text(csv_content)

        with pytest.raises(ExtractionError) as exc_info:
            parse_statement(csv_file, "monzo")
        assert "missing required columns" in str(exc_info.value).lower()

    def test_csv_only_for_monzo(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "amex.csv"
        csv_file.write_text("Date,Amount\n2025-01-01,-50.00")

        with pytest.raises(ExtractionError) as exc_info:
            parse_statement(csv_file, "amex")
        assert "monzo" in str(exc_info.value).lower()


class TestPdfParsing:
    """Test PDF parsing."""

    def test_pdf_too_large_raises(self, tmp_path: Path) -> None:
        pdf_file = tmp_path / "large.pdf"
        pdf_file.write_bytes(b"x" * (21 * 1024 * 1024))

        with pytest.raises(ExtractionError) as exc_info:
            parse_statement(pdf_file, "amex")
        assert "too large" in str(exc_info.value).lower()

    def test_unsupported_file_type_raises(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "statement.txt"
        txt_file.write_text("Some text")

        with pytest.raises(ExtractionError) as exc_info:
            parse_statement(txt_file, "amex")
        assert "Unsupported file type" in str(exc_info.value)

    def test_minimal_pdf_parses_without_error(self, tmp_path: Path) -> None:
        pdf_file = tmp_path / "statement.pdf"
        _create_minimal_pdf(pdf_file)

        # Should not raise - minimal PDF has no transactions but should parse
        result = parse_statement(pdf_file, "amex")
        assert result.account == "amex"
        assert result.extraction_method == "pdf_text"


class TestRealStatements:
    """Test parsing real statement files if they exist."""

    @pytest.fixture
    def statements_dir(self) -> Path:
        return Path(__file__).parent.parent.parent / "statements"

    def test_tide_pdf(self, statements_dir: Path) -> None:
        tide_pdf = statements_dir / "tidefeb2025.pdf"
        if not tide_pdf.exists():
            pytest.skip("Tide statement not available")

        result = parse_statement(tide_pdf, "tide")

        assert result.account == "tide"
        assert result.extraction_method == "pdf_table"
        assert len(result.transactions) > 0

        # Check transactions have required fields
        for txn in result.transactions:
            assert txn.date is not None
            assert txn.description
            assert txn.currency == "GBP"

    def test_amex_pdf(self, statements_dir: Path) -> None:
        amex_files = list(statements_dir.glob("AMEX*.pdf"))
        if not amex_files:
            pytest.skip("Amex statement not available")

        result = parse_statement(amex_files[0], "amex")

        assert result.account == "amex"
        assert result.extraction_method == "pdf_text"
        assert len(result.transactions) > 0

    def test_wise_pdf(self, statements_dir: Path) -> None:
        wise_dir = statements_dir / "wise"
        if not wise_dir.exists():
            pytest.skip("Wise statements not available")

        wise_files = list(wise_dir.glob("*.pdf"))
        if not wise_files:
            pytest.skip("No Wise PDF files found")

        result = parse_statement(wise_files[0], "wise")

        assert result.account == "wise"
        assert result.extraction_method == "pdf_text"
        assert len(result.transactions) > 0


class TestRawTransaction:
    """Test RawTransaction dataclass."""

    def test_to_dict(self) -> None:
        txn = RawTransaction(
            date=date(2025, 11, 15),
            description="TEST MERCHANT",
            amount=Decimal("-49.99"),
            currency="GBP",
            balance=Decimal("1000.01"),
        )

        d = txn.to_dict()

        assert d["date"] == "2025-11-15"
        assert d["description"] == "TEST MERCHANT"
        assert d["amount"] == "-49.99"
        assert d["currency"] == "GBP"
        assert d["balance"] == "1000.01"

    def test_to_dict_null_balance(self) -> None:
        txn = RawTransaction(
            date=date(2025, 11, 15),
            description="TEST",
            amount=Decimal("-50.00"),
            currency="GBP",
            balance=None,
        )

        d = txn.to_dict()
        assert d["balance"] is None


class TestParseResult:
    """Test ParseResult dataclass."""

    def test_transaction_count_property(self) -> None:
        result = ParseResult(
            account="amex",
            transactions=[
                RawTransaction(
                    date=date(2025, 1, 1), description="A", amount=Decimal("-10"), currency="GBP"
                ),
                RawTransaction(
                    date=date(2025, 1, 2), description="B", amount=Decimal("-20"), currency="GBP"
                ),
            ],
        )

        assert result.transaction_count == 2

    def test_to_dict(self) -> None:
        result = ParseResult(
            account="wise",
            transactions=[
                RawTransaction(
                    date=date(2025, 11, 15),
                    description="TEST",
                    amount=Decimal("-50.00"),
                    currency="USD",
                ),
            ],
            confidence=0.92,
            source_file="statement.pdf",
            page_count=3,
            extraction_method="pdf_text",
            warnings=["Some warning"],
        )

        d = result.to_dict()

        assert d["account"] == "wise"
        assert len(d["transactions"]) == 1
        assert d["confidence"] == 0.92
        assert d["source_file"] == "statement.pdf"
        assert d["page_count"] == 3
        assert d["transaction_count"] == 1
        assert d["extraction_method"] == "pdf_text"
        assert d["warnings"] == ["Some warning"]


class TestCleanText:
    """Test text cleaning utility."""

    def test_removes_control_chars(self) -> None:
        assert _clean_text("Hello\x00World") == "HelloWorld"
        assert _clean_text("Test\x1f\x7fValue") == "TestValue"

    def test_collapses_whitespace(self) -> None:
        assert _clean_text("Hello   World") == "Hello World"
        assert _clean_text("Test\t\nValue") == "TestValue"
        assert _clean_text("Hello    World") == "Hello World"

    def test_trims_whitespace(self) -> None:
        assert _clean_text("  Hello World  ") == "Hello World"

    def test_handles_empty(self) -> None:
        assert _clean_text("") == ""
        assert _clean_text(None) == ""  # type: ignore


def _create_minimal_pdf(path: Path) -> None:
    """Create a minimal valid PDF file for testing."""
    pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
   /Contents 4 0 R /Resources << >> >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT
/F1 12 Tf
100 700 Td
(Test Statement) Tj
ET
endstream
endobj
xref
0 5
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000214 00000 n
trailer
<< /Size 5 /Root 1 0 R >>
startxref
308
%%EOF
"""
    path.write_bytes(pdf_content)
