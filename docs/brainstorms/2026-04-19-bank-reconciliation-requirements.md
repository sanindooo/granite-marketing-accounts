---
date: 2026-04-19
topic: bank-reconciliation
---

# Bank Statement Reconciliation

## Problem Frame

Business expenses occur via bank transactions, but not every transaction has a captured invoice. Currently there's no systematic way to identify which transactions are missing invoices - this creates gaps in financial records and potential tax issues.

This feature surfaces transactions that need attention by matching bank statements against captured invoices.

## Requirements

- R1. Upload bank statements (PDF or CSV) from any bank
- R2. Extract transactions using a dedicated bank statement parser (not the invoice extraction pipeline)
- R3. Auto-match high-confidence transaction-invoice pairs based on amount, date proximity, and vendor name similarity
- R4. Surface uncertain matches for manual review
- R5. Unmatched transactions can be resolved as:
  - Matched to invoice (link to existing)
  - Invoice missing (flag as action item)
  - No invoice needed (personal, transfer, etc.)
  - Ignored (duplicate, wrong account, not relevant)
- R6. Maintain running list of unreconciled transactions across multiple statement uploads
- R7. Deduplicate transactions when uploading overlapping statement periods
- R8. Scope by fiscal year (Mar 1 - Feb 28/29), consistent with invoice views
- R9. Dedicated `/reconciliation` page with specialized UI (not part of existing dashboard or Needs Attention)

## Success Criteria

- Can upload a bank statement and immediately see which transactions have no matching invoice
- Transactions flagged as "invoice missing" create clear action items
- Auto-matching correctly handles 80%+ of straightforward cases (same amount, similar date, matching vendor)
- FY filter works consistently with invoice pages

## Scope Boundaries

- **In scope**: Transaction extraction, matching, resolution workflow, dedicated page
- **Out of scope (V1)**:
  - Direct API integrations (Wise, Monzo) - PDF/CSV upload only
  - Finding unpaid invoices (invoices without matching transactions)
  - Pipeline integration - standalone page only
  - Automated statement fetching

## Key Decisions

- **PDF over APIs**: Support any bank via PDF upload rather than building specific integrations. More flexible, less maintenance.
- **Dedicated parser**: Bank statements are tabular data, different from invoices. Use efficient extraction rather than LLM for every statement.
- **Hybrid matching**: Auto-match confident pairs to reduce manual work, but surface uncertain ones to avoid silent errors.
- **Standalone page**: Keep separate from pipeline flow. Reconciliation is a different workflow - periodic review vs daily processing.
- **FY scoped**: Match existing system conventions. Users think in fiscal years.

## Dependencies / Assumptions

- Bank statements contain: date, description, amount (minimum fields)
- PDF statements are text-based (not scanned images requiring OCR)
- Invoices have `vendor_name`, `amount_gross`, and `invoice_date` for matching

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Technical] What library/approach for PDF table extraction? (pdfplumber, camelot, tabula)
- [Affects R3][Technical] What matching algorithm/thresholds for auto-matching?
- [Affects R7][Technical] How to detect duplicate transactions across uploads? (hash of date+amount+description?)
- [Affects R9][Needs research] Page layout and component structure for the reconciliation UI

## Next Steps

→ `/ce:plan` for structured implementation planning
