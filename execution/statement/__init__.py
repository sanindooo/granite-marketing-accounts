"""Bank statement parsing for reconciliation.

Supports PDF (Amex, Wise, Tide) and CSV (Monzo) statement formats.
PDF extraction uses pdfplumber text extraction + Claude haiku parsing.
"""

from execution.statement.parser import (
    SUPPORTED_ACCOUNTS,
    ExtractionError,
    ParseResult,
    UnsupportedAccountError,
    parse_statement,
)

__all__ = [
    "SUPPORTED_ACCOUNTS",
    "ExtractionError",
    "ParseResult",
    "UnsupportedAccountError",
    "parse_statement",
]
