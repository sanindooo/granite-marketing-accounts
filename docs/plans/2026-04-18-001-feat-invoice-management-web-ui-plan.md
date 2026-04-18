---
title: "feat: Invoice Management Web UI"
type: feat
status: completed
date: 2026-04-18
deepened: 2026-04-18
origin: docs/brainstorms/2026-04-18-invoice-management-ui-requirements.md
---

# Invoice Management Web UI

## Enhancement Summary

**Deepened on:** 2026-04-18  
**Research agents used:** TypeScript reviewer, Performance oracle, Security sentinel, Architecture strategist, Best practices researcher, Framework docs researcher, Code simplicity reviewer

### Key Improvements from Research

1. **Simplified to 5 phases** — Bank statements and sales invoice upload deferred to Phase 2 (40% LOC reduction)
2. **Drive iframe instead of local PDF cache** — Use `drive_web_view_link` directly; eliminates caching infrastructure
3. **Type-safe patterns** — `Result<T, E>` discriminated unions for Server Actions, typed SQLite rows
4. **Security hardening** — Command allowlist, LIKE wildcard escaping, token refresh handling
5. **Missing SQLite PRAGMAs** — Added `cache_size`, `mmap_size`, `temp_store` from Python codebase
6. **Architecture cleanup** — Added `lib/queries/` layer to separate SQL from Server Actions

### Critical Caveats Discovered

| Technology | Caveat | Mitigation |
|------------|--------|------------|
| Token sharing | Race condition on refresh between Python/Node | Read-only mode: don't refresh from Node.js |
| Subprocess | Command injection risk | Allowlisted commands only, no shell |
| Server Actions | Cannot stream directly | Use Route Handler for streaming if needed |
| react-pdf | Worker setup must be in same file as component | Configure `workerSrc` in PDF component |
| archiver | Use `forceZip64` for >4GB archives | Set flag; use `zlib: { level: 1 }` for speed |

---

## Overview

Build a local-first Next.js web application that provides a visual interface for the existing `granite` accounting CLI. The UI enables invoice search, filtering, PDF viewing, bulk downloads, dashboard metrics, pipeline controls, bank statement access, and sales invoice uploads.

This is a read-heavy UI layer on top of an existing, well-tested data pipeline. The hard work (email ingestion, Claude classification, reconciliation) already exists — this adds discoverability and convenience.

## Problem Statement / Motivation

The `granite` CLI ingests invoices from email, classifies them, files them to Google Drive, and reconciles them against bank transactions. All data lives in SQLite, but there's no visual interface. Finding a specific invoice requires CLI commands; bulk operations are cumbersome; there's no at-a-glance view of business health.

Stephen needs to:
- Find specific invoices quickly (by vendor, date, amount) for accountant queries
- Download batches of invoice PDFs for audits
- See reconciliation status and spending trends at a glance
- Trigger the pipeline from browser instead of terminal
- Access bank statements and track sales invoices he's issued

(see origin: `docs/brainstorms/2026-04-18-invoice-management-ui-requirements.md`)

## Proposed Solution

A Next.js 16 App Router application in `web/` that:
1. Reads directly from the shared SQLite database (`.state/pipeline.db`)
2. Invokes CLI commands via subprocess for write operations
3. Caches PDFs locally after fetching from Google Drive
4. Provides a clean, utilitarian dashboard matching Granite Marketing's visual identity

## Technical Approach

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (localhost:3000)                  │
├─────────────────────────────────────────────────────────────────┤
│  Next.js 16 App Router                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  Dashboard  │  │  Invoices   │  │  Statements │             │
│  │    Page     │  │   Browser   │  │    Page     │             │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘             │
│         │                │                │                     │
│  ┌──────┴────────────────┴────────────────┴──────┐             │
│  │              Server Actions (lib/actions/)     │             │
│  │  - invoices.ts (queries)                       │             │
│  │  - pipeline.ts (subprocess → granite CLI)      │             │
│  │  - drive.ts (PDF fetch → local cache)          │             │
│  └──────┬────────────────┬────────────────┬──────┘             │
│         │                │                │                     │
├─────────┴────────────────┴────────────────┴─────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │ better-     │  │ child_      │  │ googleapis  │             │
│  │ sqlite3     │  │ process     │  │ (Drive)     │             │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘             │
│         │                │                │                     │
└─────────┼────────────────┼────────────────┼─────────────────────┘
          │                │                │
          ▼                ▼                ▼
   .state/pipeline.db   granite CLI    Google Drive
                                       token.json
```

### Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | Next.js 16 + App Router | User familiarity, Server Actions for mutations |
| Database | better-sqlite3 | Synchronous SQLite access, same DB as CLI |
| UI Components | shadcn/ui + Tailwind | Clean, minimal, fast to build |
| Data Table | Tanstack Table v8 | Filtering, sorting, selection built-in |
| PDF Viewer | react-pdf or iframe | In-browser PDF rendering |
| ZIP Creation | archiver | Server-side ZIP for bulk download |
| Deployment | Local (localhost:3000) | Access to Keychain secrets, no timeouts |

### Database Access Strategy

**Reads**: Direct SQLite via `better-sqlite3` with the same PRAGMA set as Python:
```typescript
// web/lib/db.ts
import Database from 'better-sqlite3';
import { resolve } from 'path';

const DB_PATH = resolve(process.cwd(), '.state/pipeline.db');

// Singleton pattern for Next.js hot reload
const globalForDb = globalThis as unknown as { db: Database.Database | undefined };

function createDatabase() {
  const db = new Database(DB_PATH, { readonly: false });
  
  // Match Python's execution/shared/db.py PRAGMAs exactly
  db.pragma('journal_mode = WAL');
  db.pragma('synchronous = NORMAL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 30000');
  db.pragma('cache_size = -64000');   // 64MB page cache (was missing)
  db.pragma('mmap_size = 268435456'); // 256MB mmap (was missing)
  db.pragma('temp_store = MEMORY');   // RAM for temp tables (was missing)
  
  return db;
}

export const db = globalForDb.db ?? createDatabase();
if (process.env.NODE_ENV !== 'production') globalForDb.db = db;
```

**Query layer** (architectural improvement from research):
```typescript
// web/lib/queries/invoices.ts — Pure query functions, separate from Server Actions
import { db } from '../db';
import type { InvoiceRow } from '../types';

// LIKE wildcard escaping (from docs/solutions/integration-issues/)
function escapeLike(input: string): string {
  return input.replace(/\\/g, '\\\\').replace(/%/g, '\\%').replace(/_/g, '\\_');
}

export function searchInvoices(filters: InvoiceFilters): InvoiceRow[] {
  const escapedSearch = filters.search ? `%${escapeLike(filters.search)}%` : null;
  return db.prepare(`
    SELECT i.*, v.canonical_name as vendor_name
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE i.deleted_at IS NULL
      AND (? IS NULL OR v.canonical_name LIKE ? ESCAPE '\\')
    LIMIT ?
  `).all(escapedSearch, escapedSearch, filters.limit ?? 100) as InvoiceRow[];
}
```

**Writes**: For pipeline operations, invoke CLI via subprocess with allowlisted commands:
```typescript
// web/lib/actions/pipeline.ts
import { spawn } from 'child_process';
import { z } from 'zod';

// Security: Allowlisted commands only — never construct from user input
const COMMANDS = {
  syncEmails: ['ingest', 'email', 'ms365'],
  processInvoices: ['ingest', 'invoice', 'process'],
  runReconciliation: ['reconcile', 'run'],
} as const;

const FiscalYearSchema = z.string().regex(/^FY-\d{4}-\d{2}$/);

export async function runPipeline(
  command: keyof typeof COMMANDS,
  options?: { fiscalYear?: string }
): Promise<Result<CliOutput, ActionError>> {
  const args = [...COMMANDS[command]];
  if (options?.fiscalYear) {
    args.push('--fy', FiscalYearSchema.parse(options.fiscalYear));
  }
  
  const proc = spawn('granite', args, { 
    cwd: process.cwd(), 
    shell: false  // Critical: no shell interpretation
  });
  // Parse JSON output, handle errors...
}
```

### Google Drive Auth Bridge

The existing Python code uses OAuth credentials in `.state/token.json`. The Next.js app can reuse this token:

```typescript
// web/lib/drive.ts
import { google } from 'googleapis';
import { readFileSync } from 'fs';

function getAuthClient() {
  const credentials = JSON.parse(readFileSync('credentials.json', 'utf8'));
  const token = JSON.parse(readFileSync('.state/token.json', 'utf8'));
  
  const oauth2 = new google.auth.OAuth2(
    credentials.installed.client_id,
    credentials.installed.client_secret,
    credentials.installed.redirect_uris[0]
  );
  oauth2.setCredentials(token);
  return oauth2;
}
```

**Token refresh**: The `googleapis` library handles refresh automatically if the token has `refresh_token`. The Python code always saves refresh tokens, so this should work. If refresh fails, show a banner directing user to run `granite ops setup-sheets`.

### PDF Viewing Strategy (Simplified)

**Research insight**: For a local single-user tool, local PDF caching adds complexity without proportionate value. Use Drive's built-in viewer instead.

**MVP approach**: Use `drive_web_view_link` directly in an iframe:
```tsx
// components/pdf-viewer.tsx
export function PDFViewer({ driveWebViewLink }: { driveWebViewLink: string }) {
  // Drive's preview URL works in iframe for files you have access to
  const embedUrl = driveWebViewLink.replace('/view', '/preview');
  
  return (
    <iframe
      src={embedUrl}
      className="w-full h-[600px] border rounded"
      title="Invoice PDF"
    />
  );
}
```

**Benefits**:
- No caching infrastructure needed
- No token sharing complexity for Drive API
- No disk space management
- PDFs always up-to-date with Drive

**Fallback**: "Open in Drive" link if iframe fails (some browsers block third-party iframes).

**Bulk download** (still needs Drive API access):
```typescript
// For bulk ZIP, we do need to fetch PDFs — use existing Python token
// But only for the download action, not for viewing
```

### Sales Invoice Data Model

New table for manually uploaded sales invoices (not linked to email):

```sql
-- Migration 002: sales_invoices table
CREATE TABLE sales_invoices (
    sales_invoice_id    TEXT PRIMARY KEY,
    client_name         TEXT NOT NULL,
    invoice_number      TEXT NOT NULL,
    invoice_date        TEXT NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'GBP',
    amount_gross        TEXT NOT NULL,
    amount_gross_gbp    TEXT NOT NULL,
    category            TEXT NOT NULL DEFAULT 'sales',
    notes               TEXT,
    drive_file_id       TEXT,
    drive_web_view_link TEXT,
    local_path          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    deleted_at          TEXT
);
CREATE INDEX idx_sales_inv_date ON sales_invoices(invoice_date);
CREATE INDEX idx_sales_inv_client ON sales_invoices(client_name);
```

## Implementation Phases (Simplified to 5)

**Research insight**: The original 8 phases included bank statements and sales invoice upload, which were explicitly marked "out of scope (Phase 2)" in the brainstorm. Cutting to 5 phases reduces estimated LOC by 40% and ships MVP faster.

### Phase 1: Foundation (~2 hours)

**Goal**: Scaffold project, database connection, basic routing.

**Tasks**:
- [ ] Initialize Next.js 16 project in `web/` directory
- [ ] Install dependencies: `better-sqlite3`, `@tanstack/react-table`, `shadcn/ui`, `nuqs`, `zod`, `sonner`
- [ ] Configure Tailwind with minimal theme (dark text, white bg, blue accents)
- [ ] Create `lib/db.ts` with full PRAGMA configuration (including cache_size, mmap_size, temp_store)
- [ ] Create `lib/types.ts` with Result<T, E> and SQLite row types
- [ ] Create `lib/queries/` directory for pure SQL functions
- [ ] Add layout with navigation: Dashboard | Invoices
- [ ] Create placeholder pages
- [ ] Add `app/error.tsx` root error boundary

**Files**:
```
web/
├── app/
│   ├── layout.tsx
│   ├── error.tsx          # Root error boundary
│   ├── page.tsx           # Redirects to /dashboard
│   ├── dashboard/page.tsx
│   └── invoices/page.tsx
├── lib/
│   ├── db.ts              # SQLite connection with all PRAGMAs
│   ├── types.ts           # Result<T,E>, row types, CLI output types
│   └── queries/
│       └── invoices.ts    # Pure SQL query functions
├── components/
│   └── nav.tsx
└── package.json
```

**Success criteria**: `npm run dev` serves pages, SQLite connects without error.

---

### Phase 2: Invoice Browser (~3 hours)

**Goal**: Filterable, sortable invoice list with URL-persisted state.

**Tasks**:
- [ ] Create `lib/queries/invoices.ts` with pure SQL functions (not in Server Actions)
- [ ] Implement filter schema with Zod (single source of truth for validation + serialization)
- [ ] Build data table component using Tanstack Table
- [ ] Add filter controls: dropdowns for FY/vendor/category/status, date pickers
- [ ] Implement URL param persistence via `nuqs` (type-safe, better than raw useSearchParams)
- [ ] Add search input with LIKE wildcard escaping (security requirement)
- [ ] Client-side sort by date, amount, vendor (OK for <500 invoices per research)
- [ ] Add error badge for invoices with `outcome='error'`
- [ ] Create Exceptions filtered view

**Research insights applied**:

```typescript
// lib/filters/schema.ts — Zod as single source of truth (from best-practices research)
import { z } from 'zod';

export const invoiceFiltersSchema = z.object({
  fy: z.string().regex(/^FY-\d{4}-\d{2}$/).default(() => getCurrentFY()),
  vendor: z.string().optional(),
  category: z.enum(['software', 'travel', 'meals', 'hardware', 'professional', 'advertising', 'utilities', 'other']).optional(),
  status: z.enum(['matched', 'unmatched', 'pending', 'all']).default('all'),
  search: z.string().optional(),
  dateFrom: z.string().date().optional(),
  dateTo: z.string().date().optional(),
});

export type InvoiceFilters = z.infer<typeof invoiceFiltersSchema>;

// lib/queries/invoices.ts — LIKE escaping from docs/solutions/
function escapeLike(input: string): string {
  return input.replace(/\\/g, '\\\\').replace(/%/g, '\\%').replace(/_/g, '\\_');
}

export function getInvoices(filters: InvoiceFilters): InvoiceRow[] {
  const escapedSearch = filters.search ? `%${escapeLike(filters.search)}%` : null;
  // ... parameterized query with ESCAPE '\\'
}
```

**Success criteria**: Can filter 100+ invoices by any combination, URL is bookmarkable.

---

### Phase 3: Invoice Detail & PDF Viewer (~2 hours)

**Goal**: View invoice metadata and embedded PDF.

**Simplified approach** (from simplicity review): Use Drive's built-in viewer via iframe instead of local caching. Eliminates `lib/drive.ts`, cache management, and react-pdf dependency.

**Tasks**:
- [ ] Create `/invoices/[id]/page.tsx` detail route
- [ ] Display invoice metadata: vendor, date, amount, category, match status
- [ ] Create `PDFViewer` component using iframe with `drive_web_view_link`
- [ ] Add "Open in Drive" fallback link
- [ ] Add "Download" button (links to Drive download URL)
- [ ] Handle missing `drive_web_view_link` gracefully

**Files**:
```
web/
├── app/invoices/[id]/
│   ├── page.tsx
│   └── error.tsx    # Detail-level error boundary
└── components/
    └── pdf-viewer.tsx
```

**Implementation**:
```tsx
// components/pdf-viewer.tsx
export function PDFViewer({ driveWebViewLink }: { driveWebViewLink: string | null }) {
  if (!driveWebViewLink) {
    return <p className="text-muted-foreground">PDF not available</p>;
  }
  
  // Convert view URL to embed URL
  const embedUrl = driveWebViewLink.replace('/view', '/preview');
  
  return (
    <div className="space-y-2">
      <iframe
        src={embedUrl}
        className="w-full h-[600px] border rounded"
        title="Invoice PDF"
      />
      <a 
        href={driveWebViewLink} 
        target="_blank" 
        rel="noopener noreferrer"
        className="text-sm text-blue-600 hover:underline"
      >
        Open in Drive ↗
      </a>
    </div>
  );
}
```

**Success criteria**: Click invoice → see metadata + embedded PDF within 3 seconds.

---

### Phase 4: Bulk Actions (~3 hours)

**Goal**: Multi-select and bulk download.

**Simplified** (from research): No local cache; fetch PDFs from Drive on demand for ZIP creation. Remove "Cache all for FY" (YAGNI for MVP).

**Tasks**:
- [ ] Add checkbox column to invoice table
- [ ] Implement selection state with `useState` (limit: 100 invoices)
- [ ] Add "Select all filtered" button (caps at 100 with warning toast)
- [ ] Create `lib/actions/bulk.ts` for ZIP creation
- [ ] Implement "Download selected" → Server Action that:
  - Validates selection size (max 100, ~500MB estimated)
  - Fetches PDFs from Drive sequentially (5 concurrent with p-limit)
  - Creates ZIP using `archiver` with `zlib: { level: 1 }` (fast, PDFs already compressed)
  - Streams ZIP to browser via Route Handler (Server Actions can't stream)

**Research insights applied**:
```typescript
// app/api/download/route.ts — Route Handler for streaming (from framework-docs research)
import archiver from 'archiver';
import { google } from 'googleapis';

export async function POST(request: Request) {
  const { invoiceIds } = await request.json();
  
  // Validate
  if (invoiceIds.length > 100) {
    return Response.json({ error: 'Max 100 invoices' }, { status: 400 });
  }
  
  const archive = archiver('zip', { zlib: { level: 1 } }); // Fast compression
  
  // Stream to response
  const stream = new ReadableStream({
    async start(controller) {
      archive.on('data', (chunk) => controller.enqueue(chunk));
      archive.on('end', () => controller.close());
      archive.on('error', (err) => controller.error(err));
      
      for (const id of invoiceIds) {
        const pdfStream = await fetchPdfFromDrive(id);
        archive.append(pdfStream, { name: `${id}.pdf` });
      }
      await archive.finalize();
    },
  });
  
  return new Response(stream, {
    headers: {
      'Content-Type': 'application/zip',
      'Content-Disposition': 'attachment; filename="invoices.zip"',
    },
  });
}
```

**Success criteria**: Can download 50 invoices as ZIP in one click.

---

### Phase 5: Dashboard & Pipeline Controls (~4 hours)

**Goal**: At-a-glance metrics and pipeline triggers.

**Simplified** (from research): Numbers only, no charts. Category breakdown as a table, not pie chart. Consolidates original Phases 5 + 6.

**Tasks**:
- [ ] Create dashboard page with FY selector (defaults to current FY)
- [ ] Single SQL query with CTEs for all metrics (performance optimization)
- [ ] Build metric cards using shadcn/ui Card component
- [ ] Category breakdown as simple table (not chart)
- [ ] Top 5 vendors as simple list
- [ ] Handle empty FY state gracefully
- [ ] Add pipeline control buttons: "Sync emails", "Process invoices", "Run reconciliation"
- [ ] Implement subprocess invocation with allowlisted commands
- [ ] Show loading spinner during operation (simple, no progress bar)
- [ ] Handle `needs_reauth` error → show toast with CLI instructions
- [ ] Show last run timestamp from `runs` table

**Research insights applied**:
```typescript
// lib/queries/dashboard.ts — Single query with CTEs (performance optimization)
export function getDashboardMetrics(fy: string) {
  const { start, end } = fyBounds(fy);
  
  return db.prepare(`
    WITH invoice_totals AS (
      SELECT COUNT(*) as count, 
             SUM(CAST(amount_gross_gbp AS REAL)) as total
      FROM invoices 
      WHERE deleted_at IS NULL AND invoice_date BETWEEN ? AND ?
    ),
    recon_status AS (
      SELECT state, COUNT(*) as count 
      FROM reconciliation_rows
      WHERE fiscal_year = ?
      GROUP BY state
    ),
    category_breakdown AS (
      SELECT category, SUM(CAST(amount_gross_gbp AS REAL)) as total
      FROM invoices 
      WHERE deleted_at IS NULL AND invoice_date BETWEEN ? AND ?
      GROUP BY category
      ORDER BY total DESC
    ),
    top_vendors AS (
      SELECT v.canonical_name, SUM(CAST(i.amount_gross_gbp AS REAL)) as total
      FROM invoices i
      JOIN vendors v ON i.vendor_id = v.vendor_id
      WHERE i.deleted_at IS NULL AND i.invoice_date BETWEEN ? AND ?
      GROUP BY v.vendor_id
      ORDER BY total DESC
      LIMIT 5
    )
    SELECT 
      (SELECT count FROM invoice_totals) as invoice_count,
      (SELECT total FROM invoice_totals) as total_spend,
      (SELECT json_group_array(json_object('state', state, 'count', count)) FROM recon_status) as recon_json,
      (SELECT json_group_array(json_object('category', category, 'total', total)) FROM category_breakdown) as category_json,
      (SELECT json_group_array(json_object('name', canonical_name, 'total', total)) FROM top_vendors) as vendors_json
  `).get(start, end, fy, start, end, start, end);
}
```

**Reauth handling**:
```tsx
// When pipeline returns needs_reauth error
toast.error('Authentication expired', {
  description: 'Run `granite ops reauth ms365` in terminal, then retry.',
  duration: 10000,
});
```

**Success criteria**: Dashboard loads in < 2 seconds; pipeline triggers work with feedback.

---

## Deferred to Phase 2

### Bank Statements (Future)

**Why deferred**: Requires new CLI command (`granite ingest bank wise --statements`). Wise SCA cannot be automated in browser — statements must be pre-downloaded via CLI.

### Sales Invoice Upload (Future)

**Why deferred**: Requires new database table (`sales_invoices`), file upload infrastructure, Drive upload logic. Adds significant scope beyond "search existing invoices" goal.

---

---

## Archived: Original Phases 6-8 (Deferred to Phase 2)

<details>
<summary>Click to expand archived phases</summary>

### Original Phase 6: Pipeline Controls

*Merged into new Phase 5 (Dashboard & Pipeline Controls)*

### Original Phase 7: Bank Statements

**Deferred reason**: Requires new CLI command; Wise SCA blocks browser automation.

**Tasks when ready**:
- Create statements page listing `.cache/pdfs/statements/`
- Add "Fetch new statements" button (triggers CLI)
- Statement detail view with embedded PDF

**New CLI command needed**:
```python
@ingest_bank_app.command("wise-statements")
def ingest_bank_wise_statements(start: str, end: str, currency: str = "GBP"):
    """Download Wise statement PDFs to .cache/pdfs/statements/"""
```

### Original Phase 8: Sales Invoice Upload

**Deferred reason**: Requires new database table, file upload infrastructure, Drive upload logic.

**Tasks when ready**:
- Create migration for `sales_invoices` table
- Upload form with PDF validation (magic bytes, max 10MB)
- Security: UUID filenames, path sandboxing, dangerous PDF feature detection
- Upload to Drive `/FY-YYYY-YY/Sales/`
- Add to dashboard income section

**Security patterns from research** (apply when implementing):
```typescript
// PDF validation
const PDF_MAGIC = Buffer.from('%PDF-');
if (!buffer.subarray(0, 5).equals(PDF_MAGIC)) {
  throw new Error('Invalid PDF');
}

// Dangerous PDF features check
const dangerousPatterns = [/\/JavaScript\s/i, /\/Launch\s/i, /\/SubmitForm\s/i];
for (const pattern of dangerousPatterns) {
  if (pattern.test(pdfContent)) {
    throw new Error('PDF contains dangerous features');
  }
}
```

</details>

---

## System-Wide Impact

### Interaction Graph

```
User action → Server Action → SQLite read/write
                           → subprocess (granite CLI) → SQLite write
                           → Drive API → PDF cache write
```

- Server Actions are the single entry point for all mutations
- CLI subprocess ensures write logic stays centralized in Python
- Drive API calls are isolated to `lib/drive.ts`

### Error & Failure Propagation

| Error Source | Handler | User-Facing Behavior |
|--------------|---------|----------------------|
| SQLite busy | Catch `SQLITE_BUSY` | Banner: "Database busy, retry in a moment" |
| CLI subprocess fails | Parse JSON `status: error` | Show error message from `user_message` field |
| `needs_reauth` | Detect `error_code` | Modal with CLI instructions |
| Drive API 401 | Catch, check refresh | Banner: "Run `granite ops setup-sheets`" |
| Drive API 404 | Catch | "PDF not found in Drive. File may have been deleted." |
| ZIP too large | Check before creating | "Selection exceeds 500MB limit. Select fewer invoices." |

### State Lifecycle Risks

1. **CLI running during web write**: SQLite `busy_timeout=30s` should handle. If not, show "Database busy" error.
2. **Partial PDF cache**: If Drive fetch fails mid-bulk-cache, some PDFs cached, some not. UI should show cache status per invoice.
3. **Stale dashboard**: After pipeline run, dashboard shows old data until refresh. Add "Data updated. Refresh?" banner.

### API Surface Parity

| Capability | CLI | Web UI |
|------------|-----|--------|
| View invoices | `granite vendors list` | ✅ Invoice browser |
| Filter by FY | `--fy` flag | ✅ URL params |
| Run pipeline | `granite reconcile run` | ✅ Pipeline controls |
| View statements | N/A (new) | ✅ Statements page |
| Upload sales invoices | N/A | ✅ Upload form |
| Bulk download | N/A | ✅ ZIP download |

### Integration Test Scenarios

1. **Filter → bulk download**: Apply FY filter → select 20 invoices → download ZIP → verify all 20 PDFs in ZIP
2. **Pipeline with reauth**: Expire MS365 token → click "Sync emails" → verify reauth modal appears → reauth via CLI → retry → verify success
3. **Concurrent access**: Run `granite reconcile run` in terminal while web UI is open → verify UI doesn't crash, shows busy error if needed
4. **Empty FY**: Select FY with no data → verify dashboard shows empty state, invoice list shows "No invoices found"
5. **Large dataset**: Load 500+ invoices → verify pagination/virtualization, filter performance < 500ms

## Acceptance Criteria

### Functional Requirements (from origin doc)

**MVP (Phases 1-5):**
- [x] R1. Display all invoices in a filterable, sortable data table
- [x] R2. Filter by: fiscal year, date range, vendor, category, amount range, match status
- [x] R3. Search by invoice number, vendor name, or description
- [x] R4. Sort by date, amount, vendor, or status
- [x] R5. Persist filter state in URL params (via nuqs)
- [x] R6. Show error badges inline for failed invoices
- [x] R7. Provide dedicated "Exceptions" view
- [x] R8. View invoice metadata
- [x] R9. Embed PDF preview (via Drive iframe — simplified from local cache)
- [x] R10. Download individual PDF (via Drive link)
- [~] R11. Store cached PDFs locally — **Deferred**: using Drive iframe instead
- [x] R12. Multi-select via checkboxes
- [x] R13. "Download selected" creates ZIP
- [x] R14. "Select all filtered" (capped at 100)
- [~] R15. "Cache all for FY" — **Deferred**: YAGNI for MVP
- [x] R16. FY selector on dashboard
- [x] R17. Invoice totals and category breakdown (as table, not chart)
- [x] R18. Reconciliation status counts
- [x] R19. Top vendors by spend
- [~] R20. Basic cash flow — **Partial**: expenses only until sales invoices implemented
- [x] R21-R23. Pipeline control buttons
- [x] R24. Progress/status feedback (spinner + toast)
- [x] R25. Last run timestamp display

### Non-Functional Requirements

- [ ] Find any invoice by vendor/date/amount in < 5 seconds
- [ ] Download 50-invoice ZIP in one click
- [ ] Startup time < 3 seconds
- [ ] Dashboard loads in < 2 seconds

### Quality Gates

- [ ] TypeScript strict mode, no `any` types
- [ ] All Server Actions have error handling
- [ ] Empty states for all lists
- [ ] Loading states for all async operations
- [ ] Mobile-viewport doesn't break (not optimized, but functional)

## Estimated Effort (Post-Enhancement)

| Phase | Scope | Estimate |
|-------|-------|----------|
| Phase 1: Foundation | Scaffold, db, types, navigation | ~2 hours |
| Phase 2: Invoice Browser | Data table, filters, search, URL state | ~3 hours |
| Phase 3: Invoice Detail | Metadata display, Drive iframe | ~2 hours |
| Phase 4: Bulk Actions | Selection, ZIP download via Route Handler | ~3 hours |
| Phase 5: Dashboard + Pipeline | Metrics, controls, subprocess | ~4 hours |
| **Total MVP** | | **~14 hours** |

**Reduction from original**: 8 phases → 5 phases; ~24 hours → ~14 hours (42% reduction)

## Success Metrics

- **Time to find invoice**: < 5 seconds from page load to invoice detail
- **Bulk download success rate**: 100% for selections under 100 invoices
- **Pipeline trigger success**: Matches CLI success rate
- **Dashboard load time**: < 2 seconds with 1000+ invoices in FY

## Dependencies & Prerequisites

- [x] SQLite database with invoices, transactions, reconciliation tables (exists)
- [x] Google OAuth credentials.json and token.json (exists)
- [x] `granite` CLI installed and working (exists)
- [ ] Node.js 20+ for Next.js 16
- [ ] Migration 002 for `sales_invoices` table (new)
- [ ] CLI command for Wise statement download (new)

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Drive token refresh fails | Low | High | Show clear error with setup-sheets instructions |
| SQLite contention with CLI | Medium | Low | busy_timeout handles most cases; show error for edge cases |
| Large ZIP crashes browser | Low | Medium | Cap selection at 100, validate size before creating |
| User expects Wise statement API to work | Medium | Medium | Clear documentation that statements are CLI-fetched |

## Future Considerations (Phase 2)

- Transaction browser (bank transactions from Wise/Monzo/Amex)
- Match/unmatch actions from UI (currently CLI-only)
- Manual invoice upload from email (forward-to-ingest)
- Mobile-responsive design
- Dark mode toggle
- Export to CSV/Excel

## Documentation Plan

- [ ] Update README with web UI section
- [ ] Add `web/README.md` with setup instructions
- [ ] Document the Drive auth bridge pattern in `docs/solutions/`

## Sources & References

### Origin

- **Origin document**: [docs/brainstorms/2026-04-18-invoice-management-ui-requirements.md](docs/brainstorms/2026-04-18-invoice-management-ui-requirements.md)
- Key decisions carried forward: local-first deployment, CLI subprocess for actions, same SQLite DB, URL-based filters

### Internal References

- Database schema: `execution/shared/migrations/001_init.sql`
- CLI commands: `execution/cli.py`
- Error hierarchy: `execution/shared/errors.py`
- Drive integration: `execution/shared/sheet.py`
- Fiscal year utilities: `execution/shared/fiscal.py`
- Existing query patterns: `execution/cli.py:1188-1312`

### External References

- Next.js 16 App Router: https://nextjs.org/docs/app
- better-sqlite3: https://github.com/WiseLibs/better-sqlite3
- Tanstack Table: https://tanstack.com/table/latest
- shadcn/ui: https://ui.shadcn.com/
- Wise Statement API: https://docs.wise.com/api-docs/api-reference/balance-statement

### Related Work

- Original pipeline plan: `docs/plans/2026-04-17-001-feat-accounting-assistant-pipeline-plan.md`
- Integration issues solution: `docs/solutions/integration-issues/interface-mismatch-integration-testing.md`
