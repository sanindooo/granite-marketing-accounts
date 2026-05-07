"""Bank statement parser — PDF/CSV → normalized transactions.

Supports:
- Amex (PDF): Text extraction + Claude haiku parsing
- Wise (PDF): Text extraction + Claude haiku parsing (multi-currency)
- Tide (PDF): Text extraction + Claude haiku parsing
- Monzo (CSV): Direct parsing, no LLM

PDF extraction uses pdfplumber for text, then Claude haiku to parse
the narrative format into structured JSON transactions. This approach
handles bank-specific formatting variations without brittle table parsing.

Cost: ~$0.02-0.10 per PDF statement depending on page count.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import pdfplumber

from execution.shared.errors import PipelineError

if TYPE_CHECKING:
    from execution.shared.claude_client import ClaudeClient

Account = Literal["amex", "wise", "tide", "monzo"]
SUPPORTED_ACCOUNTS: Final[tuple[Account, ...]] = ("amex", "wise", "tide", "monzo")

MAX_PDF_SIZE_BYTES: Final[int] = 20 * 1024 * 1024  # 20 MB
MAX_PDF_PAGES: Final[int] = 50
MAX_CSV_ROWS: Final[int] = 5000

# Regex for cleaning text
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
    *,
    claude_client: ClaudeClient | None = None,
    mock: bool = False,
) -> ParseResult:
    """Parse a bank statement file into normalized transactions.

    Args:
        file_path: Path to the statement file (PDF or CSV)
        account: Account type (amex, wise, tide, monzo)
        claude_client: Claude client for PDF extraction (required for PDF unless mock=True)
        mock: If True, return mock data without calling LLM

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
        return _parse_pdf(file_path, account, claude_client=claude_client, mock=mock)
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

        # Validate we have the expected columns
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

    # Monzo dates are DD/MM/YYYY
    try:
        txn_date = datetime.strptime(date_str, "%d/%m/%Y").date()  # noqa: DTZ007
    except ValueError:
        # Try ISO format as fallback
        txn_date = datetime.strptime(date_str, "%Y-%m-%d").date()  # noqa: DTZ007

    amount_str = _clean_text(row.get("Amount", ""))
    if not amount_str:
        return None
    amount = Decimal(amount_str.replace(",", ""))

    currency = _clean_text(row.get("Currency", "GBP")) or "GBP"

    # Description from Name or Description field
    description = _clean_text(row.get("Name", "")) or _clean_text(row.get("Description", ""))
    if not description:
        return None

    # Balance if present
    balance_str = _clean_text(row.get("Balance", ""))
    balance = Decimal(balance_str.replace(",", "")) if balance_str else None

    return RawTransaction(
        date=txn_date,
        description=description,
        amount=amount,
        currency=currency,
        balance=balance,
    )


def _parse_pdf(
    file_path: Path,
    account: Account,
    *,
    claude_client: ClaudeClient | None = None,
    mock: bool = False,
) -> ParseResult:
    """Parse a PDF statement using text extraction + LLM."""
    # Check file size
    file_size = file_path.stat().st_size
    if file_size > MAX_PDF_SIZE_BYTES:
        raise ExtractionError(
            f"PDF too large: {file_size} bytes (max {MAX_PDF_SIZE_BYTES})",
            details={"file": str(file_path), "size": file_size},
        )

    # Extract text from PDF
    text, page_count = _extract_pdf_text(file_path)

    if not text.strip():
        raise ExtractionError(
            "No text extracted from PDF",
            details={"file": str(file_path), "page_count": page_count},
        )

    if mock:
        return _mock_pdf_result(file_path, account, page_count)

    if claude_client is None:
        raise ExtractionError(
            "Claude client required for PDF extraction (or use mock=True)",
            details={"file": str(file_path)},
        )

    # Parse with LLM
    return _parse_with_llm(file_path, account, text, page_count, claude_client)


def _extract_pdf_text(file_path: Path) -> tuple[str, int]:
    """Extract all text from a PDF using pdfplumber."""
    text_parts: list[str] = []
    page_count = 0

    try:
        with pdfplumber.open(file_path) as pdf:
            page_count = len(pdf.pages)
            if page_count > MAX_PDF_PAGES:
                raise ExtractionError(
                    f"PDF has too many pages: {page_count} (max {MAX_PDF_PAGES})",
                    details={"file": str(file_path), "page_count": page_count},
                )

            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)

    except pdfplumber.pdfminer.pdfparser.PDFSyntaxError as e:
        raise ExtractionError(
            f"Invalid PDF file: {e}",
            details={"file": str(file_path)},
        ) from e

    return "\n\n--- Page Break ---\n\n".join(text_parts), page_count


def _parse_with_llm(
    file_path: Path,
    account: Account,
    text: str,
    page_count: int,
    claude_client: ClaudeClient,
) -> ParseResult:
    """Parse statement text using Claude haiku."""
    from execution.shared.claude_client import HAIKU
    from execution.shared.prompts import LoadedPrompt

    # Build the prompt
    prompt_text = _get_extraction_prompt(account)

    # Create a minimal LoadedPrompt for the extraction
    loaded_prompt = LoadedPrompt(
        name=f"statement_extract_{account}",
        model_id=HAIKU,
        text=prompt_text,
        schema={},  # No strict schema validation for statement extraction
        version=_compute_prompt_version(prompt_text),
        estimated_tokens=len(prompt_text) // 4,
    )

    # Call Claude
    response_text, _call = claude_client.call_with_cached_prompt(
        loaded_prompt=loaded_prompt,
        user_content=f"Extract transactions from this {account.upper()} statement:\n\n{text}",
        max_tokens=4096,
        stage="extract",
        model=HAIKU,
    )

    # Parse response
    transactions, confidence, warnings = _parse_llm_response(response_text, account)

    return ParseResult(
        account=account,
        transactions=transactions,
        confidence=confidence,
        source_file=file_path.name,
        page_count=page_count,
        extraction_method="pdf_llm",
        warnings=warnings,
    )


def _get_extraction_prompt(account: Account) -> str:
    """Get the extraction prompt for a specific account type."""
    prompts_dir = Path(__file__).parent / "prompts"
    prompt_file = prompts_dir / f"extract_{account}.md"

    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")

    # Fallback to generic prompt
    return _get_generic_extraction_prompt(account)


def _get_generic_extraction_prompt(account: Account) -> str:
    """Generic extraction prompt for bank statements."""
    return f"""# Bank Statement Transaction Extractor

You extract transactions from a {account.upper()} bank statement.

Your output must be a single JSON object with this structure:
```json
{{
  "transactions": [
    {{
      "date": "YYYY-MM-DD",
      "description": "Merchant or transaction description",
      "amount": "-123.45",
      "currency": "GBP",
      "balance": "1234.56"
    }}
  ],
  "confidence": 0.95,
  "warnings": []
}}
```

## Rules

1. **Extract all transactions** from the statement, in chronological order (oldest first).

2. **Date format**: Always use ISO format YYYY-MM-DD. Convert from DD/MM/YYYY or other formats.

3. **Amount sign convention**:
   - Negative amounts are money OUT (purchases, payments, fees)
   - Positive amounts are money IN (refunds, credits, deposits)
   - If the statement shows debits/credits separately, use negative for debits.

4. **Currency**: Use ISO 4217 codes (GBP, USD, EUR, etc.). Default to GBP if unclear.

5. **Description**: Extract the merchant name or transaction description. Clean up but preserve key info.

6. **Balance**: Include if shown (running balance after transaction). Set to null if not present.

7. **Confidence**: Set between 0.0 and 1.0 based on extraction quality:
   - 0.95+ : Clean extraction, all fields clearly parsed
   - 0.80-0.95 : Minor ambiguity but likely correct
   - Below 0.80 : Significant uncertainty

8. **Warnings**: List any issues encountered (unclear amounts, missing dates, etc.)

9. **Do not invent data**. If a field is unclear, set to null and add a warning.

Return ONLY the JSON object, no markdown formatting or extra text.
"""


def _compute_prompt_version(prompt_text: str) -> str:
    """Compute a short hash version of the prompt."""
    return hashlib.sha256(prompt_text.encode()).hexdigest()[:8]


def _parse_llm_response(
    response_text: str,
    account: Account,
) -> tuple[list[RawTransaction], float, list[str]]:
    """Parse the LLM response JSON into transactions."""
    # Strip markdown code blocks if present
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (code fence)
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExtractionError(
            f"LLM returned invalid JSON: {e}",
            details={"response": response_text[:500]},
        ) from e

    if not isinstance(data, dict):
        raise ExtractionError(
            "LLM response is not a JSON object",
            details={"type": type(data).__name__},
        )

    raw_transactions = data.get("transactions", [])
    if not isinstance(raw_transactions, list):
        raise ExtractionError(
            "transactions field is not a list",
            details={"type": type(raw_transactions).__name__},
        )

    transactions: list[RawTransaction] = []
    warnings: list[str] = list(data.get("warnings", []))

    for idx, raw in enumerate(raw_transactions):
        try:
            txn = _parse_transaction_dict(raw)
            transactions.append(txn)
        except (ValueError, KeyError, InvalidOperation) as e:
            warnings.append(f"Transaction {idx}: {e}")

    confidence = float(data.get("confidence", 0.9))

    return transactions, confidence, warnings


def _parse_transaction_dict(raw: dict[str, Any]) -> RawTransaction:
    """Parse a transaction dict from LLM output."""
    date_str = raw.get("date")
    if not date_str:
        raise ValueError("missing date")

    # Parse date (ISO format expected)
    try:
        txn_date = date.fromisoformat(date_str)
    except ValueError:
        # Try DD/MM/YYYY fallback
        txn_date = datetime.strptime(date_str, "%d/%m/%Y").date()  # noqa: DTZ007

    description = raw.get("description", "")
    if not description:
        raise ValueError("missing description")

    amount_str = raw.get("amount")
    if amount_str is None:
        raise ValueError("missing amount")
    amount = Decimal(str(amount_str).replace(",", ""))

    currency = raw.get("currency", "GBP")

    balance_str = raw.get("balance")
    balance = Decimal(str(balance_str).replace(",", "")) if balance_str else None

    return RawTransaction(
        date=txn_date,
        description=description,
        amount=amount,
        currency=currency,
        balance=balance,
    )


def _mock_pdf_result(file_path: Path, account: Account, page_count: int) -> ParseResult:
    """Return mock data for testing without LLM calls."""
    mock_transactions = [
        RawTransaction(
            date=date(2025, 11, 15),
            description="MOCK MERCHANT ONE",
            amount=Decimal("-50.00"),
            currency="GBP",
            balance=Decimal("1000.00"),
        ),
        RawTransaction(
            date=date(2025, 11, 16),
            description="MOCK MERCHANT TWO",
            amount=Decimal("-25.50"),
            currency="GBP",
            balance=Decimal("974.50"),
        ),
        RawTransaction(
            date=date(2025, 11, 17),
            description="MOCK REFUND",
            amount=Decimal("10.00"),
            currency="GBP",
            balance=Decimal("984.50"),
        ),
    ]

    return ParseResult(
        account=account,
        transactions=mock_transactions,
        confidence=1.0,
        source_file=file_path.name,
        page_count=page_count,
        extraction_method="mock",
        warnings=["Mock mode - no actual extraction performed"],
    )


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
