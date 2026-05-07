"""Bank statement parsing and storage for reconciliation.

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
from execution.statement.store import (
    StoreResult,
    canonicalize_description,
    compute_txn_id,
    get_unreconciled_transactions,
    mark_needs_manual_download,
    store_transactions,
)

__all__ = [
    "SUPPORTED_ACCOUNTS",
    "ExtractionError",
    "ParseResult",
    "StoreResult",
    "UnsupportedAccountError",
    "canonicalize_description",
    "compute_txn_id",
    "get_unreconciled_transactions",
    "mark_needs_manual_download",
    "parse_statement",
    "store_transactions",
]
