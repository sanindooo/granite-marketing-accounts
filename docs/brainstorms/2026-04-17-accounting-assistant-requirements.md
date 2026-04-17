---
date: 2026-04-17
topic: accounting-assistant
---

# Accounting Assistant

## Problem Frame

Year-end accounting is painful because business-expense invoices are scattered across three inboxes (MS 365 business, Gmail personal, iCloud/IMAP), paid across three accounts (Amex UK, Wise Business, personal Monzo), and nothing ties them together. ~200–500 invoices per fiscal year need to be found, downloaded, filed, and reconciled against bank transactions. Today this is manual end-of-year slog; the goal is programmatic retrieval + structured reconciliation with a lightweight human verification step, so the accountant hand-off takes hours rather than days.

## Requirements

- **R1.** Ingest business-expense invoices from email inboxes (primary: MS 365 business; secondary: Gmail personal, iCloud/IMAP) and download them locally. "Invoice" includes PDF/image attachments and invoices behind "view invoice" links from SaaS providers.
- **R2.** Classify each email as invoice / receipt / neither, and extract vendor, date, total, currency from the invoice itself.
- **R3.** File invoices on disk by fiscal year → category → month, using a naming convention that makes year-end grep trivial.
- **R4.** Pull transactions from Amex UK, Wise Business, and personal Monzo into one unified ledger, with de-dup logic so the Wise→Amex clearing payment doesn't double-count the underlying Amex expenses.
- **R5.** Reconcile invoices against transactions and produce a per-fiscal-year Google Sheet with matched rows, unmatched transactions (need invoice), and unmatched invoices (need transaction). Human verifies in the sheet.
- **R6.** Surface inbound sales/credits (into Wise or Monzo) on a separate tab of the same sheet, flagging any credit without a matching issued invoice.
- **R7.** Operate as a scheduled background process (weekly or daily) so the sheet is always near-current — not a big-bang once-a-year run.

## Success Criteria

- At year-end, ≥90% of business-expense transactions on Amex/Wise/Monzo are automatically matched to a downloaded invoice; the rest are clearly flagged with enough context for manual resolution.
- Accountant hand-off consists of one folder + one Google Sheet; no ad-hoc inbox searching needed.
- Running the tool adds £0 in ongoing subscriptions (Claude API tokens for classification are the only variable cost — budgeted in low single-digit £/month).
- A month's worth of new invoices can be processed + reconciled in a single scheduled run without manual babysitting, with flagged items landing in the sheet for review.

## Scope Boundaries

- No invoice *generation* or *sending* (outbound sales invoices are manual; we only reconcile inbound payments against them).
- No direct submission to HMRC or Companies House. Output stops at the reconciled sheet + filed invoices for the accountant.
- No VAT logic in v1 (categories only). VAT can be layered in later if required.
- No mobile app or consumer distribution — single-user local tool.
- No automatic *deletion* of source emails. Read-only on inboxes.
- No full-blown accounting software replacement (chart of accounts, P&L reports, etc.). Stop at "filed + reconciled."

## Key Decisions

- **Email stack — adapter-per-provider, MS 365 first.** Most invoices land in the business inbox. Ship that end-to-end, then bolt on Gmail + IMAP adapters behind the same interface. Avoids a unification layer that won't pay for itself until v1.1+.
- **Banking feed — GoCardless Bank Account Data (free PSD2).** Covers Amex UK (Open Banking since 2023), Monzo, and Wise through one feed. Free, no subscription, PSD2-regulated. Wise's own API is a viable alternative just for Wise if the GoCardless Wise coverage is thin; Amex has no public API so Open Banking is the only automated path.
- **Reconciliation axis — unified ledger of all three accounts with de-dup.** User wants one view; Wise's "Amex payment" line will be flagged and collapsed against the underlying Amex settlement so the same expense isn't counted twice.
- **Categorization — 8 broad buckets.** Software/SaaS, Travel, Meals & Entertainment, Hardware/Office, Professional Services, Advertising, Utilities, Other. Matches how most UK accountants want the data, keeps classifier errors recoverable.
- **Folder layout — fiscal year → category → month.** `invoices/FY2026-2027/<Category>/YYYY-MM/YYYY-MM-DD_<vendor-slug>_<amount>_<currency>.pdf`. Fiscal year = Mar 1 → Feb 28/29 (standard UK Ltd). Vendor as secondary axis handled via filename + sheet, not nested folders (keeps tree shallow).
- **Verification UX — Google Sheet per fiscal year.** One sheet per FY, tabs for Expenses, Sales, Exceptions. Matches the project's existing Google integration pattern and gives the accountant a native format.
- **Build vs buy — custom build.** Existing tools (Hubdoc/Dext) require subscriptions the user is avoiding; Paperless-ngx is a viable future bolt-on for document search but isn't needed for v1. The 3-layer architecture in this repo is already shaped for exactly this kind of directive + Python script pipeline.
- **Schedule — weekly cron at minimum.** Keeps sheet current, spreads Claude API cost evenly, surfaces problems (e.g. broken auth) long before year-end pressure.

## Dependencies / Assumptions

- User can authorize GoCardless Bank Account Data end-user agreement for Amex UK, Wise, Monzo (requires SCA consent every ~90 days — tool must handle renewal gracefully).
- MS Graph app registration in the Granite Marketing tenant with Mail.Read scope.
- Google Sheets + Drive API already accessible in this project (credentials.json / token.json pattern per CLAUDE.md).
- Wise Business API token available if GoCardless coverage of Wise proves unreliable.
- Claude API key available for invoice classification/extraction (fits existing `.env` pattern).

## Outstanding Questions

### Resolve Before Planning

*(none — brainstorm-level product decisions are settled.)*

### Deferred to Planning

- **[Affects R1][Technical]** Best MS Graph auth flow for a long-running single-user tool — client credentials (app-only) vs delegated (user token with refresh). User-token refresh is likely simpler for one mailbox; confirm tenant admin-consent requirements.
- **[Affects R1][Needs research]** For "view invoice" links (Stripe, Paddle, etc.), which vendors expose a direct PDF URL vs require a logged-in session? Planning should enumerate top 20 vendors from the user's inbox and handle each. Vendor-specific adapters vs a generic headless-browser fallback is a real design call.
- **[Affects R2][Technical]** Prompt-caching strategy for the invoice classifier — per-vendor examples cached, or a single zero-shot prompt? At ~500 invoices/year volume it may not matter, but worth deciding once.
- **[Affects R4][Needs research]** Confirm GoCardless Bank Account Data coverage for Amex UK cards (personal vs corporate card distinction matters) and Wise Business multi-currency sub-accounts. Fallback is Wise API + CSV for Amex.
- **[Affects R4][Technical]** De-dup rule for Wise→Amex clearing: match by date + amount within ±3 days, or require exact statement-balance match? Plan should spec the algorithm and edge cases (partial payments, currency conversion on Wise side).
- **[Affects R3][Technical]** Multi-currency handling in filenames and folders — convert to GBP at transaction date, or keep original? Affects reconciliation math.
- **[Affects R5][Technical]** Matching heuristic between invoices and transactions — exact amount match first, then vendor fuzzy, then date window. Plan should define the confidence threshold that triggers "auto-matched" vs "needs review."
- **[Affects R6][Needs research]** What does "issued invoice" look like today — do you send them from a tool (e.g. Stripe, Xero, a template), or is it ad-hoc? Matching inbound payments needs an authoritative list of what you've invoiced.
- **[Affects R7][Technical]** Scheduling host — local cron on the user's Mac (simplest, stops when laptop sleeps), or a small always-on runner (e.g. a Raspberry Pi / cheap VPS / GitHub Actions). Planning should decide based on reliability expectations.

## Next Steps

→ `/ce:plan` for structured implementation planning
