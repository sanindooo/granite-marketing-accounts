---
title: "feat: Bank-Centric Reconciliation"
type: feat
status: active
date: 2026-05-07
origin: docs/brainstorms/2026-05-07-bank-centric-reconciliation-requirements.md
---

# Bank-Centric Reconciliation

## Summary

Build a bank-statement-anchored reconciliation workflow: upload statements, auto-match transactions to invoices and unprocessed emails, auto-process matching emails with inline invoices, flag gaps for bulk PDF upload resolution. Single scrolling `/reconciliation` page with FY scoping.

---

## Problem Frame

Monthly reconciliation currently requires reactive hunting — accountant software flags missing invoices after the fact, then manual searching through emails and vendor portals. The email pipeline already captures invoices automatically, but there's no way to start from the bank statement and systematically work through what's missing. (see origin: docs/brainstorms/2026-05-07-bank-centric-reconciliation-requirements.md)

---

## Requirements

- R1. Upload bank statements as PDF or CSV
- R2. Extract transactions: date, description, amount, currency
- R3. Deduplicate transactions across overlapping statement uploads
- R4. Support predefined schemas: Amex (PDF), Wise (PDF), Tide (PDF), Monzo (CSV)
- R5. For non-GBP transactions, convert to GBP using the transaction date's exchange rate
- R6. Match transactions to captured invoices by GBP amount (with tolerance for FX variance), date proximity, and vendor name similarity
- R7. When a match is found in an unprocessed email with an inline invoice, auto-process the email and link the resulting invoice to the transaction
- R8. When a match is found in an unprocessed email that requires manual download (third-party link), flag the transaction as "needs manual download"
- R9. When no match is found, flag the transaction as "missing invoice"
- R10. Upload multiple invoice files at once (PDFs)
- R12. Auto-match uploaded invoices to flagged transactions using the same matching logic
- R13. Filter transactions by vendor, status (matched, needs download, missing), and date range
- R14. Manually link an invoice to a transaction when auto-match fails
- R15. Mark a transaction as "no invoice needed" (personal expense, transfer, etc.)
- R16. Scope all views by fiscal year (Mar 1 - Feb 28/29)
- R17. Dedicated `/reconciliation` page separate from dashboard and inbox

**Origin actors:** A1 (Stephen), A2 (System/reconciliation engine)
**Origin flows:** F1 (Monthly reconciliation batch), F2 (Bulk upload resolution), F3 (Edge case resolution)
**Origin acceptance examples:** AE1 (covers R6, R7), AE2 (covers R6, R8), AE3 (covers R9, R12), AE5 (covers R3), AE6 (covers R5, R6)

---

## Scope Boundaries

- Direct bank API integrations (PDF/CSV upload only for V1)
- Real-time email monitoring or webhooks
- Invoice-first reconciliation ("which invoices have no transaction")
- Custom CSV schema mapping UI (predefined schemas only for V1)
- Automated statement fetching

### Deferred to Follow-Up Work

- **R11. OCR support for iPhone photos and scanned receipts**: Deferred until first reconciliation pass reveals gap composition. If Kenya/Uganda paper receipts dominate the "missing" list, that's the signal to add OCR.
- **AE4** (iPhone photo test): Deferred with R11

---

## Context & Research

### Relevant Code and Patterns

| Component | Path | Reuse Strategy |
|-----------|------|----------------|
| Transactions table | `execution/shared/migrations/001_init.sql` | Use directly — has txn_id, account, booking_date, amount_gbp |
| Reconciliation state | `execution/reconcile/state.py` | Use existing `UNMATCHED` state for missing invoices; add `needs_manual_download` column to transactions table |
| Deduplication | `execution/adapters/amex_csv.py` | Reuse `compute_txn_id()` pattern (SHA-256 of account+date+desc+amount) |
| LLM extraction | `execution/shared/llm.py` | Existing Claude client pattern for PDF text → structured JSON |
| Prompt templates | `execution/shared/prompts/` | Existing prompt template pattern |
| Matching algorithm | `execution/reconcile/match.py` | Extend for transaction-first matching against invoices AND emails |
| FX conversion | `execution/shared/fx.py` | Use `get_rate_to_gbp()` for multi-currency statements |
| PDF extraction | `execution/invoice/processor.py` | Uses pdfplumber — same approach for statement tables |
| UI patterns | `web/src/app/inbox/inbox-content.tsx` | Filters, selection, bulk actions, SSE streaming |
| CLI streaming | `web/src/app/api/pipeline/stream/route.ts` | Spawn CLI, stream SSE events |

### Institutional Learnings

- **FX API**: Use `api.frankfurter.dev/v1/` with `follow_redirects=True` (see `docs/solutions/integration-issues/fx-api-redirect-and-dashboard-fixes.md`)
- **List query performance**: Project only display columns to avoid SQLite queue backup; use `TransactionListRow` type (see `docs/solutions/integration-issues/invoice-export-and-link-pdf-fixes.md`)
- **Thread safety**: If using ThreadPoolExecutor for parallel matching, HTTP clients and SQLite connections must be thread-local (see `docs/solutions/runtime-errors/ms365-thread-safe-http-client.md`)
- **LIKE escaping**: Escape `%`, `_`, `\` when fuzzy-matching vendor names (see `docs/solutions/integration-issues/interface-mismatch-integration-testing.md`)
- **CLI-first**: Add CLI commands before building UI — any action a user can take, an agent must be able to take via CLI

### External References

- pdfplumber table extraction: already proven in invoice pipeline
- Existing matching thresholds: 0.93 auto-match, 0.70 suggested (from `match.py`)

---

## Key Technical Decisions

- **Bank statement is source of truth**: Transactions anchor the workflow; emails and invoices are documentation sources
- **Auto-process matching emails**: When system finds an unprocessed email matching a transaction with an inline invoice, it processes the email automatically (R7)
- **Transaction-first matching**: Match against `invoices` table (full scoring: amount, date, vendor). For emails, match by sender domain vs transaction vendor + date proximity only (emails lack stored amounts) — email matches are candidates for auto-processing, not direct reconciliation
- **Deduplication via txn_id**: SHA-256 of (account, date, canonical_description, amount) — same pattern as Amex adapter
- **Multi-currency via GBP normalization**: All amounts converted to GBP using transaction-date FX rates; matching uses GBP amounts with 3% tolerance
- **PDF via text extraction + LLM**: Bank statement PDFs use narrative format (not tables), so extraction uses pdfplumber text extraction → Claude parsing → JSON transactions. Same pattern as invoice extraction. CSV supported where available (Monzo)
- **Single scrolling page**: Reconciliation page uses sections (Summary stats, Suggested matches, Needs attention, Auto-matched, Resolved) rather than tabs — matches inbox pattern

---

## Open Questions

### Resolved During Planning

- **PDF extraction approach**: pdfplumber text extraction + Claude haiku parsing (tested against real Amex/Wise/Tide statements — table extraction doesn't work, but text extraction + LLM works well)
- **Matching thresholds**: Use existing 0.93/0.70 from `match.py`
- **Deduplication strategy**: SHA-256 of (account, date, description, amount) — follows Amex adapter
- **Page layout**: Single scrolling view with sections (like inbox)
- **OCR provider**: Deferred — extraction layer will be pluggable
- **Providers**: Amex (PDF), Wise (PDF, multi-currency), Tide (PDF), Monzo (CSV)

### Deferred to Implementation

- **Statement format edge cases**: Exact table structure varies by bank; will need tuning during testing
- **Email matching confidence**: May need different thresholds for email-to-transaction vs invoice-to-transaction

---

## Implementation Units

### U1. Statement Parser

**Goal:** Parse bank statements (PDF or CSV) into normalized transaction records using text extraction + LLM parsing.

**Requirements:** R1, R2, R4

**Dependencies:** None

**Files:**
- Create: `execution/statement/parser.py`
- Create: `execution/statement/__init__.py`
- Create: `execution/statement/prompts/extract_transactions.md` (LLM prompt template)
- Test: `tests/statement/test_parser.py`

**Approach:**
- **PDF extraction path** (Amex, Wise, Tide):
  1. Use pdfplumber to extract text from all pages (not table extraction — statements are narrative format)
  2. Pass extracted text to Claude (haiku for cost efficiency) with provider-specific prompt
  3. LLM returns structured JSON: `[{date, description, amount, currency, balance?}, ...]`
  4. Validate LLM output against expected schema
  5. Cost: ~$0.02-0.10 per statement depending on page count
- **CSV extraction path** (Monzo):
  1. Direct parsing with schema validation (no LLM needed)
  2. Reuse patterns from `execution/adapters/amex_csv.py`
- Require account selection on upload for correct extraction path and deduplication
- Return `ParseResult` with transactions and confidence score
- Support `--mock` flag for testing without LLM API cost

**Technical design:**
```
parse_statement(file: Path, account: str) -> ParseResult:
    if file.suffix == ".pdf":
        text = extract_text_with_pdfplumber(file)
        prompt = load_prompt(f"extract_transactions_{account}.md")
        response = call_claude_haiku(prompt, text)
        transactions = validate_and_parse(response)
    elif file.suffix == ".csv":
        transactions = parse_csv(file, account)
    return ParseResult(transactions, confidence)
```

**Patterns to follow:**
- `execution/invoice/processor.py` for pdfplumber text extraction
- `execution/shared/prompts/` for prompt template pattern
- `execution/adapters/amex_csv.py` for CSV schema validation and `compute_txn_id()` logic
- Existing Claude client pattern in `execution/shared/llm.py`

**Test scenarios:**
- Happy path: Parse Amex PDF → LLM extracts transactions with correct date/amount/description
- Happy path: Parse Wise PDF (GBP) → extracts transactions including running balance
- Happy path: Parse Wise PDF (USD) → extracts transactions with original currency
- Happy path: Parse Tide PDF → extracts transactions with paid in/out columns
- Happy path: Parse Monzo CSV → direct parsing, no LLM call
- Edge case: Multi-page PDF (18 pages) → all pages concatenated and parsed
- Edge case: Foreign currency transactions with FX details → amount extracted correctly
- Error path: Corrupt/unreadable PDF → raises `ExtractionError` with details
- Error path: LLM returns malformed JSON → retry once, then surface error
- Error path: Unknown account type → raises `UnsupportedAccountError` with supported list

**Verification:**
- Can parse sample statements from all four providers (Amex, Wise, Tide, Monzo)
- PDF extraction uses text + LLM, not table detection
- Mock mode works without incurring API costs

---

### U2. Transaction Deduplication and Storage

**Goal:** Store extracted transactions with deduplication across overlapping statement uploads.

**Requirements:** R3, R5

**Dependencies:** U1

**Files:**
- Create: `execution/statement/store.py`
- Create: `execution/shared/migrations/0XX_add_needs_manual_download_to_transactions.sql` (add column)
- Test: `tests/statement/test_store.py`

**Approach:**
- Compute stable `txn_id` using SHA-256 of (account, booking_date, canonical_description, amount_original)
- Canonical description: uppercase, strip trailing reference numbers and location codes
- For non-GBP transactions: fetch FX rate via `get_rate_to_gbp()`, store both original and GBP amounts
- INSERT OR IGNORE pattern for deduplication
- Use existing `UNMATCHED` state from `state.py` for transactions with no invoice match (maps to "missing invoice")
- Add `needs_manual_download BOOLEAN DEFAULT FALSE` column to transactions table for emails requiring portal download

**Patterns to follow:**
- `execution/adapters/amex_csv.py:compute_txn_id()` for stable ID generation
- `execution/shared/fx.py` for FX conversion

**Test scenarios:**
- Happy path: Store new transactions → all inserted with correct txn_id
- Happy path: Store USD transaction → FX rate fetched, amount_gbp populated
- Covers AE5: Upload April statement with 5 overlapping March transactions → those 5 skipped, only new transactions added
- Edge case: FX rate not in cache → fetches from API, caches, then converts
- Edge case: Weekend transaction date → FX falls back to previous working day rate
- Error path: FX API unavailable → stores original amount, flags for later backfill

**Verification:**
- Duplicate transactions across uploads are not re-inserted
- FX conversion uses transaction date, not upload date

---

### U3. Transaction-to-Invoice/Email Matcher

**Goal:** Match transactions against both captured invoices AND unprocessed emails.

**Requirements:** R6, R8, R9

**Dependencies:** U2

**Files:**
- Create: `execution/reconcile/transaction_matcher.py`
- Modify: `execution/reconcile/match.py` (extend to search emails table)
- Test: `tests/reconcile/test_transaction_matcher.py`

**Approach:**
- For each unreconciled transaction in FY:
  1. Search `invoices` table for candidates (amount within 3% tolerance, date within ±7 days, vendor similarity ≥0.5)
  2. Score invoice candidates using existing weighted algorithm (vendor 50%, amount 35%, currency 10%, date 5%)
  3. Auto-match if score ≥0.93, suggest if 0.70-0.93
  4. If no invoice match ≥0.70: search `emails` table for unprocessed emails by sender domain vs transaction vendor + date proximity (emails lack stored amounts, so no amount scoring)
  5. For email matches: classify as "inline invoice" vs "third-party link" based on attachment type
  6. If no candidates found → flag as `UNMATCHED`
- Email matches are candidates for auto-processing (U4), not direct reconciliation — the email must be processed first to extract an invoice with amount data
- If email has third-party link → set `needs_manual_download` flag on transaction (column, not state)

**Patterns to follow:**
- `execution/reconcile/match.py` for scoring algorithm
- `execution/adapters/ms365.py` for email classification patterns

**Test scenarios:**
- Covers AE6: USD transaction $50 + invoice $50 → converts both to GBP, matches with FX tolerance
- Happy path: Transaction matches invoice at 0.95 → auto-matched, linked
- Happy path: Transaction matches unprocessed email with PDF attachment → flagged for auto-process (U4)
- Covers AE2: Transaction matches email with download link → state set to `NEEDS_DOWNLOAD`
- Edge case: Transaction matches multiple candidates → highest score wins, others surfaced as alternatives
- Edge case: Amount matches but vendor differs significantly → suggested match (0.70-0.93), not auto
- Error path: No candidates found → state set to `UNMATCHED`

**Verification:**
- Matching searches invoices table (full scoring) and emails table (sender + date only)
- Third-party link emails correctly set `needs_manual_download` flag

---

### U4. Email Auto-Process Trigger

**Goal:** When a transaction matches an unprocessed email with an inline invoice, auto-process that email.

**Requirements:** R7

**Dependencies:** U3

**Files:**
- Modify: `execution/reconcile/transaction_matcher.py` (add auto-process trigger)
- Test: `tests/reconcile/test_auto_process.py`

**Approach:**
- After matching identifies an unprocessed email with inline invoice (PDF/image attachment):
  1. Invoke existing `granite process` logic for that email
  2. On success: link resulting invoice to the transaction, set state to `AUTO_MATCHED`
  3. On failure: log error, flag transaction for manual review
- Use existing email processing infrastructure — no new extraction code

**Patterns to follow:**
- `execution/invoice/processor.py` for email processing flow
- `execution/cli.py` for invoking processing programmatically

**Test scenarios:**
- Covers AE1: Transaction £49.99 "ANTHROPIC" + unprocessed email from anthropic.com with PDF → email auto-processed, invoice linked
- Happy path: Auto-process succeeds → transaction state is `AUTO_MATCHED`, invoice linked
- Error path: Email processing fails (corrupt PDF) → transaction flagged for manual review, error logged
- Edge case: Email already processed by another path → skip re-processing, just link existing invoice

**Verification:**
- Matching emails with inline invoices trigger automatic processing
- Processing failures don't crash the reconciliation — they flag for manual review

---

### U5. Bulk Upload for PDFs

**Goal:** Upload multiple invoice PDFs at once to resolve flagged transactions.

**Requirements:** R10, R12

**Dependencies:** U3

**Files:**
- Create: `execution/statement/bulk_upload.py`
- Test: `tests/statement/test_bulk_upload.py`

**Approach:**
- Accept multiple PDF files in single upload
- For each PDF:
  1. Extract invoice data via existing `execution/invoice/processor.py` pipeline directly (no abstraction layer)
  2. Match against flagged transactions (`UNMATCHED` state or `needs_manual_download=true`) using same matching logic
  3. On match: link invoice to transaction, update state to `USER_VERIFIED`
- Stream progress events via stderr for SSE consumption
- If OCR is greenlit in follow-up work, introduce abstraction at that point when there are two implementations

**Patterns to follow:**
- `execution/invoice/processor.py` for PDF extraction (call directly)
- `execution/reconcile/match.py` for matching logic
- Generator-of-batches for memory efficiency with many files

**Test scenarios:**
- Covers AE3: Bulk upload Figma invoice £35 → matches flagged transaction, moves to reconciled
- Happy path: Upload 5 PDFs → 4 match flagged transactions, 1 has no match (stored as unlinked invoice)
- Edge case: Duplicate invoice (already uploaded) → skipped, not re-processed
- Edge case: Invoice matches multiple flagged transactions → best match wins, others remain flagged
- Error path: Corrupt PDF → error logged, other files continue processing
- Integration: Progress events emitted for each file → SSE stream shows progress

**Verification:**
- Multiple PDFs processed in single operation
- Matched invoices link to transactions and update state

---

### U6. Reconciliation CLI Commands

**Goal:** CLI commands for all reconciliation operations (agent-native, UI-independent).

**Requirements:** R1, R6, R10, R13, R14, R15

**Dependencies:** U1, U2, U3, U5

**Files:**
- Modify: `execution/cli.py` (add reconcile subcommands)
- Test: `tests/cli/test_reconcile_commands.py`

**Approach:**
- `granite reconcile upload --account <account> <file>` — parse statement, store transactions, run matching
- `granite reconcile match --fy <fy>` — run matching for all unreconciled transactions in FY
- `granite reconcile bulk-upload <file1> [file2...]` — bulk upload PDFs
- `granite reconcile resolve <txn_id> --state <state> [--invoice-id <id>]` — manual resolution
- `granite reconcile list --fy <fy> [--state <state>] [--vendor <vendor>]` — list transactions
- All commands output agent-native JSON, emit progress events to stderr

**Patterns to follow:**
- Existing CLI commands in `execution/cli.py`
- `emit_success`/`emit_error` for agent-native output
- Progress events via stderr for SSE streaming

**Test scenarios:**
- Happy path: `granite reconcile upload --account amex statement.csv` → outputs JSON with txn_count, new_count, matched_count
- Happy path: `granite reconcile list --fy 2025-26 --state missing` → outputs JSON array of transactions
- Happy path: `granite reconcile resolve TXN123 --state personal` → updates state, outputs success JSON
- Error path: Unknown account → exits 1 with error JSON
- Error path: Invalid file format → exits 1 with helpful error message

**Verification:**
- All reconciliation operations available via CLI
- Output is agent-native JSON suitable for automation

---

### U7. Reconciliation API Routes

**Goal:** API routes that spawn CLI commands and stream results to the web UI.

**Requirements:** R17

**Dependencies:** U6

**Files:**
- Create: `web/src/app/api/reconciliation/upload/route.ts`
- Create: `web/src/app/api/reconciliation/resolve/route.ts`
- Create: `web/src/lib/actions/reconciliation.ts`
- Create: `web/src/lib/queries/reconciliation.ts`
- Test: `web/src/lib/queries/__tests__/reconciliation.test.ts`

**Approach:**
- `POST /api/reconciliation/upload` — accepts FormData, spawns `granite reconcile upload`, streams SSE
- `POST /api/reconciliation/resolve` — spawns `granite reconcile resolve`, returns JSON
- Server actions for mutations, query functions for reads
- Query functions project only list columns (`TransactionListRow`) to avoid SQLite queue backup

**Patterns to follow:**
- `web/src/app/api/pipeline/stream/route.ts` for CLI spawning and SSE streaming
- `web/src/lib/queries/inbox.ts` for query patterns with FY filtering
- `web/src/lib/actions/inbox.ts` for mutation patterns

**Test scenarios:**
- Happy path: POST upload with valid file → SSE stream shows progress, final result has counts
- Happy path: Query transactions with FY filter via server action → returns paginated list matching filter
- Error path: Upload invalid file → SSE stream ends with error event
- Integration: Upload triggers CLI spawn → CLI progress events stream to browser

**Verification:**
- API routes spawn CLI commands (not direct DB writes from web layer)
- Streaming works for long-running uploads

---

### U8. Reconciliation Page

**Goal:** Dedicated `/reconciliation` page with upload, filtering, and resolution actions.

**Requirements:** R13, R14, R15, R16, R17

**Dependencies:** U7

**Files:**
- Create: `web/src/app/reconciliation/page.tsx`
- Create: `web/src/app/reconciliation/reconciliation-content.tsx`
- Create: `web/src/app/reconciliation/upload-dialog.tsx`
- Create: `web/src/app/reconciliation/transaction-list.tsx`
- Create: `web/src/app/reconciliation/resolution-actions.tsx`
- Create: `web/src/app/reconciliation/bulk-upload-dialog.tsx`
- Create: `web/src/app/reconciliation/link-invoice-dialog.tsx`
- Test: Manual testing via dev server

**Approach:**

**Page sections (single scrolling view):**
1. **Summary stats** — Total transactions, matched count, needs attention count, resolved count
2. **Suggested matches** — Transactions with 0.70-0.93 match score; shows transaction + suggested invoice side-by-side with "Confirm" / "Reject" buttons
3. **Needs attention** — Transactions with `UNMATCHED` state or `needs_manual_download=true`; grouped by status badge
4. **Auto-matched** — Transactions with `AUTO_MATCHED` state; collapsed by default, expandable to review
5. **Resolved** — Transactions with `USER_VERIFIED`, `USER_PERSONAL`, or `USER_IGNORE` states

**Upload dialog (`upload-dialog.tsx`):**
- File picker (accepts .pdf or .csv) + account selector dropdown (Amex/Wise/Tide/Monzo)
- Progress state: shows spinner + "Extracting transactions..." during PDF/CSV processing
- For PDFs: shows "Analyzing statement with AI..." during LLM extraction
- Success state: dialog closes, transactions appear in list, toast shows "Added X transactions (Y new, Z duplicates skipped)"
- Error states:
  - Unsupported file type → inline error "Please upload a PDF or CSV bank statement."
  - Unknown account type → inline error "Unrecognized account. Supported: Amex, Wise, Tide, Monzo."
  - LLM extraction failed → inline error "Could not extract transactions. Please check the file is a valid statement."
  - Parse error → inline error with details, dialog stays open for retry

**Bulk upload dialog (`bulk-upload-dialog.tsx`):**
- Multi-file picker for PDFs
- Progress state: shows per-file list with status icons (spinner → checkmark/X)
- Completion state with mixed results: "4 matched, 1 failed" summary; failed files show error reason; "Done" button to close
- Partial failure: dialog stays open showing results, user can retry failed files or dismiss

**Manual invoice linking (`link-invoice-dialog.tsx`):**
- Triggered by "Link invoice" button on transaction row
- Opens dialog with:
  - Search field to query existing invoices by vendor/amount/date
  - Results list showing matching invoices with preview (vendor, amount, date)
  - "Upload new" button to upload a PDF if no existing invoice matches
- On selection: immediately links invoice to transaction, updates state to `USER_VERIFIED`, closes dialog

**State-to-action availability:**

| State | Link Invoice | Mark Personal | Mark No Invoice Needed |
|-------|-------------|---------------|------------------------|
| SUGGESTED | ✓ (override suggestion) | ✓ | ✓ |
| UNMATCHED | ✓ | ✓ | ✓ |
| needs_manual_download | ✓ | ✓ | ✓ |
| AUTO_MATCHED | ✓ (override) | — | — |
| USER_VERIFIED | ✓ (re-link) | — | — |
| USER_PERSONAL | — | — | — (already set) |
| USER_IGNORE | — | — | — (already set) |

**Other UI details:**
- FY selector in header (reuse existing pattern)
- Transaction list with filters: vendor search, status filter, date range
- Use `nuqs` for URL query state, `usePipelineStream` for SSE streaming

**Patterns to follow:**
- `web/src/app/inbox/inbox-content.tsx` for list layout, filters, bulk actions
- `web/src/app/inbox/filter-bar.tsx` for filter UI
- `web/src/components/ui/` for shadcn components

**Test scenarios:**
- Happy path: Navigate to /reconciliation → page loads with FY selector and upload button
- Happy path: Upload statement → progress shown, transactions appear in list
- Happy path: Filter by "missing" status → only UNMATCHED transactions shown
- Happy path: Click "Confirm" on suggested match → state updates to USER_VERIFIED, moves to Resolved
- Happy path: Click "Link invoice" on UNMATCHED transaction → dialog opens, search works, selection links invoice
- Happy path: Click "mark as personal" on transaction → state updates, moves to Resolved section
- Happy path: Bulk upload PDFs → per-file progress shown, matched transactions move to Resolved
- Error path: Upload unsupported file → inline error in dialog, dialog stays open
- Error path: Bulk upload with 2 corrupt PDFs → shows "3 matched, 2 failed" summary with error details
- Edge case: No transactions for FY → empty state with helpful message
- Integration: FY filter matches dashboard/inbox behavior

**Verification:**
- All transaction states visible and filterable
- Suggested matches have Confirm/Reject affordances
- Manual invoice linking works via search or upload
- Resolution actions respect state-to-action matrix
- Upload and bulk upload show progress and handle errors gracefully

---

## System-Wide Impact

- **Interaction graph:** Upload PDF/CSV → Parser (PDF: pdfplumber text → Claude haiku → JSON; CSV: direct parse) → compute_txn_id → INSERT/dedupe → match_transactions (invoices by score, emails by sender+date) → auto-process matching emails → UI refresh
- **Error propagation:** Parser errors surface immediately in upload dialog (inline, dialog stays open). Matching errors logged but don't block. Email auto-process failures flag transaction for manual review.
- **State lifecycle:** Transactions persist permanently. Uses existing `UNMATCHED` state plus new `needs_manual_download` column. Resolution states are user-editable. No orphan risk — transactions exist independently of invoices.
- **API surface parity:** New `/reconciliation` page is standalone. No changes to existing invoice/dashboard/inbox APIs.
- **Integration coverage:** U4 (auto-process) crosses email processing pipeline — needs integration test confirming email → invoice → transaction link works end-to-end.
- **Unchanged invariants:** Existing invoice capture pipeline continues to function independently. Dashboard metrics unchanged. Inbox page unchanged. Existing 9-state machine in `state.py` unchanged (no new states added).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| LLM extraction returns incorrect data | Validate LLM output against expected schema; show extraction preview before committing; `--mock` mode for testing |
| Bank changes PDF format | LLM-based extraction is format-flexible; only prompt tuning needed, not code changes |
| LLM API cost per statement | Use claude-haiku (~$0.02-0.10 per statement); acceptable for monthly batch workflow |
| FX rate API unavailable during upload | Store original amount, flag for later backfill; existing `backfillFx` pattern |
| Email auto-processing may fail for edge cases | Failures flag for manual review rather than crashing; existing processing is proven |
| Large statement uploads (1000+ transactions) | Batch processing with progress events; generator-of-batches pattern |
| Suggested matches (0.70-0.93) may have low precision | UI shows transaction + invoice side-by-side for manual confirmation; user can reject false positives |

---

## Documentation / Operational Notes

- Add `/reconciliation` to navigation after implementation
- Document supported statement formats: Amex PDF, Wise PDF (all currencies), Tide PDF, Monzo CSV
- Monthly workflow: upload statements after month end, review "needs attention" section, bulk upload missing PDFs
- LLM extraction cost: ~$0.02-0.10 per statement (claude-haiku)

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-07-bank-centric-reconciliation-requirements.md](docs/brainstorms/2026-05-07-bank-centric-reconciliation-requirements.md)
- **Prior plan:** [docs/plans/2026-04-19-003-feat-bank-reconciliation-plan.md](docs/plans/2026-04-19-003-feat-bank-reconciliation-plan.md) (covers basic statement upload; this plan extends with email auto-processing, bulk upload)
- Related code: `execution/reconcile/match.py`, `execution/reconcile/state.py`, `execution/adapters/amex_csv.py`
- Learnings applied: `docs/solutions/integration-issues/fx-api-redirect-and-dashboard-fixes.md`, `docs/solutions/integration-issues/invoice-export-and-link-pdf-fixes.md`
