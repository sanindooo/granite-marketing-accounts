"""Bank statement parser — PDF/CSV → normalized transactions.

Supports:
- Amex (PDF): Text extraction with regex parsing
- Wise (PDF): Text extraction with regex parsing (multi-currency)
- Tide (PDF): Direct table extraction via pdfplumber
- Monzo (CSV): Direct CSV parsing

All PDF parsing uses pdfplumber for deterministic extraction without LLM calls.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal

import pdfplumber

from execution.shared.errors import PipelineError

Account = Literal["amex", "wise", "tide", "monzo"]
SUPPORTED_ACCOUNTS: Final[tuple[Account, ...]] = ("amex", "wise", "tide", "monzo")

MAX_PDF_SIZE_BYTES: Final[int] = 20 * 1024 * 1024  # 20 MB
MAX_PDF_PAGES: Final[int] = 50
MAX_CSV_ROWS: Final[int] = 5000

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")


class ExtractionError(PipelineError):
    """Failed to extract transactions from statement."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, source="statement_parser", details=details)


class UnsupportedAccountError(PipelineError):
    """Account type is not supported."""

    def __init__(self, account: str) -> None:
        super().__init__(
            f"Unsupported account type: {account}. Supported: {', '.join(SUPPORTED_ACCOUNTS)}",
            source="statement_parser",
            details={"account": account, "supported": list(SUPPORTED_ACCOUNTS)},
        )


@dataclass(frozen=True, slots=True)
class RawTransaction:
    """A single transaction extracted from a statement."""

    date: date
    description: str
    amount: Decimal
    currency: str
    balance: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "description": self.description,
            "amount": str(self.amount),
            "currency": self.currency,
            "balance": str(self.balance) if self.balance is not None else None,
        }


@dataclass
class ParseResult:
    """Result of parsing a bank statement."""

    account: Account
    transactions: list[RawTransaction]
    confidence: float = 1.0
    source_file: str = ""
    page_count: int = 0
    extraction_method: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def transaction_count(self) -> int:
        return len(self.transactions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "transactions": [t.to_dict() for t in self.transactions],
            "confidence": self.confidence,
            "source_file": self.source_file,
            "page_count": self.page_count,
            "transaction_count": self.transaction_count,
            "extraction_method": self.extraction_method,
            "warnings": self.warnings,
        }


def parse_statement(
    file_path: Path,
    account: Account,
) -> ParseResult:
    """Parse a bank statement file into normalized transactions.

    Args:
        file_path: Path to the statement file (PDF or CSV)
        account: Account type (amex, wise, tide, monzo)

    Returns:
        ParseResult with extracted transactions

    Raises:
        UnsupportedAccountError: If account type is not supported
        ExtractionError: If extraction fails
    """
    if account not in SUPPORTED_ACCOUNTS:
        raise UnsupportedAccountError(account)

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        return _parse_csv(file_path, account)
    elif suffix == ".pdf":
        return _parse_pdf(file_path, account)
    else:
        raise ExtractionError(
            f"Unsupported file type: {suffix}. Expected .pdf or .csv",
            details={"file": str(file_path), "suffix": suffix},
        )


def _parse_csv(file_path: Path, account: Account) -> ParseResult:
    """Parse a CSV statement (currently only Monzo)."""
    if account != "monzo":
        raise ExtractionError(
            f"CSV parsing only supported for Monzo, got {account}",
            details={"account": account},
        )

    return _parse_monzo_csv(file_path)


def _parse_monzo_csv(file_path: Path) -> ParseResult:
    """Parse Monzo CSV export.

    Monzo CSV format (2024+):
    Transaction ID, Date, Time, Type, Name, Emoji, Category,
    Amount, Currency, Local amount, Local currency, Notes and #tags,
    Address, Receipt, Description, Category split, Money Out, Money In, Balance
    """
    transactions: list[RawTransaction] = []
    warnings: list[str] = []

    with file_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        required = {"Date", "Amount", "Currency", "Name"}
        if not required.issubset(set(fieldnames)):
            missing = required - set(fieldnames)
            raise ExtractionError(
                f"Monzo CSV missing required columns: {missing}",
                details={"missing": list(missing), "found": fieldnames},
            )

        for idx, row in enumerate(reader):
            if idx >= MAX_CSV_ROWS:
                warnings.append(f"Truncated at {MAX_CSV_ROWS} rows")
                break

            try:
                txn = _parse_monzo_row(row)
                if txn:
                    transactions.append(txn)
            except (ValueError, InvalidOperation) as e:
                warnings.append(f"Row {idx + 2}: {e}")

    return ParseResult(
        account="monzo",
        transactions=transactions,
        confidence=1.0,
        source_file=file_path.name,
        page_count=0,
        extraction_method="csv_direct",
        warnings=warnings,
    )


def _parse_monzo_row(row: dict[str, str]) -> RawTransaction | None:
    """Parse a single Monzo CSV row."""
    date_str = _clean_text(row.get("Date", ""))
    if not date_str:
        return None

    try:
        txn_date = datetime.strptime(date_str, "%d/%m/%Y").date()  # noqa: DTZ007
    except ValueError:
        txn_date = datetime.strptime(date_str, "%Y-%m-%d").date()  # noqa: DTZ007

    amount_str = _clean_text(row.get("Amount", ""))
    if not amount_str:
        return None
    amount = Decimal(amount_str.replace(",", ""))

    currency = _clean_text(row.get("Currency", "GBP")) or "GBP"

    description = _clean_text(row.get("Name", "")) or _clean_text(row.get("Description", ""))
    if not description:
        return None

    balance_str = _clean_text(row.get("Balance", ""))
    balance = Decimal(balance_str.replace(",", "")) if balance_str else None

    return RawTransaction(
        date=txn_date,
        description=description,
        amount=amount,
        currency=currency,
        balance=balance,
    )


def _parse_pdf(file_path: Path, account: Account) -> ParseResult:
    """Parse a PDF statement using pdfplumber."""
    file_size = file_path.stat().st_size
    if file_size > MAX_PDF_SIZE_BYTES:
        raise ExtractionError(
            f"PDF too large: {file_size} bytes (max {MAX_PDF_SIZE_BYTES})",
            details={"file": str(file_path), "size": file_size},
        )

    try:
        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)
            if page_count > MAX_PDF_PAGES:
                raise ExtractionError(
                    f"PDF has too many pages: {page_count} (max {MAX_PDF_PAGES})",
                    details={"file": str(file_path), "page_count": page_count},
                )

            if account == "tide":
                return _parse_tide_pdf(pdf, file_path)
            elif account == "amex":
                return _parse_amex_pdf(pdf, file_path)
            elif account == "wise":
                return _parse_wise_pdf(pdf, file_path)
            else:
                raise ExtractionError(
                    f"PDF parsing not implemented for {account}",
                    details={"account": account},
                )

    except pdfplumber.pdfminer.pdfparser.PDFSyntaxError as e:
        raise ExtractionError(
            f"Invalid PDF file: {e}",
            details={"file": str(file_path)},
        ) from e


# =============================================================================
# TIDE PDF PARSER - Uses direct table extraction
# =============================================================================

def _parse_tide_pdf(pdf: pdfplumber.pdf.PDF, file_path: Path) -> ParseResult:
    """Parse Tide bank statement PDF using table extraction.

    Tide statements have a clean table with columns:
    Date | Transaction type | Details | Paid in (£) | Paid out (£) | Balance (£)
    """
    transactions: list[RawTransaction] = []
    warnings: list[str] = []

    for page_num, page in enumerate(pdf.pages):
        tables = page.extract_tables()

        for table in tables:
            if not table or len(table) < 2:
                continue

            header_row = _find_header_row(table, ["Date", "Paid in", "Paid out", "Balance"])
            if header_row is None:
                continue

            col_indices = _map_tide_columns(table[header_row])

            for row_idx, row in enumerate(table[header_row + 1:], start=header_row + 2):
                try:
                    txn = _parse_tide_row(row, col_indices)
                    if txn:
                        transactions.append(txn)
                except (ValueError, InvalidOperation) as e:
                    warnings.append(f"Page {page_num + 1}, row {row_idx}: {e}")

    if not transactions:
        warnings.append("No transactions found in PDF")

    return ParseResult(
        account="tide",
        transactions=transactions,
        confidence=1.0,
        source_file=file_path.name,
        page_count=len(pdf.pages),
        extraction_method="pdf_table",
        warnings=warnings,
    )


def _find_header_row(table: list[list[str | None]], keywords: list[str]) -> int | None:
    """Find the row index containing header keywords."""
    for idx, row in enumerate(table):
        row_text = " ".join(str(cell or "") for cell in row).lower()
        if all(kw.lower() in row_text for kw in keywords):
            return idx
    return None


def _map_tide_columns(header_row: list[str | None]) -> dict[str, int]:
    """Map Tide column names to indices."""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        cell_text = str(cell or "").lower().strip()
        if "date" in cell_text:
            mapping["date"] = idx
        elif "transaction type" in cell_text or "type" in cell_text:
            mapping["type"] = idx
        elif "details" in cell_text or "description" in cell_text:
            mapping["details"] = idx
        elif "paid in" in cell_text:
            mapping["paid_in"] = idx
        elif "paid out" in cell_text:
            mapping["paid_out"] = idx
        elif "balance" in cell_text:
            mapping["balance"] = idx
    return mapping


def _parse_tide_row(row: list[str | None], col_indices: dict[str, int]) -> RawTransaction | None:
    """Parse a single Tide table row."""
    date_idx = col_indices.get("date", 0)
    date_str = _clean_text(str(row[date_idx] or ""))
    if not date_str:
        return None

    try:
        txn_date = _parse_date(date_str)
    except ValueError:
        return None

    details_idx = col_indices.get("details", 2)
    type_idx = col_indices.get("type", 1)
    description = _clean_text(str(row[details_idx] or ""))
    if not description:
        txn_type = _clean_text(str(row[type_idx] or ""))
        description = txn_type or "Unknown"

    paid_in_idx = col_indices.get("paid_in", 4)
    paid_out_idx = col_indices.get("paid_out", 5)
    balance_idx = col_indices.get("balance", 6)

    paid_in_str = _clean_text(str(row[paid_in_idx] or ""))
    paid_out_str = _clean_text(str(row[paid_out_idx] or ""))
    balance_str = _clean_text(str(row[balance_idx] or ""))

    if paid_in_str:
        amount = Decimal(paid_in_str.replace(",", ""))
    elif paid_out_str:
        amount = -Decimal(paid_out_str.replace(",", ""))
    else:
        return None

    balance = Decimal(balance_str.replace(",", "")) if balance_str else None

    return RawTransaction(
        date=txn_date,
        description=description,
        amount=amount,
        currency="GBP",
        balance=balance,
    )


# =============================================================================
# AMEX PDF PARSER - Uses text extraction with regex
# =============================================================================

# Pattern: Nov5 Nov5 DESCRIPTION AMOUNT or Nov5 Nov5 DESCRIPTION AMOUNT\nCR
_AMEX_TXN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^([A-Z][a-z]{2}\d{1,2})\s+([A-Z][a-z]{2}\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})(?:\s*\nCR)?$",
    re.MULTILINE,
)

# Alternative pattern for lines with foreign currency
_AMEX_FOREIGN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^([A-Z][a-z]{2}\d{1,2})\s+([A-Z][a-z]{2}\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})\s*$",
    re.MULTILINE,
)


def _parse_amex_pdf(pdf: pdfplumber.pdf.PDF, file_path: Path) -> ParseResult:
    """Parse Amex statement PDF using text extraction.

    Amex statements have transactions in format:
    TransDate ProcessDate Description Amount
    Nov5 Nov5 MERCHANT NAME CITY 123.45
    """
    transactions: list[RawTransaction] = []
    warnings: list[str] = []
    statement_year: int | None = None

    for page_num, page in enumerate(pdf.pages):
        text = page.extract_text() or ""

        if statement_year is None:
            statement_year = _extract_amex_statement_year(text)

        page_txns, page_warnings = _parse_amex_page(text, statement_year or 2025, page_num + 1)
        transactions.extend(page_txns)
        warnings.extend(page_warnings)

    if not transactions:
        warnings.append("No transactions found in PDF")

    return ParseResult(
        account="amex",
        transactions=transactions,
        confidence=1.0,
        source_file=file_path.name,
        page_count=len(pdf.pages),
        extraction_method="pdf_text",
        warnings=warnings,
    )


def _extract_amex_statement_year(text: str) -> int | None:
    """Extract statement year from Amex header."""
    # Look for "Statement Period From 4November to3December2025"
    match = re.search(r"Statement Period.*?(\d{4})", text)
    if match:
        return int(match.group(1))

    # Look for date patterns like "03/12/25"
    match = re.search(r"\d{2}/\d{2}/(\d{2})", text)
    if match:
        year_short = int(match.group(1))
        return 2000 + year_short

    return None


def _parse_amex_page(text: str, year: int, page_num: int) -> tuple[list[RawTransaction], list[str]]:
    """Parse transactions from a single Amex page."""
    transactions: list[RawTransaction] = []
    warnings: list[str] = []

    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Look for transaction pattern: MonDD MonDD Description Amount
        match = re.match(
            r"^([A-Z][a-z]{2})(\d{1,2})\s+([A-Z][a-z]{2})(\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})$",
            line,
        )

        if match:
            txn_month = match.group(1)
            txn_day = int(match.group(2))
            description = match.group(5).strip()
            amount_str = match.group(6)

            # Check next line for CR (credit indicator)
            is_credit = False
            if i + 1 < len(lines) and lines[i + 1].strip() == "CR":
                is_credit = True
                i += 1

            # Skip foreign currency info lines
            while i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if (
                    re.match(r"^(UNITED STATES DOLLAR|Exchange Rate|ROUTING:|TO:|TICKET NUMBER:|PASSENGER NAME:)", next_line)
                    or re.match(r"^[\d,]+\.\d{2}$", next_line)
                    or re.match(r"^\+Nonsterling Transaction Fee", next_line)
                ):
                    i += 1
                else:
                    break

            try:
                txn_date = _parse_amex_date(txn_month, txn_day, year)
                amount = Decimal(amount_str.replace(",", ""))
                if not is_credit:
                    amount = -amount

                transactions.append(
                    RawTransaction(
                        date=txn_date,
                        description=description,
                        amount=amount,
                        currency="GBP",
                        balance=None,
                    )
                )
            except (ValueError, InvalidOperation) as e:
                warnings.append(f"Page {page_num}, line {i + 1}: {e}")

        i += 1

    return transactions, warnings


def _parse_amex_date(month_str: str, day: int, year: int) -> date:
    """Parse Amex date format (e.g., 'Nov', 5 -> date)."""
    months = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }
    month = months.get(month_str)
    if month is None:
        raise ValueError(f"Unknown month: {month_str}")
    return date(year, month, day)


# =============================================================================
# WISE PDF PARSER - Uses text extraction with line-by-line parsing
# =============================================================================

# Wise transaction line ends with amounts: [incoming] [outgoing] [balance]
# Pattern captures description and trailing numbers
_WISE_AMOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(.+?)\s+([-]?[\d,]+\.\d{2})\s+([\d,]+\.\d{2})$"
)

# Alternative: three numbers at end (incoming, outgoing, balance)
_WISE_THREE_AMOUNTS: Final[re.Pattern[str]] = re.compile(
    r"^(.+?)\s+([\d,]+\.\d{2})\s+([-]?[\d,]+\.\d{2})\s+([\d,]+\.\d{2})$"
)

# Date line pattern: "28 February 2026 ..."
_WISE_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})"
)


def _parse_wise_pdf(pdf: pdfplumber.pdf.PDF, file_path: Path) -> ParseResult:
    """Parse Wise statement PDF using text extraction.

    Wise statements have multi-line transactions:
    Line 1: Description [incoming] [outgoing] balance
    Line 2: DD Month YYYY Card/Transaction details
    """
    transactions: list[RawTransaction] = []
    warnings: list[str] = []

    # Extract currency from filename or first page
    currency = _extract_wise_currency(file_path, pdf)

    for page_num, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        page_txns, page_warnings = _parse_wise_page(text, currency, page_num + 1)
        transactions.extend(page_txns)
        warnings.extend(page_warnings)

    if not transactions:
        warnings.append("No transactions found in PDF")

    return ParseResult(
        account="wise",
        transactions=transactions,
        confidence=1.0,
        source_file=file_path.name,
        page_count=len(pdf.pages),
        extraction_method="pdf_text",
        warnings=warnings,
    )


def _extract_wise_currency(file_path: Path, pdf: pdfplumber.pdf.PDF) -> str:
    """Extract currency from Wise statement filename or content."""
    # Try filename first: statement_37433713_GBP_2025-03-01_2026-02-28.pdf
    filename = file_path.stem
    match = re.search(r"_([A-Z]{3})_\d{4}-\d{2}-\d{2}", filename)
    if match:
        return match.group(1)

    # Try first page header
    if pdf.pages:
        text = pdf.pages[0].extract_text() or ""
        match = re.search(r"^([A-Z]{3}) statement", text, re.MULTILINE)
        if match:
            return match.group(1)

    return "GBP"


def _parse_wise_page(text: str, currency: str, page_num: int) -> tuple[list[RawTransaction], list[str]]:
    """Parse transactions from a single Wise page."""
    transactions: list[RawTransaction] = []
    warnings: list[str] = []

    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Skip header and footer lines
        if _is_wise_header_footer(line):
            i += 1
            continue

        # Try to match transaction line (description + amounts)
        # Wise format: "Description text -amount balance" or "Description text amount balance"

        # Check for two numbers at end of line
        match = _WISE_AMOUNT_PATTERN.match(line)
        if match:
            description = match.group(1).strip()
            amount_str = match.group(2)
            balance_str = match.group(3)

            # Look for date on next line
            txn_date = None
            if i + 1 < len(lines):
                date_match = _WISE_DATE_PATTERN.match(lines[i + 1].strip())
                if date_match:
                    try:
                        txn_date = _parse_wise_date(
                            int(date_match.group(1)),
                            date_match.group(2),
                            int(date_match.group(3)),
                        )
                        i += 1  # Skip the date line
                    except ValueError:
                        pass

            if txn_date:
                try:
                    amount = Decimal(amount_str.replace(",", ""))
                    balance = Decimal(balance_str.replace(",", ""))

                    # Clean up description
                    description = _clean_wise_description(description)

                    if description:
                        transactions.append(
                            RawTransaction(
                                date=txn_date,
                                description=description,
                                amount=amount,
                                currency=currency,
                                balance=balance,
                            )
                        )
                except (ValueError, InvalidOperation) as e:
                    warnings.append(f"Page {page_num}, line {i + 1}: {e}")

        i += 1

    return transactions, warnings


def _is_wise_header_footer(line: str) -> bool:
    """Check if line is a Wise header/footer to skip."""
    skip_patterns = [
        "Wise Payments Ltd",
        "GBP statement",
        "USD statement",
        "EUR statement",
        "Generated on:",
        "Account Holder",
        "Account number",
        "IBAN",
        "Swift/BIC",
        "Description Incoming Outgoing Amount",
        "ref:76a8b2b1",
        "/ 18",  # Page numbers
    ]
    return any(pattern in line for pattern in skip_patterns)


def _parse_wise_date(day: int, month_str: str, year: int) -> date:
    """Parse Wise date format."""
    months = {
        "January": 1, "February": 2, "March": 3, "April": 4,
        "May": 5, "June": 6, "July": 7, "August": 8,
        "September": 9, "October": 10, "November": 11, "December": 12,
    }
    month = months.get(month_str)
    if month is None:
        raise ValueError(f"Unknown month: {month_str}")
    return date(year, month, day)


def _clean_wise_description(description: str) -> str:
    """Clean up Wise transaction description."""
    # Remove amounts that might be embedded in description
    # e.g., "Card transaction of 76.00 THB issued by Www.grab.com BANGKOK"
    # We want to keep the merchant name

    # Remove leading transaction type info if it contains the original amount
    if description.startswith("Card transaction of"):
        # Extract just the merchant part
        match = re.search(r"issued by\s+(.+)$", description)
        if match:
            description = match.group(1)

    # Remove "Sent money to " prefix but keep recipient
    if description.startswith("Sent money to "):
        description = description[14:]

    # Remove "Received money from " prefix
    if description.startswith("Received money from "):
        description = description[20:]

    # Clean up "Paid to " prefix
    if description.startswith("Paid to "):
        description = description[8:]

    # Handle "Moved X.XX GBP from Tax"
    if description.startswith("Moved "):
        match = re.search(r"from\s+(.+)$", description)
        if match:
            description = f"Transfer from {match.group(1)}"

    # Handle "Converted X.XX USD to Y.YY GBP"
    if description.startswith("Converted "):
        description = "Currency conversion"

    return _clean_text(description)


def _parse_date(date_str: str) -> date:
    """Parse various date formats."""
    formats = [
        "%d %b %Y",      # 28 Feb 2025
        "%d %B %Y",      # 28 February 2025
        "%d/%m/%Y",      # 28/02/2025
        "%Y-%m-%d",      # 2025-02-28
        "%d-%m-%Y",      # 28-02-2025
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()  # noqa: DTZ007
        except ValueError:
            continue

    raise ValueError(f"Cannot parse date: {date_str}")


def _clean_text(text: str) -> str:
    """Strip control chars, collapse whitespace, and trim."""
    out = _CONTROL_CHARS.sub("", text or "")
    return _WHITESPACE.sub(" ", out).strip()


__all__ = [
    "SUPPORTED_ACCOUNTS",
    "Account",
    "ExtractionError",
    "ParseResult",
    "RawTransaction",
    "UnsupportedAccountError",
    "parse_statement",
]
