---
title: "feat: bank statement reconciliation"
type: feat
status: ready
date: 2026-04-19
origin: docs/brainstorms/2026-04-19-bank-reconciliation-requirements.md
---

# Bank Statement Reconciliation

## Overview

Upload bank statements (PDF/CSV), extract transactions, and match them against captured invoices to surface transactions missing documentation. Dedicated `/reconciliation` page with FY scoping.

## Problem Statement

Business expenses occur via bank transactions, but not every transaction has a captured invoice. Currently there's no systematic way to identify which transactions are missing invoices - creating gaps in financial records and potential tax issues. (see origin: docs/brainstorms/2026-04-19-bank-reconciliation-requirements.md)

## Proposed Solution

Build a dedicated reconciliation page where users can:
1. Upload bank statements (PDF or CSV)
2. See transactions automatically matched to invoices
3. Review uncertain matches
4. Flag transactions as "invoice missing" for follow-up
5. Dismiss personal/irrelevant transactions

**Key architectural decision**: Leverage existing schema (`transactions`, `reconciliation_rows`, `reconciliation_links` tables) rather than creating new tables. Add `INVOICE_MISSING` state to existing state machine.

## Technical Approach

### Existing Infrastructure (Reuse)

| Component | Path | Reuse Strategy |
|-----------|------|----------------|
| Transactions table | `001_init.sql:64-88` | Use directly - already has txn_id, account, booking_date, amount_gbp |
| Reconciliation state | `execution/reconcile/state.py` | Add `INVOICE_MISSING` state |
| Deduplication | `execution/adapters/amex_csv.py:277-299` | Reuse `compute_txn_id()` pattern |
| Matching algorithm | `execution/reconcile/match.py` | Adapt for transaction-first matching |
| FY filtering | `web/src/lib/fiscal.ts`, `execution/shared/fiscal.py` | Reuse directly |
| Page structure | `web/src/app/invoices/page.tsx` | Follow pattern with NuqsAdapter |

### New Components

#### 1. Statement Parser (`execution/statement/parser.py`)

**PDF extraction** using pdfplumber (already in dependencies):
- Extract tables from bank statement PDFs
- Return confidence score for extraction quality
- Reject with error if confidence < 70%

**CSV parsing** - extend existing adapter pattern:
- V1: Support predefined schemas (Amex, Wise, Monzo)
- Validate schema on upload, reject unknown formats with helpful error

**Decision from SpecFlow**: Require account selection on upload to ensure correct deduplication.

```python
class StatementParser:
    def parse(self, file_path: Path, account: str) -> ParseResult:
        """Parse statement file, return transactions with confidence."""
        if file_path.suffix.lower() == ".pdf":
            return self._parse_pdf(file_path, account)
        elif file_path.suffix.lower() == ".csv":
            return self._parse_csv(file_path, account)
        else:
            raise UnsupportedFormatError(f"Unsupported format: {file_path.suffix}")
```

#### 2. Transaction-First Matcher (`execution/reconcile/transaction_matcher.py`)

Existing `run_matcher()` is invoice-centric. Build transaction-first orchestration:

```python
def match_transactions(conn: Connection, fiscal_year: str) -> MatchResult:
    """Match unreconciled transactions against invoices."""
    # 1. Load unreconciled transactions for FY
    # 2. For each, find candidate invoices (amount, date, vendor)
    # 3. Score candidates using existing match.py logic
    # 4. Auto-match high confidence (≥0.85), surface uncertain (0.5-0.85)
```

**Matching thresholds** (from existing `match.py` patterns):
- Amount tolerance: 3% for FX variance
- Date window: ±7 days
- Vendor similarity: ≥0.85 token_set_ratio for auto-match, ≥0.5 for suggestion

#### 3. Reconciliation Page (`web/src/app/reconciliation/page.tsx`)

```
/reconciliation
├── page.tsx              # FY selector, upload button, content wrapper
├── reconciliation-content.tsx  # Main content with sections
├── upload-dialog.tsx     # File upload with account selector
├── transaction-list.tsx  # Paginated transaction list
├── match-review.tsx      # Review suggested matches
└── resolution-actions.tsx # Resolve/dismiss actions
```

**Page sections**:
1. **Summary stats**: Total transactions, matched, needs review, unmatched
2. **Auto-matched**: Collapsed by default, expand to review
3. **Needs review**: Suggested matches requiring confirmation
4. **Unmatched**: Transactions with no match candidates
5. **Resolved**: Filter to show dismissed/ignored transactions

#### 4. API Routes

```
POST /api/reconciliation/upload
  - Accepts FormData with file + account
  - Spawns CLI: granite reconcile upload --account <account> <file_path>
  - Returns: { txn_count, new_count, duplicate_count }

POST /api/reconciliation/resolve
  - Body: { txn_id, resolution: "matched" | "missing" | "personal" | "ignored", invoice_id? }
  - Updates reconciliation_rows state

GET /api/reconciliation/transactions
  - Query: fy, state, page
  - Returns paginated transactions with match suggestions
```

### Schema Changes

**Add state to existing enum** in `execution/reconcile/state.py`:

```python
class RowState(str, Enum):
    # ... existing states ...
    INVOICE_MISSING = "invoice_missing"  # NEW: flagged for follow-up
```

**Migration** (if needed): None - existing `state` column is TEXT, new enum value works directly.

### FX Handling

For uploaded statements with non-GBP transactions:
1. Extract original currency and amount from statement
2. Use `booking_date` to fetch ECB rate from `fx_rates` table
3. Convert to GBP on ingest (consistent with existing pipeline)
4. Store both original and GBP amounts

## System-Wide Impact

- **Interaction graph**: Upload → Parser → compute_txn_id → INSERT/dedupe → match_transactions → UI refresh
- **Error propagation**: Parser errors surface immediately in upload response. Matching errors logged but don't block.
- **State lifecycle**: Transactions persist permanently. Resolution states are user-editable. No orphan risk.
- **API surface parity**: New `/reconciliation` page is standalone. No changes to existing invoice/dashboard APIs.

## Acceptance Criteria

### From Requirements (see origin)
- [ ] Upload bank statement (PDF or CSV) and see extracted transactions
- [ ] Transactions auto-matched to invoices where confident
- [ ] Uncertain matches surfaced for manual review
- [ ] Can resolve transactions as: matched, missing, personal, ignored
- [ ] Running list maintained across multiple uploads
- [ ] Duplicate transactions detected and skipped
- [ ] FY filter works consistently with invoice pages

### From SpecFlow Analysis
- [ ] Account selection required on upload (for deduplication)
- [ ] PDF extraction shows confidence; rejects low-confidence extractions
- [ ] V1 supports only predefined CSV schemas with clear error for unsupported
- [ ] `INVOICE_MISSING` state exists and surfaces in action items
- [ ] Cross-FY statements handled gracefully (transactions assigned to their booking date FY)

## Implementation Phases

### Phase 1: Parser & Storage (Backend)
1. Create `execution/statement/parser.py` with PDF/CSV support
2. Add `INVOICE_MISSING` to state enum
3. Create CLI command: `granite reconcile upload --account <account> <file>`
4. Tests for parsing Amex/Wise/Monzo formats

### Phase 2: Transaction-First Matching
1. Create `execution/reconcile/transaction_matcher.py`
2. Adapt existing matching logic for transaction anchor
3. CLI command: `granite reconcile match --fy <fy>`
4. Tests for matching thresholds

### Phase 3: API Routes
1. `/api/reconciliation/upload` - file upload handler
2. `/api/reconciliation/transactions` - paginated list
3. `/api/reconciliation/resolve` - state changes

### Phase 4: UI
1. `/reconciliation` page structure
2. Upload dialog with account selector
3. Transaction list with sections (matched, review, unmatched)
4. Resolution actions (confirm, dismiss, flag missing)
5. FY selector integration

### Phase 5: Polish
1. Bulk actions (resolve multiple)
2. Search/filter within transactions
3. Export unresolved list

## Dependencies & Prerequisites

- pdfplumber (already in dependencies)
- Existing `transactions` table schema
- Existing FY utilities (`fiscal.py`, `fiscal.ts`)
- Existing matching algorithm (`match.py`)

## Success Metrics

- Upload-to-results latency < 5s for typical statement (100 transactions)
- Auto-match rate ≥80% for straightforward cases (same amount, similar date, matching vendor)
- Zero silent data loss (all extraction failures surfaced to user)

## Test Scenarios

1. Upload Amex CSV → transactions extracted, deduplicated, matched
2. Upload Wise PDF → table extracted, transactions stored
3. Upload unknown CSV format → clear error message
4. Upload statement with overlap → duplicates detected, not re-inserted
5. Cross-FY statement → transactions assigned to correct FY by booking date
6. Mark transaction as "invoice missing" → appears in action items
7. Low-confidence PDF extraction → user-facing error, no silent failure

## Scope Boundaries

**In scope (V1)**:
- PDF and CSV upload
- Predefined schemas (Amex, Wise, Monzo)
- Transaction-to-invoice matching
- Four resolution states
- Dedicated page with FY filtering

**Out of scope (V1)** (see origin):
- Direct API integrations (Wise, Monzo OAuth)
- Finding unpaid invoices (invoice-first reconciliation)
- Pipeline integration
- Custom CSV schema mapping UI
- Bulk import history / rollback

## Sources & References

### Origin
- **Origin document:** [docs/brainstorms/2026-04-19-bank-reconciliation-requirements.md](docs/brainstorms/2026-04-19-bank-reconciliation-requirements.md)
- Key decisions carried forward: PDF over APIs, hybrid matching, standalone page, FY scoped

### Internal References
- Transactions schema: `execution/shared/migrations/001_init.sql:64-88`
- Deduplication: `execution/adapters/amex_csv.py:277-299`
- Matching: `execution/reconcile/match.py`
- State machine: `execution/reconcile/state.py`
- FY utilities: `execution/shared/fiscal.py`, `web/src/lib/fiscal.ts`
- Page patterns: `web/src/app/invoices/page.tsx`

### Learnings Applied
- Interface boundary discipline from `docs/solutions/integration-issues/interface-mismatch-integration-testing.md`
- Schema validation pattern from Amex CSV adapter
- Agent-native output pattern for CLI commands
