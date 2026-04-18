# Granite Marketing Accounts

Self-hosted accounting pipeline for a UK Ltd: email invoice ingestion + Amex/Wise/Monzo bank reconciliation. Runs daily via launchd, produces a per-fiscal-year Google Sheet with matched expenses and exceptions.

## How to Use

**Talk to me naturally.** This system uses a 3-layer architecture where you (via an AI agent like Claude Code) orchestrate deterministic Python scripts. You don't need to memorize commands — just ask:

> "Fetch new emails and process any invoices"
> "Show me all invoices from April 2026"
> "Run the full reconciliation for this fiscal year"
> "Which transactions are still unmatched?"

I'll translate your request into the right CLI commands or database queries.

## Quick Start

```bash
# 1. Initial setup (one-time)
granite db migrate
granite ops setup-sheets
granite ops reauth ms365

# 2. Daily workflow (or let launchd run it)
granite reconcile run --adapters ms365,amex_csv,wise,monzo
```

## CLI Reference

### Database

| Command | Description |
|---------|-------------|
| `granite db migrate` | Apply pending migrations |
| `granite db status` | Show DB path and schema version |

### Operations

| Command | Description |
|---------|-------------|
| `granite ops health` | Quick health probe |
| `granite ops healthcheck` | Full pre-run check (JSON output) |
| `granite ops smoke-claude` | Verify Claude API connectivity |
| `granite ops setup-sheets` | Run Google OAuth flow |
| `granite ops reauth <source>` | Re-authenticate MS365, Monzo, or Wise |

### Email Ingestion

| Command | Description |
|---------|-------------|
| `granite ingest email ms365` | Fetch new emails from MS365 inbox |
| `granite ingest email ms365 --initial` | Ignore watermark, fetch all recent |

### Invoice Processing

| Command | Description |
|---------|-------------|
| `granite ingest invoice process` | Classify + extract + file pending emails |
| `granite ingest invoice process --backfill` | Bulk mode: £20 budget, 1h cache |
| `granite ingest invoice process --limit 10` | Process at most N emails |

### Bank Ingestion

| Command | Description |
|---------|-------------|
| `granite ingest bank monzo` | Pull Monzo transactions |
| `granite ingest bank wise` | Pull Wise statements (SCA-signed) |
| `granite ingest bank amex-csv` | Process CSVs from drop folder |

### Reconciliation

| Command | Description |
|---------|-------------|
| `granite reconcile run` | End-to-end: ingest → match → sheet |
| `granite reconcile run --skip-ingest` | Match only (skip adapter fetches) |
| `granite reconcile run --skip-sheet` | Match only (skip sheet write) |

### Output

| Command | Description |
|---------|-------------|
| `granite output create-fy FY-2026-27` | Create Drive folder + Sheets workbook |

## Example Queries

Since the data lives in SQLite, you can query it directly:

```bash
# Invoices from this month (April 2026)
sqlite3 .state/pipeline.db "SELECT vendor_name_raw, amount_gross, invoice_date FROM invoices WHERE invoice_date >= '2026-04-01'"

# Unmatched transactions
sqlite3 .state/pipeline.db "SELECT booking_date, description_raw, amount_gbp FROM transactions WHERE txn_id NOT IN (SELECT txn_id FROM reconciliation_rows WHERE txn_id IS NOT NULL)"

# Processing errors
sqlite3 .state/pipeline.db "SELECT msg_id, error_code FROM emails WHERE outcome = 'error'"

# Total expenses this FY
sqlite3 .state/pipeline.db "SELECT SUM(CAST(amount_gbp AS REAL)) FROM transactions WHERE txn_type = 'purchase' AND booking_date >= '2026-03-01'"
```

Or just ask me — I'll run the query for you.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  directives/          (Layer 1 — SOPs in Markdown)          │
│  ingest_email.md  setup.md  reauth.md                       │
└─────────────────────────┬───────────────────────────────────┘
                          │ You talk to the AI agent (Layer 2)
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  execution/           (Layer 3 — Deterministic Python)      │
│                                                             │
│  adapters/           invoice/          reconcile/           │
│  ├─ ms365.py         ├─ classifier.py  ├─ match.py          │
│  ├─ wise.py          ├─ extractor.py   ├─ split.py          │
│  ├─ monzo.py         ├─ filer.py       └─ state.py          │
│  └─ amex_csv.py      └─ processor.py                        │
│                                                             │
│  shared/             output/           ops/                 │
│  ├─ db.py            └─ sheet.py       ├─ healthcheck.py    │
│  ├─ claude_client.py                   └─ launchd/          │
│  └─ ...                                                     │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

```
MS365 Inbox ──► emails table ──► classifier ──► extractor ──► invoices table
                                     │                              │
                                     │                              ▼
Amex CSV    ──┐                      │                    Google Drive PDFs
Wise API    ──┼──► transactions ─────┴──► reconciler ──► Google Sheets
Monzo API   ──┘        table                                 │
                                                             ▼
                                              Expenses | Invoices | Exceptions
```

## Project Status

| Phase | Status | Description |
|-------|--------|-------------|
| 1. Foundation | Complete | SQLite, Keychain, CLI scaffold |
| 2. Email + Invoice | Complete | MS365 ingest, Claude classify/extract, Drive filing |
| 3. Bank Adapters | Complete | Amex CSV, Wise SCA, Monzo OAuth |
| 4. Reconciliation | Complete | Weighted matcher, sheet output |
| 5. Scheduling | Complete | launchd, healthcheck |
| 6. Expansion | Pending | Gmail, IMAP, vendor learning, year-end |

**The core pipeline is operational.** Phase 6 adds secondary inboxes and quality-of-life features.

## Directives

| Directive | Purpose |
|-----------|---------|
| `ingest_email.md` | Full email → invoice pipeline documentation |
| `setup.md` | Initial credential configuration |
| `reauth.md` | Token renewal procedures |

## Costs

- **Subscriptions:** £0/year (no paid APIs)
- **Claude API:** ~£5-10/year steady-state, ~£5-10 one-time backfill
- **Storage:** Google Drive (existing account)

## Setup

See `directives/setup.md` for full instructions. Quick version:

```bash
# 1. Install dependencies
pip install -e ".[all,dev]"

# 2. Store credentials in Keychain
security add-generic-password -a granite-accounts -s granite-accounts/anthropic/api_key -w "sk-ant-..."

# 3. Run OAuth flows
granite ops setup-sheets
granite ops reauth ms365

# 4. Initialize database
granite db migrate

# 5. Create fiscal year workbook
granite output create-fy FY-2026-27
```

## Development

```bash
# Run tests
pytest tests/ -k 'not live'

# Lint
ruff check .

# Type check
mypy execution/shared/
```
