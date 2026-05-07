"""Tests for statement parser module."""

from __future__ import annotations

import json
import tempfile
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
    _parse_llm_response,
    parse_statement,
)


class TestSupportedAccounts:
    """Test account type validation."""

    def test_supported_accounts_list(self) -> None:
        assert SUPPORTED_ACCOUNTS == ("amex", "wise", "tide", "monzo")

    def test_unsupported_account_raises(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            with pytest.raises(UnsupportedAccountError) as exc_info:
                parse_statement(Path(f.name), "unknown")  # type: ignore
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
    """Test PDF parsing with mock LLM."""

    def test_mock_mode_returns_mock_data(self, tmp_path: Path) -> None:
        # Create a minimal valid PDF
        pdf_file = tmp_path / "statement.pdf"
        _create_minimal_pdf(pdf_file)

        result = parse_statement(pdf_file, "amex", mock=True)

        assert result.account == "amex"
        assert len(result.transactions) == 3
        assert result.extraction_method == "mock"
        assert "Mock mode" in result.warnings[0]

    def test_pdf_without_client_raises(self, tmp_path: Path) -> None:
        pdf_file = tmp_path / "statement.pdf"
        _create_minimal_pdf(pdf_file)

        with pytest.raises(ExtractionError) as exc_info:
            parse_statement(pdf_file, "amex", mock=False, claude_client=None)
        assert "Claude client required" in str(exc_info.value)

    def test_pdf_too_large_raises(self, tmp_path: Path) -> None:
        pdf_file = tmp_path / "large.pdf"
        # Create a file larger than MAX_PDF_SIZE_BYTES (20MB)
        pdf_file.write_bytes(b"x" * (21 * 1024 * 1024))

        with pytest.raises(ExtractionError) as exc_info:
            parse_statement(pdf_file, "amex", mock=True)
        assert "too large" in str(exc_info.value).lower()

    def test_unsupported_file_type_raises(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "statement.txt"
        txt_file.write_text("Some text")

        with pytest.raises(ExtractionError) as exc_info:
            parse_statement(txt_file, "amex")
        assert "Unsupported file type" in str(exc_info.value)


class TestLlmResponseParsing:
    """Test parsing of LLM JSON responses."""

    def test_parse_valid_response(self) -> None:
        response = json.dumps(
            {
                "transactions": [
                    {
                        "date": "2025-11-15",
                        "description": "MERCHANT",
                        "amount": "-50.00",
                        "currency": "GBP",
                        "balance": "1000.00",
                    },
                    {
                        "date": "2025-11-16",
                        "description": "REFUND",
                        "amount": "10.00",
                        "currency": "GBP",
                        "balance": "1010.00",
                    },
                ],
                "confidence": 0.95,
                "warnings": [],
            }
        )

        transactions, confidence, warnings = _parse_llm_response(response, "amex")

        assert len(transactions) == 2
        assert transactions[0].date == date(2025, 11, 15)
        assert transactions[0].amount == Decimal("-50.00")
        assert transactions[1].amount == Decimal("10.00")
        assert confidence == 0.95
        assert warnings == []

    def test_parse_response_with_markdown_code_block(self) -> None:
        response = """```json
{
    "transactions": [
        {"date": "2025-11-15", "description": "TEST", "amount": "-25.00", "currency": "GBP", "balance": null}
    ],
    "confidence": 0.90,
    "warnings": ["Minor extraction issue"]
}
```"""
        transactions, confidence, warnings = _parse_llm_response(response, "amex")

        assert len(transactions) == 1
        assert transactions[0].description == "TEST"
        assert confidence == 0.90
        assert "Minor extraction issue" in warnings

    def test_parse_response_invalid_json_raises(self) -> None:
        response = "This is not valid JSON"

        with pytest.raises(ExtractionError) as exc_info:
            _parse_llm_response(response, "amex")
        assert "invalid JSON" in str(exc_info.value)

    def test_parse_response_not_object_raises(self) -> None:
        response = json.dumps([1, 2, 3])

        with pytest.raises(ExtractionError) as exc_info:
            _parse_llm_response(response, "amex")
        assert "not a JSON object" in str(exc_info.value)

    def test_parse_response_invalid_transaction_adds_warning(self) -> None:
        response = json.dumps(
            {
                "transactions": [
                    {
                        "date": "2025-11-15",
                        "description": "VALID",
                        "amount": "-50.00",
                        "currency": "GBP",
                    },
                    {
                        "date": "2025-11-16",
                        "description": "",
                        "amount": "-25.00",
                        "currency": "GBP",
                    },  # Missing description
                ],
                "confidence": 0.85,
                "warnings": [],
            }
        )

        transactions, _confidence, warnings = _parse_llm_response(response, "amex")

        assert len(transactions) == 1  # Only valid one
        assert len(warnings) == 1
        assert "Transaction 1" in warnings[0]


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
            extraction_method="pdf_llm",
            warnings=["Some warning"],
        )

        d = result.to_dict()

        assert d["account"] == "wise"
        assert len(d["transactions"]) == 1
        assert d["confidence"] == 0.92
        assert d["source_file"] == "statement.pdf"
        assert d["page_count"] == 3
        assert d["transaction_count"] == 1
        assert d["extraction_method"] == "pdf_llm"
        assert d["warnings"] == ["Some warning"]


class TestCleanText:
    """Test text cleaning utility."""

    def test_removes_control_chars(self) -> None:
        # Control chars are removed without adding spaces
        assert _clean_text("Hello\x00World") == "HelloWorld"
        assert _clean_text("Test\x1f\x7fValue") == "TestValue"

    def test_collapses_whitespace(self) -> None:
        assert _clean_text("Hello   World") == "Hello World"
        # Tabs and newlines are control chars (0x09, 0x0A) so they're removed
        # not collapsed to spaces
        assert _clean_text("Test\t\nValue") == "TestValue"
        # But multiple spaces collapse to one
        assert _clean_text("Hello    World") == "Hello World"

    def test_trims_whitespace(self) -> None:
        assert _clean_text("  Hello World  ") == "Hello World"

    def test_handles_empty(self) -> None:
        assert _clean_text("") == ""
        assert _clean_text(None) == ""  # type: ignore


def _create_minimal_pdf(path: Path) -> None:
    """Create a minimal valid PDF file for testing."""
    # Use pdfplumber's ability to read this minimal structure
    # This is a valid minimal PDF that pdfplumber can open
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
