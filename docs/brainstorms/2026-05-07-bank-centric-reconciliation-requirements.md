---
date: 2026-05-07
topic: bank-centric-reconciliation
---

# Bank-Centric Reconciliation

## Summary

Upload bank statements, auto-match transactions to invoices from the email pipeline, flag gaps for bulk upload resolution. Monthly batch workflow that turns hours of reactive hunting into minutes of proactive reconciliation.

---

## Problem Frame

Business expenses happen via bank transactions. Invoices arrive via email, third-party portals, or physical receipts. Today, reconciling these requires reactive hunting: accountant software flags missing invoices after the fact, then Stephen manually searches emails, logs into vendor portals, and tracks what's been found versus what's still missing. This takes hours spread across multiple days.

The email pipeline already captures invoices automatically. The inbox page shows what's been processed. But there's no way to start from the bank statement and systematically work through what's missing — no single view that says "here are your transactions, here's what's matched, here's what you still need to find."

---

## Actors

- A1. Stephen (primary user): Uploads statements, reviews matches, bulk-uploads missing invoices, resolves edge cases
- A2. System (reconciliation engine): Extracts transactions, matches to invoices/emails, flags gaps, processes matched emails

---

## Key Flows

- F1. Monthly reconciliation batch
  - **Trigger:** Stephen uploads a bank statement (PDF or CSV)
  - **Actors:** A1, A2
  - **Steps:**
    1. System extracts transactions from statement
    2. System deduplicates against already-imported transactions
    3. For each new transaction, system searches for matching invoices (captured) and emails (unprocessed)
    4. High-confidence matches auto-link; emails with inline invoices auto-process
    5. Emails with third-party links flagged as "needs manual download"
    6. Transactions with no match flagged as "missing invoice"
    7. Stephen reviews the summary: matched, needs download, missing
  - **Outcome:** Clear picture of what's reconciled vs. what needs attention
  - **Covered by:** R1, R2, R3, R4, R5, R6, R7

- F2. Bulk upload resolution
  - **Trigger:** Stephen has downloaded invoices from vendor portals
  - **Actors:** A1, A2
  - **Steps:**
    1. Stephen uploads multiple invoice files (PDFs, photos)
    2. System extracts invoice data (OCR for photos)
    3. System matches uploaded invoices to flagged transactions
    4. Matched transactions move to reconciled state
  - **Outcome:** Flagged transactions resolved without one-by-one manual linking
  - **Covered by:** R10, R11, R12

- F3. Edge case resolution
  - **Trigger:** Transactions remain unmatched after bulk upload
  - **Actors:** A1
  - **Steps:**
    1. Stephen filters by vendor to find specific gaps
    2. Stephen manually links invoice to transaction, or marks as "no invoice needed"
  - **Outcome:** All transactions resolved
  - **Covered by:** R13, R14, R15

---

## Requirements

**Statement upload and extraction**
- R1. Upload bank statements as PDF or CSV
- R2. Extract transactions: date, description, amount, currency
- R3. Deduplicate transactions across overlapping statement uploads
- R4. Support predefined schemas: Amex, Wise, Monzo (V1)
- R5. For non-GBP transactions, convert to GBP using the transaction date's exchange rate from the existing FX infrastructure

**Auto-matching**
- R6. Match transactions to captured invoices by GBP amount (with tolerance for FX variance), date proximity, and vendor name similarity
- R7. When a match is found in an unprocessed email with an inline invoice, auto-process the email and link the resulting invoice to the transaction
- R8. When a match is found in an unprocessed email that requires manual download (third-party link), flag the transaction as "needs manual download"
- R9. When no match is found, flag the transaction as "missing invoice"

**Bulk upload**
- R10. Upload multiple invoice files at once (PDFs, images)
- R11. OCR support for iPhone photos and scanned receipts
- R12. Auto-match uploaded invoices to flagged transactions using the same matching logic

**Resolution and filtering**
- R13. Filter transactions by vendor, status (matched, needs download, missing), and date range
- R14. Manually link an invoice to a transaction when auto-match fails
- R15. Mark a transaction as "no invoice needed" (personal expense, transfer, etc.)
- R16. Scope all views by fiscal year (Mar 1 - Feb 28/29)

**Page and navigation**
- R17. Dedicated `/reconciliation` page separate from dashboard and inbox

---

## Acceptance Examples

- AE1. **Covers R6, R7.** Given a bank transaction for £49.99 on 2026-04-15 from "ANTHROPIC", and an unprocessed email from anthropic.com received 2026-04-14 with a PDF attachment, when reconciliation runs, then the email is auto-processed and the resulting invoice is linked to the transaction.

- AE2. **Covers R6, R8.** Given a bank transaction for £120.00 on 2026-04-10 from "RAILWAY", and an unprocessed email from railway.app received 2026-04-10 containing a link to download the invoice, when reconciliation runs, then the transaction is flagged as "needs manual download" with reference to the email.

- AE3. **Covers R9, R12.** Given a bank transaction for £35.00 on 2026-04-20 from "FIGMA" with no matching email, when Stephen bulk-uploads a Figma invoice PDF for £35.00 dated 2026-04-19, then the invoice is auto-matched and the transaction moves to reconciled.

- AE4. **Covers R11.** Given Stephen uploads an iPhone photo of a paper receipt for £12.50, when the system processes it, then the receipt text is extracted via OCR and matched to a transaction.

- AE5. **Covers R3.** Given transactions from March statement already imported, when Stephen uploads April statement with 5 overlapping March transactions, then those 5 are skipped (not duplicated) and only new April transactions are added.

- AE6. **Covers R5, R6.** Given a Wise USD transaction for $50.00 on 2026-04-12 and an invoice for $50.00, when reconciliation runs, then the transaction is converted to GBP using the 2026-04-12 exchange rate and matched to the invoice (with FX tolerance).

---

## Success Criteria

- Monthly reconciliation takes minutes, not hours
- Stephen can see at a glance: how many transactions, how many matched, how many need attention
- Bulk upload resolves 80%+ of missing invoices without manual one-to-one linking
- Zero silent data loss — every extraction failure surfaces to the user

---

## Scope Boundaries

- Direct bank API integrations (PDF/CSV upload only for V1)
- Email-as-source-of-truth view (bank statement anchors everything)
- Real-time email monitoring or webhooks
- Invoice-first reconciliation ("which invoices have no transaction")
- Custom CSV schema mapping UI (predefined schemas only for V1)
- Automated statement fetching

---

## Key Decisions

- **Bank statement is source of truth**: Transactions anchor the workflow; emails and uploads are invoice sources
- **Auto-process matching emails**: When the system finds an unprocessed email matching a transaction, it processes the email automatically rather than just surfacing the match
- **Bulk upload with smart matching**: Uploaded invoices auto-match to flagged transactions using the same matching logic as email-to-transaction matching
- **OCR for photos**: Support iPhone photos and scanned receipts via OCR extraction
- **Reuse existing schema**: Build on `transactions`, `reconciliation_rows`, `reconciliation_links` tables
- **Multi-currency via GBP normalization**: All amounts converted to GBP using transaction-date FX rates; matching uses GBP amounts with tolerance for rate variance

---

## Dependencies / Assumptions

- Bank statements contain: date, description, amount, currency (minimum extractable fields)
- PDF statements are text-based or OCR-able
- Invoices have vendor name, amount, and date for matching
- Existing email pipeline and inbox infrastructure continues to function independently
- Existing `fx_rates` table and FX conversion infrastructure available for multi-currency support
- Wise statements may contain multiple currencies (USD, EUR, etc.) in a single statement

---

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Technical] PDF table extraction approach — pdfplumber, camelot, or hybrid?
- [Affects R5][Technical] Matching algorithm thresholds — what scores trigger auto-match vs. uncertain vs. no-match?
- [Affects R3][Technical] Transaction deduplication strategy — hash of date+amount+description, or provider-specific IDs?
- [Affects R10][Technical] OCR service selection — local (tesseract), cloud (Google Vision), or existing pipeline?
- [Affects R16][Needs research] Page layout — single scrolling view with sections, or tabbed interface?
