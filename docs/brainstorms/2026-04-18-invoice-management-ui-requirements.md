---
date: 2026-04-18
topic: invoice-management-ui
---

# Invoice Management Web UI

## Problem Frame

The `granite` CLI ingests invoices from email, classifies them with Claude, files them to Google Drive, and reconciles them against bank transactions. All data lives in a SQLite database, but there's no visual interface — querying requires CLI commands, and bulk operations (downloading multiple PDFs, filtering by date/vendor) are cumbersome.

Stephen needs a web interface to search, filter, view, and download invoices without running CLI commands each time. The UI should also expose pipeline controls (ingest, process, reconcile) and provide at-a-glance metrics.

## Requirements

### Invoice Browser
- R1. Display all invoices in a filterable, sortable data table
- R2. Filter by: fiscal year, date range, vendor, category, amount range, match status (matched/unmatched/pending)
- R3. Search by invoice number, vendor name, or description
- R4. Sort by date, amount, vendor, or status
- R5. Persist filter state in URL params (bookmarkable, back-button works)
- R6. Show error badges inline for invoices that failed processing
- R7. Provide a dedicated "Exceptions" filtered view for problem invoices

### Invoice Detail & PDF
- R8. View invoice metadata (vendor, date, amount, category, match status, Drive link)
- R9. Embed PDF preview in-app (fetch from Drive, cache locally)
- R10. Download individual invoice PDF from local cache
- R11. Store cached PDFs in `~/.granite/pdfs/` or project-local `.cache/pdfs/`

### Bulk Actions
- R12. Select multiple invoices via checkboxes
- R13. "Download selected" creates a ZIP of cached PDFs
- R14. "Select all (filtered)" selects all invoices matching current filters
- R15. "Cache all for FY" pre-downloads all PDFs for a fiscal year

### Dashboard
- R16. Display fiscal year selector (defaults to current FY)
- R17. Show invoice totals: count, total spend, by category breakdown
- R18. Show reconciliation status: matched count, unmatched count, pending count
- R19. Show top vendors by spend
- R20. Basic cash flow: income vs expenses for the period (if transaction data available)

### Pipeline Controls
- R21. "Sync emails" button triggers `granite ingest email ms365`
- R22. "Process invoices" button triggers `granite ingest invoice process`
- R23. "Run reconciliation" button triggers `granite reconcile run`
- R24. Show progress/status feedback for running operations
- R25. Display last run timestamp and outcome for each operation

## Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | Next.js 16 + App Router | Familiarity, Server Actions for mutations |
| Database | better-sqlite3 | Direct SQLite access, same DB as CLI |
| UI Components | shadcn/ui + Tailwind | Clean, minimal, fast to build |
| Data Table | Tanstack Table | Filtering, sorting, selection built-in |
| Deployment | Local (localhost:3000) | Access to Keychain secrets, no timeouts |
| Project location | `web/` directory | Shares repo with CLI, same DB path |

## Success Criteria

- Can find any invoice by vendor/date/amount in under 5 seconds
- Can download a batch of 50 invoices as ZIP in one click
- Can see FY totals and match status at a glance without CLI
- Can trigger full pipeline (ingest → process → reconcile) from browser
- Startup time < 3 seconds on localhost

## Scope Boundaries

**In scope (MVP):**
- Invoice browsing, filtering, search
- PDF viewing and bulk download
- Basic dashboard metrics
- Pipeline trigger buttons

**Out of scope (Phase 2):**
- Transaction browser (bank transactions are in DB but not exposed)
- Match/unmatch actions from UI (currently CLI-only)
- Manual invoice upload (currently email-only)
- Multi-user auth (single user, local-only)
- Mobile-responsive design (desktop-first internal tool)

## Key Decisions

- **Local-first deployment**: Avoids serverless timeout limits, keeps Keychain access for MS365/Google auth, simplifies architecture
- **Local PDF cache**: Fetch from Drive once, serve from disk — enables fast bulk downloads and offline access
- **URL-based filters**: Bookmarkable queries, browser navigation works naturally
- **CLI subprocess for actions**: Pipeline triggers shell out to `granite` commands rather than importing Python directly — keeps Next.js and Python decoupled
- **Same SQLite DB**: Web app reads/writes the same `.state/pipeline.db` the CLI uses

## Dependencies / Assumptions

- Existing `invoices`, `transactions`, `reconciliation_rows` tables are the source of truth
- Google Drive OAuth is already set up and working (existing `token.json`)
- PDF files are accessible via Drive API using stored `drive_file_id`
- CLI commands output JSON and return meaningful exit codes

## Outstanding Questions

### Deferred to Planning
- [Affects R9][Technical] How to handle Google Drive API auth from Next.js — reuse existing Python token or separate JS OAuth flow?
- [Affects R21-R23][Technical] Best approach for subprocess invocation from Server Actions — direct spawn, or wrap in a shell script?
- [Affects R11][Technical] Exact cache directory location — project-local `.cache/` vs user-level `~/.granite/`?
- [Affects R24][Needs research] Progress feedback pattern for long-running operations — polling, SSE, or WebSocket?

## Next Steps

→ `/ce:plan` for structured implementation planning
