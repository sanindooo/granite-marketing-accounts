# Email Invoice Ingestion

Ingest emails from MS365 inbox, classify them, extract invoice data, and file PDFs to Google Drive.

## Prerequisites

- MS365 OAuth configured (`granite ops reauth ms365`)
- Google OAuth configured (`granite ops setup-sheets`)
- Claude API key in Keychain (`granite-accounts/anthropic/api_key`)
- Database initialized (`granite db migrate`)

## Quick Start

```bash
# 1. Fetch new emails from inbox
granite ingest email ms365

# 2. Process pending emails (classify + extract + file)
granite ingest invoice process
```

## Commands

### Fetch Emails

```bash
granite ingest email ms365 [OPTIONS]
```

**Options:**
- `--initial`: Ignore saved watermark, fetch all recent messages (does NOT set up delta sync)
- `--reset`: Clear all synced emails and watermark, then do a full inbox search (90 days back)
- `--backfill-from DATE`: Fetch all emails from DATE onwards AND set up delta sync for future incremental fetches
- `--sender NAME`: Search for emails from a specific sender (e.g., `--sender uber`)
- `--from DATE`: Only fetch emails received on or after this date (YYYY-MM-DD)
- `--to DATE`: Only fetch emails received on or before this date (YYYY-MM-DD)
- `--rescan`: Re-fetch emails even if already in database. Updates existing records and clears their processed status so they get re-classified and re-extracted.

#### Sync Modes Explained

**1. Default (Delta Sync)** — `granite ingest email ms365`
- Asks MS Graph: "What's new since I last checked?"
- Only returns emails that arrived AFTER your last sync
- Fast and efficient for daily use

**2. Backfill** — `granite ingest email ms365 --backfill-from 2026-01-01`
- Searches ALL emails from the specified date to now
- Then runs a delta sync to establish a checkpoint for future incremental syncs
- Best for: capturing historical invoices you missed

**3. Date Range (One-off search)** — `granite ingest email ms365 --from 2026-01-01 --to 2026-03-31`
- Searches emails within the specified date range
- Does NOT set up or affect delta sync
- Best for: re-scanning a specific period without changing sync state

**4. Initial** — `granite ingest email ms365 --initial`
- Fetches recent emails ignoring the watermark
- Does NOT update the watermark (unlike backfill)
- Use carefully — prefer `--backfill-from` for most historical catch-up needs

When using `--sender`, `--from`, or `--to` (without `--backfill-from`), the command uses search mode:
- Searches your inbox for matching emails
- Automatically skips emails already in the database (deduplication)
- Does not update the watermark

**Standard sync (delta mode):**
```json
{"source": "ms365", "batches": 2, "emails": 47, "next_watermark_saved": true}
```

**Backfill mode:**
```json
{"source": "ms365", "backfill_mode": true, "backfill_from": "2026-01-01", "backfill_emails": 150, "watermark_saved": true}
```

**Search mode:**
```json
{"source": "ms365", "batches": 1, "emails": 5, "search_mode": true, "sender_filter": "uber", "skipped_duplicates": 12}
```

### Process Invoices

```bash
granite ingest invoice process [--budget 2.00] [--backfill] [--limit N]
```

For each unprocessed email:
1. Classify via Claude Haiku (invoice | receipt | statement | neither)
2. If invoice/receipt: fetch PDF, extract 13 HMRC VAT fields
3. Assign expense category
4. Upload PDF to Google Drive
5. Write `invoices` row to database

Options:
- `--budget 2.00`: Per-run Claude API spend ceiling in GBP
- `--backfill`: Use higher budget (£20) and 1-hour cache TTL for bulk processing
- `--limit N`: Process at most N emails

Output:
```json
{
  "processed": 47,
  "classified_invoice": 12,
  "classified_receipt": 3,
  "classified_neither": 32,
  "filed": 15,
  "duplicates": 0,
  "errors": 0,
  "cost_gbp": "0.1850"
}
```

### View Pending Emails

```bash
granite ingest email pending [--limit N]
```

Lists emails that need attention (manual download required, errors, no attachment found). These are emails that couldn't be fully processed automatically.

Output:
```json
{
  "count": 3,
  "items": [
    {"msg_id": "AAM...", "from_addr": "noreply@zoom.us", "subject": "Your invoice", "outcome": "needs_manual_download"},
    {"msg_id": "BBN...", "from_addr": "billing@vendor.com", "subject": "Receipt", "outcome": "no_attachment"}
  ]
}
```

### View Email Body

```bash
granite ingest email body <MSG_ID>
```

Fetches and returns the full email body for inspection. Useful for debugging classification issues or checking email content before dismissing.

Output:
```json
{
  "msg_id": "AAM...",
  "body_html": "<html>...",
  "body_text": "Plain text version..."
}
```

### Dismiss Email

```bash
granite ingest email dismiss <MSG_ID> --reason <REASON>
```

Dismisses a pending email with a reason. This removes it from the "needs attention" list and records feedback to help train future classification.

**Reasons:**
- `not_invoice`: Email is not an invoice (marketing, notification, etc.)
- `resolved`: Issue was resolved manually (e.g., PDF uploaded by hand)
- `duplicate`: Duplicate of another email already processed

The dismissal is recorded in the `email_feedback` table for future learning.

## Re-scanning Emails

If emails were processed when the system had bugs, or you want to re-process them with improved classifiers:

```bash
# Re-scan all emails from Anthropic
granite ingest email ms365 --sender anthropic --rescan

# Re-scan all emails from November 2025 onwards
granite ingest email ms365 --from 2025-11-01 --rescan

# Re-scan emails from a specific date range
granite ingest email ms365 --from 2025-11-01 --to 2026-01-31 --rescan

# Then re-process them
granite ingest invoice process
```

**What `--rescan` does:**
1. Fetches emails from your inbox (matching your filters)
2. Updates existing records in the database with fresh data
3. Clears their `processed_at` status so they appear as "pending" again
4. The next `process invoices` run will re-classify and re-extract them

**When to use rescan:**
- After fixing bugs in the email sync or processing pipeline
- When you suspect emails were incorrectly classified
- To re-run extraction with improved prompts or models
- When you want to ensure nothing was missed

**Natural language examples:**
- "Re-scan all emails from Anthropic" → `--sender anthropic --rescan`
- "Re-process everything from November" → `--from 2025-11-01 --rescan`
- "Re-scan emails from Uber between January and March" → `--sender uber --from 2026-01-01 --to 2026-03-31 --rescan`

## Search for Specific Vendor

To find and process invoices from a specific company:

```bash
# Search for Uber invoices from the last 6 months
granite ingest email ms365 --sender uber --from 2025-10-01

# Process the newly fetched emails
granite ingest invoice process
```

The search mode:
- Searches your inbox for emails from the specified sender
- Checks each email against the database to skip duplicates
- Reports how many new emails were found vs skipped
- Does not affect your delta sync watermark (regular syncs still work)

This is useful when:
- You want invoices from a new vendor you haven't tracked before
- You need to backfill historical invoices from a specific company
- You're troubleshooting missing invoices from a particular sender

## Backfill Mode

For initial bulk processing of historical emails:

```bash
# Recommended: Fetch all emails from a date AND set up delta sync
granite ingest email ms365 --backfill-from 2026-01-01

# Process with higher budget and longer cache
granite ingest invoice process --backfill
```

**Why use `--backfill-from` instead of `--initial`?**
- `--backfill-from` fetches historical emails AND sets up delta sync for future runs
- `--initial` fetches emails but does NOT update the watermark (you'll re-fetch them next time)
- For historical catch-up, `--backfill-from` is almost always what you want

Invoice processing backfill mode:
- £20 budget ceiling (vs £2 default)
- 1-hour prompt cache TTL (vs 5 minutes)
- ~88% token savings through cache reuse
- Expected cost: £5-10 for ~500 invoices

## Pipeline Flow

```
MS365 Inbox
    │
    ▼
┌─────────────────┐
│ granite ingest  │  Delta query → emails table
│ email ms365     │  (envelope only: subject, from, date)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ granite ingest  │
│ invoice process │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
Classify   Fetch Full
(Haiku)    Body + PDF
    │         │
    └────┬────┘
         │
         ▼ (if invoice/receipt)
    ┌─────────┐
    │ Extract │  Haiku → Sonnet escalation
    │ (13 VAT │  Hallucination guards
    │ fields) │  Arithmetic validation
    └────┬────┘
         │
         ▼
    ┌─────────┐
    │ Category│  Override → Domain-hint → LLM
    └────┬────┘
         │
         ▼
    ┌─────────┐
    │  File   │  .tmp/ → Drive → DB commit
    └────┬────┘
         │
         ▼
Google Drive: Accounts/FY-YYYY-YY/<category>/<YYYY-MM>/<filename>.pdf
SQLite: invoices table with all extracted fields
```

## Edge Cases

### Expiring PDF URLs

Stripe (30-day) and Paddle (1-hour) invoice URLs expire. The processor fetches PDFs immediately on email receipt. If a URL has already expired, the email is marked `needs_manual_download` and surfaces in the Exceptions tab.

### Login-Gated Vendors

Some vendors (Zoom, Notion, AWS, GitHub) require portal login to download invoices. These are automatically flagged as `needs_manual_download`. Download the PDF manually and place it in the vendor's folder.

### Sonnet Escalation

The extractor uses Claude Haiku for cost efficiency. It escalates to Sonnet when:
- Overall confidence < 0.75
- Critical field confidence < 0.70 (VAT number, invoice number, date, amounts)
- Arithmetic validation fails (net + VAT != gross)
- Invoice date outside ±90 days of email received date

Sonnet is terminal — if it also fails confidence checks, the invoice is flagged for manual review.

### Duplicate Invoices

Same vendor + invoice number with same amount → logged as `duplicate_resend`, original kept.
Same vendor + invoice number with different amount → both flagged as `corrected_invoice` in Exceptions.

### Hallucination Guards

Extracted values are validated against source text:
- VAT numbers must match `GB\d{9}(\d{3})?` regex
- Supplier name must fuzzy-match sender domain or PDF text
- Invoice numbers and addresses must appear in source document

Fields that fail validation are nulled with confidence=0.

## Web UI

The system includes a web dashboard at `http://localhost:3000/dashboard` for visual operation:

**Pipeline Controls:**
- Run sync, process, and reconciliation with one click
- Live progress indicators while jobs are running
- Cancel stuck/stale runs (jobs running >1 hour)
- Automatic 15-second polling when runs are active

**Filters:**
- Search by vendor/sender
- Date range (one-off search, doesn't affect delta sync)
- Backfill from date (historical catch-up + sets up delta sync)
- Process limit

**Needs Attention Card:**
- Shows emails that need manual action (no PDF, login-gated, errors)
- View email body content
- Dismiss with feedback (not_invoice, resolved, duplicate) to train the system
- Upload PDF manually for login-gated vendors

**Invoices Page:**
- Search and filter invoices
- View invoice details
- Mark as deleted (soft delete)

To start the web UI:
```bash
cd web && npm run dev
```

## Monitoring

Check processing status:
```bash
# Recent runs
sqlite3 .state/pipeline.db "SELECT run_id, started_at, status FROM runs ORDER BY started_at DESC LIMIT 5"

# Running jobs (check for stuck processes)
sqlite3 .state/pipeline.db "SELECT run_id, operation, started_at, stats_json FROM runs WHERE status = 'running'"

# Unprocessed emails
sqlite3 .state/pipeline.db "SELECT COUNT(*) FROM emails WHERE processed_at IS NULL"

# Pending attention (needs manual action)
granite ingest email pending

# Error breakdown
sqlite3 .state/pipeline.db "SELECT outcome, COUNT(*) FROM emails GROUP BY outcome"

# Dismiss history (feedback for learning)
sqlite3 .state/pipeline.db "SELECT sender_domain, feedback_value, COUNT(*) FROM email_feedback GROUP BY sender_domain, feedback_value"
```

## Vendors

As invoices are processed, vendors are automatically tracked. View known vendors:

```bash
# List all vendors with invoice counts
granite vendors list

# Search for a specific vendor
granite vendors list --search anthropic
granite vendors list --search uber
```

Output:
```json
{
  "status": "success",
  "count": 1,
  "vendors": [
    {
      "vendor_id": "f967244e07c8f78c",
      "name": "anthropic, pbc",
      "domain": "mail.anthropic.com",
      "category": "software_saas",
      "invoice_count": 3,
      "total_gbp": "90.00",
      "last_invoice": "2026-04-17"
    }
  ]
}
```

Use this to:
- Find all invoices from a specific vendor
- Verify vendor categorization
- Track spending patterns by vendor

## Troubleshooting

### "needs_reauth" Error

MS365 token expired. Re-authenticate:
```bash
granite ops reauth ms365
```

### Budget Exhausted

Processing stopped due to Claude API spend ceiling. Options:
- Wait and re-run (budget resets per run)
- Increase budget: `granite ingest invoice process --budget 5.00`
- Use backfill mode: `granite ingest invoice process --backfill`

### Classification Errors

If emails are misclassified, check the classifier prompt:
```
execution/invoice/prompts/classifier.md
```

Add vendor-specific examples to the few-shot gallery if needed.
