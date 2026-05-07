---
title: Fix invoice export, downloaded indicator, vendor search, and link-to-PDF extraction
type: fix
status: complete
date: 2026-05-01
completed: 2026-05-06
---

<!-- Completion note (2026-05-06): Phases A-F complete. Phase 0 (vitest test infra) deferred — all user-facing bugs are fixed and solution doc is written. Test infrastructure tracked separately. -->

# Fix invoice export, downloaded indicator, vendor search, and link-to-PDF extraction

## Enhancement Summary (deepening pass — 2026-05-01)

Eight parallel review/research agents (correctness, security, performance, maintainability, testing, api-contract, best-practices, learnings-researcher) inspected the v1 plan. Key changes incorporated below:

**Diagnosis corrections (load-bearing):**
1. **Bug 3's "RSC payload too big" theory was wrong.** Next.js `bodySizeLimit` applies to the REQUEST body of a Server Action, not the response — there is no documented response-size cap. The "fails minutes later" symptom matches **sync better-sqlite3 queue backup + no debounce on search input**, not payload size. Column trimming is still worth doing (less serialization CPU and wire bytes) but is *not* the load-bearing fix. The load-bearing fix is **debounce + accept that AbortController is cosmetic against Server Actions.**
2. **AbortController does NOT cancel server-side Server Action execution** (Next.js #81418, discussion #54516). It only stops the client from waiting. The blocked event loop persists. AbortController is therefore a UX nicety to drop stale render results, not a throughput fix.
3. **Bug 2 export-marking has a stream-lifecycle bug.** `archive.append(stream, ...)` is non-blocking; `archive.finalize()` resolves when archiver finishes writing into the buffer, not when the client received bytes. Marking on `.then(finalize)` over-credits invoices on client disconnect. Fix: mark per-entry on the inner stream's `end` event, no `error`.
4. **Bug 4 fix #1 has a classifier-quality regression.** `body_text = text_body or html_body` feeds raw `<table>…<a>…</a>` markup with inline styles into the classifier prompt when an email is HTML-only. Burns tokens, hurts accuracy. Fix: strip HTML to plaintext for classifier input; keep raw HTML for URL extraction only.

**Architectural correction:**
5. `docs/solutions/architecture-issues/layer-separation-enforcement.md` mandates `lib/queries/` is read-only. The proposed `markInvoicesExported` writes — must move to a new `lib/actions/exports.ts` or inline in the API route.

**Streaming correction:**
6. The current `PassThrough → ReadableStream` bridge in `web/src/app/api/download/route.ts` bypasses Web Streams backpressure (archiver issues #613, #571, #321). Replace with `Readable.toWeb(archive) as ReadableStream` (Node 18+) and wire `request.signal` to `archive.abort()` for client-disconnect handling.

**Security additions:**
7. `/api/download` has no auth gate — pre-existing, but the plan touches this handler. invoice_id has only ~64 bits of entropy and is not secret. Track as out-of-scope-but-noted.
8. `manual_download_url` (Bug 4 #4) must reject non-`https://` schemes before persisting (`javascript:`/`data:` URLs from email HTML are an XSS risk if rendered as `<a href={url}>` later).
9. Anchor-text scoring is fully attacker-controllable. Worst case (with SSRF + magic-byte still on): an attacker PDF gets filed as the user's invoice. Mitigation: prefer same-origin / allowlisted hosts before generic candidates.

**Scope cuts (per maintainability reviewer + Stephen's tight-slice preference):**
10. **Drop `export_count`** — `last_exported_at` answers "is exported?" and "when?". The `count` was speculative for a future "stale export" warning.
11. **Drop the `idx_invoices_exported` partial index** — premature without profiling on a few-thousand-row table.
12. **Drop the `INVOICE_LIKELY_HOSTS` constant** — two speculative entries with "// add as we encounter them" is premature abstraction. Re-introduce on third real entry.
13. **Drop "Skip already-exported" toggle (default ON)** — violates least surprise (Select All silently selecting fewer than N). Keep only the badge + the filter.
14. **Defer Stretch Fix #2 (anchor scoring + cap-5)** — Phase E step 1 alone (HTML body + existing regex) resolves the user's reported Webflow case because the Stripe URL pattern is already on the allowlist.

**Testing infrastructure:**
15. `web/package.json` has no test runner. Adopt **vitest** (handles TS/ESM, mocks `better-sqlite3`/`googleapis` cleanly, integrates with Next 16). Co-locate as `route.test.ts` next to `route.ts`.
16. Replace size-based payload assertions with structural ones (assert no `confidence_json` key in row, not size < 500 KB).
17. Add fixture scrubbing script `execution/dev/scrub_email_fixture.py` (regex-replace `acct_*`/`in_*`, redact signed-token query params).

**Sources cited by deepening agents:** archiverjs/node-archiver#613, #571, #321 · Next.js #81418, #54516, #48682 · Stripe Invoice object docs · `docs/solutions/architecture-issues/layer-separation-enforcement.md` · `docs/solutions/integration-issues/interface-mismatch-integration-testing.md` · `docs/solutions/patterns/stale-run-cleanup-pattern.md`.

---


## Overview

Four user-reported bugs are blocking day-to-day use of the invoices page. Each has a clear, narrow root cause once you read the code, but they touch four different layers (HTTP API, schema, query path, ingestion pipeline). This plan groups them into one PR-sized change because every fix is small, low-risk, and the user is blocked on all four simultaneously.

| # | Bug | Root cause (one line) |
|---|-----|----------------------|
| 1 | `/api/download` returns 400 for every selection | `z.string().uuid()` validator rejects 16-char sha256 invoice IDs |
| 2 | No way to see which invoices have already been exported | Schema has no "downloaded" column; UI has no badge or filter |
| 3 | Searching vendor "Webflow" hangs and silently fails minutes later | `LIKE '%term%'` on a non-indexed JOIN, plus better-sqlite3 blocking the Node event loop on the dev server while React Server Action keeps the connection idle |
| 4 | Webflow-style emails (clickable link → PDF) classify as `no_attachment` | `fetch_message_body()` defaults to plaintext, so HTML anchor `href`s never reach the URL-extraction regex. Webflow's "PDF" link is in fact a `invoice.stripe.com/.../pdf` URL — already in the regex allowlist; we just never see it because we strip HTML before scanning. |

## Problem Statement / Motivation

The user has just shipped Phases 1–5 of the accounting assistant. The invoices page is the deliverable surface — if export, search, and link-only invoice capture do not work, the whole pipeline's value is gated. All four issues were observed today (2026-05-01) and reported as a single batch. They are all bugs, not enhancements (with bug #2 being a small UX add that the user explicitly framed alongside the other three).

Carry-over context from prior solutions worth re-reading:
- `docs/solutions/integration-issues/fx-api-redirect-and-dashboard-fixes.md` — same class of issue (filter mismatch / silent failures) on the dashboard.
- `docs/solutions/runtime-errors/ms365-thread-safe-http-client.md` — pattern for thread-safe HTTP clients used by the ingestion pipeline.

## Proposed Solution

A single branch — `fix/invoice-export-search-and-link-pdf` — with four commits, one per bug, plus a migration commit. Each commit is independently revertable.

### Bug 1 — Download endpoint 400

**File:** `web/src/app/api/download/route.ts:13`

```ts
// before
const downloadSchema = z.object({
  invoiceIds: z.array(z.string().uuid()).min(1).max(MAX_INVOICES),
});

// after — invoice_id is sha256(msg_id||idx)[:16], not a UUID
const INVOICE_ID = z.string().regex(/^[a-f0-9]{16}$/, "invoice_id must be 16 hex chars");
const downloadSchema = z.object({
  invoiceIds: z.array(INVOICE_ID).min(1).max(MAX_INVOICES),
});
```

Source of truth for the format: `execution/invoice/filer.py:268` (`_invoice_id` returns `hashlib.sha256(...).hexdigest()[:16]`).

**Also:** when `parseError` triggers, log the actual zod error so the next regression is visible:
```ts
if (!result.success) {
  console.error("download: invalid body", result.error.flatten());
  return NextResponse.json(
    { error: "Invalid invoice IDs", issues: result.error.flatten() },
    { status: 400 }
  );
}
```

### Bug 2 — "Already downloaded" indicator (revised after deepening)

**Schema** (`execution/shared/migrations/009_add_invoice_export_tracking.sql`, new — minimum viable):
```sql
-- Drop export_count and the partial index per maintainability review.
-- last_exported_at alone answers "is this exported?" and "when?".
ALTER TABLE invoices ADD COLUMN last_exported_at TEXT;
```

Migrations are applied by `execution/shared/db.py` on connect.

**Layer separation correction** (per `docs/solutions/architecture-issues/layer-separation-enforcement.md`): `lib/queries/` is read-only by convention and grep-gated in CI. The `markInvoicesExported` write must NOT live there. New file: `web/src/lib/actions/exports.ts`:

```ts
"use server";
import { db } from "@/lib/db";

export async function markInvoicesExported(invoiceIds: string[]): Promise<void> {
  if (invoiceIds.length === 0) return;
  const placeholders = invoiceIds.map(() => "?").join(",");
  db.prepare(
    `UPDATE invoices
     SET last_exported_at = datetime('now')
     WHERE invoice_id IN (${placeholders})`
  ).run(...invoiceIds);
}
```

**Stream-lifecycle correction (load-bearing).** The v1 patch credited exports on `archive.finalize()`, but `archive.append(stream, ...)` is non-blocking and `finalize()` resolves when archiver has written into the PassThrough buffer — not when the client received bytes. Under client disconnect or stream errors, that over-credits. Mark per-entry, gated on the inner stream actually completing.

**API rewrite** (`web/src/app/api/download/route.ts`): also fixes the PassThrough leak per archiver issues #613/#571 (bypassed Web Streams backpressure):

```ts
import { Readable } from "node:stream";
import archiver from "archiver";
import { markInvoicesExported } from "@/lib/actions/exports";

// inside POST(request):
const archive = archiver("zip", { zlib: { level: 1 } });
const exportedIds: string[] = [];

// Wire client disconnect to abort the archive
request.signal.addEventListener("abort", () => archive.abort());

const limit = pLimit(CONCURRENCY);
const tasks = invoicesWithFiles.map((invoice) =>
  limit(async () => {
    if (!invoice?.drive_file_id) return;
    try {
      const stream = await downloadFileFromDrive(invoice.drive_file_id);
      const filename = makeFilename(invoice);
      // Wait for archiver to finish reading THIS entry before marking it.
      await new Promise<void>((resolve, reject) => {
        stream.once("end", () => {
          exportedIds.push(invoice.invoice_id);
          resolve();
        });
        stream.once("error", reject);
        archive.append(stream, { name: filename });
      });
    } catch (err) {
      console.error(`Failed to download ${invoice.invoice_id}:`, err);
      // Do NOT push to exportedIds.
    }
  })
);

Promise.all(tasks)
  .then(() => archive.finalize())
  .catch(() => archive.abort());

// Use Readable.toWeb instead of PassThrough → ReadableStream — preserves backpressure
const body = Readable.toWeb(archive) as ReadableStream;

// Mark exports only AFTER archive emits 'end' (zip fully written) AND
// the request was not aborted.
archive.on("end", () => {
  if (!request.signal.aborted) {
    markInvoicesExported(exportedIds).catch((e) =>
      console.error("markInvoicesExported failed:", e)
    );
  }
});

return new Response(body, {
  headers: {
    "Content-Type": "application/zip",
    "Content-Disposition": `attachment; filename="invoices.zip"`,
  },
});
```

Caveat carried forward: a slow client that disconnects after `archive.on('end')` fires but before the OS finishes flushing TCP buffers will still get marked. Acceptable trade-off; document in code comment.

**Type** (`web/src/lib/types.ts`):
```ts
last_exported_at: string | null;
// no export_count
```

**Type contract correction (per api-contract review):** the list queries trim columns (drop `confidence_json`, etc.) but `InvoiceRow` still declares them as required, so TS will not catch a regression that reads a missing field. Split into two types:

```ts
export interface InvoiceListRow {
  invoice_id: string;
  source_msg_id: string;
  vendor_id: string;
  vendor_name_raw: string;
  invoice_number: string;
  invoice_date: string;
  currency: string;
  amount_net: string | null;
  amount_vat: string | null;
  amount_gross: string;
  amount_gross_gbp: string | null;
  vat_rate: string | null;
  category: string;
  category_source: string;
  drive_file_id: string | null;
  drive_web_view_link: string | null;
  last_exported_at: string | null;
  deleted_at: string | null;
  vendor_name?: string;
}

export interface InvoiceRow extends InvoiceListRow {
  // detail-only fields:
  vat_number_supplier: string | null;
  reverse_charge: number;
  confidence_json: string | null;
  classifier_version: string;
  hash_schema_version: number;
  is_business: number | null;
  deleted_reason: string | null;
}
```

`getInvoices`/`getInvoicesByIds`/`getExceptionInvoices` return `InvoiceListRow[]`. `getInvoiceById` returns `InvoiceRow | null`.

**UI** (`web/src/components/invoice-table.tsx`):
- "Exported" pill column right of "Date": small badge when `last_exported_at != null`, with `title` tooltip showing the date. Use `lucide-react` `CheckCircle2` icon (existing dep via shadcn).
- **Drop the "Skip already-exported" toggle** per maintainability review. The badge + the explicit filter below are sufficient and avoid Select-All-silently-selecting-fewer surprise.

**Filter** (`web/src/components/invoice-filters.tsx`):
- New "Export" select beside Status: `All | Not exported | Exported`. nuqs key `exported` defaults to absent (`null` = no filter), so old shareable URLs continue to behave identically.
- Wire through `InvoiceFilters` query interface as `exported?: "yes" | "no"`.

**Query path** (`web/src/lib/queries/invoices.ts`):
```ts
if (filters.exported === "yes") conditions.push("i.last_exported_at IS NOT NULL");
if (filters.exported === "no")  conditions.push("i.last_exported_at IS NULL");
```

### Bug 3 — Vendor search hangs and fails minutes later (revised after deepening)

> **Diagnosis correction:** v1 attributed this to "RSC payload size" exceeding a Next.js limit. After verifying the bundled Next.js 15 docs (`web/node_modules/next/dist/docs/01-app/.../serverActions.md`), `bodySizeLimit` (default 1 MB) applies only to the REQUEST body sent TO a Server Action — not the response. There is no documented response-size cap. A 1 MB RSC response is normal-traffic-sized and would not produce a "fails minutes later" silent failure. Column trimming is still worth doing (less serialization CPU and wire bytes) but it is **not the load-bearing fix.**

The actual reinforcing causes:

**Cause A (load-bearing) — sync better-sqlite3 + no debounce on the search input.** `web/src/app/invoices/invoice-list.tsx:35-72` has no debounce; every nuqs `setFilters({ search })` fires a Server Action. A user typing "Webflow" produces ~7 actions in rapid succession. With better-sqlite3 being a sync C++ binding and the singleton connection in `db.ts`, every query blocks the Node main thread for its full duration. The 7 queries serialize globally; the result of the last keystroke is what the user sees, but earlier ones are still occupying the event loop. If any one query takes 500 ms (LIKE + JOIN on a few thousand rows), 7 of them = 3.5 s of stalled SSR. Add HMR socket churn on top of that in dev, and the connection eventually surfaces a generic toast "minutes later" when something times out upstream.

**Cause B (real but secondary) — list query selects `i.*` including `confidence_json`.** Cuts wire bytes and serialization CPU but does not by itself fix the queue stall.

**Fix A (server-side query):**
- Stop `SELECT i.*` and project only the columns the table needs. Add a `SelectInvoiceListColumns` helper or a literal column list; explicitly omit `confidence_json` from list endpoints.
- Add `LIMIT` enforcement (already present at 500) and surface a "showing first 500" hint when truncated.
- Add a partial covering index for the LIKE path on `vendors.canonical_name` is unhelpful for `%foo%`; instead add a normalized lowercase column or accept the scan and just project less.

```sql
-- 010_add_invoice_list_index.sql (only if profiling shows the JOIN itself is slow)
CREATE INDEX IF NOT EXISTS idx_inv_vendor_active
  ON invoices(vendor_id, invoice_date DESC)
  WHERE deleted_at IS NULL;
```

**Fix B (server-action payload):**

```ts
// web/src/lib/queries/invoices.ts
const LIST_COLUMNS = `
  i.invoice_id, i.source_msg_id, i.vendor_id, i.vendor_name_raw,
  i.invoice_number, i.invoice_date, i.currency,
  i.amount_net, i.amount_vat, i.amount_gross, i.amount_gross_gbp,
  i.vat_rate, i.category, i.category_source,
  i.drive_file_id, i.drive_web_view_link,
  i.last_exported_at, i.export_count,
  i.deleted_at,
  v.canonical_name as vendor_name
`;
```

Replace `SELECT i.*, v.canonical_name as vendor_name` with `SELECT ${LIST_COLUMNS}` in `getInvoices`, `getInvoicesByIds`, `getExceptionInvoices`. Detail-page query (`getInvoiceById`) keeps `SELECT i.*` since it needs the blob.

**Fix C (load-bearing client-side fix) — debounce the search input on the client BEFORE setFilters.** The 300 ms debounce on `debouncedSetSearch` in `invoice-filters.tsx:44` only debounces the URL update, but the URL change still triggers `useEffect → fetchInvoices` per change. Either (a) add a debounce inside the `useEffect` itself, or (b) keep server-side memoization. Concrete change in `invoice-list.tsx`:

```ts
// Destructure individual primitive fields rather than depending on object identity (per correctness review).
const { fy, vendor, category, status, search, dateFrom, dateTo, exceptions } = filters;
useEffect(() => {
  const ctrl = new AbortController();
  // The Server Action handler can't observe AbortSignal, but the client
  // can drop the result of stale calls before setState — that's the only
  // value AbortController provides here.
  let cancelled = false;
  (async () => {
    setLoading(true);
    setError(null);
    try {
      const result = exceptions
        ? await fetchExceptionInvoices(fy)
        : await fetchInvoices({ fy, vendor: vendor || undefined, category: category || undefined,
            status: (status as "matched" | "unmatched" | "pending" | "all") || "all",
            search: search || undefined, dateFrom: dateFrom || undefined, dateTo: dateTo || undefined });
      if (cancelled) return;
      if (result.ok) setInvoices(result.data); else setError(result.error.message);
    } catch { if (!cancelled) setError("Failed to load invoices"); }
    finally { if (!cancelled) setLoading(false); }
  })();
  return () => { cancelled = true; ctrl.abort(); };
}, [fy, vendor, category, status, search, dateFrom, dateTo, exceptions]);
```

**Caveat (per performance + correctness review):** `AbortController.abort()` on a Server Action does NOT cancel server-side execution — see `vercel/next.js#81418` and discussion `#54516`. The Server Action runs to completion; we just discard the result client-side. The blocked event loop is only relieved by **(a) making the query fast** (column trim + the existing LIMIT 500) and **(b) not stacking up un-debounced calls** (the destructured-deps + cancelled-flag pattern above). If the queue-backup symptom returns, the next-step fix is to convert the search path to a Route Handler (which DOES expose `request.signal` to the server) or to move better-sqlite3 onto a worker thread.

**Sanity check during work:** run `EXPLAIN QUERY PLAN` on the Webflow-search SQL against the production DB copy and confirm it uses `idx_inv_active_date` for the date range and a SCAN for the LIKE. If SCAN dominates, accept it (data set is small) and rely on payload trimming. Add a server-side `console.time("getInvoices")` for one PR to measure real query duration before deciding on the optional `idx_inv_vendor_active` index.

### Bug 4 — Extract PDFs from clickable email links (Webflow)

**Discovery from the user's screenshot (2026-05-01):** the "Your Webflow receipt" email shows an `Invoice ID in_0T2cA1o2ZNzxqgUAOqtqEX9g` followed by a "PDF" anchor. That's a **Stripe-hosted invoice** (the `in_…` prefix and the URL shape `https://invoice.stripe.com/i/acct_…/in_…/pdf` are Stripe's standard). Webflow bills through Stripe. Crucially, **the existing regex `https?://invoice\.stripe\.com/[^\s<>\"']+` would already match this URL** — the only reason the pipeline misses it is that we never feed HTML body into the regex.

This narrows Bug 4 to a single root cause: HTML body is dropped before URL extraction.

**Root cause: HTML body is dropped.** `execution/invoice/processor.py:465` calls `adapter.fetch_message_body(email_row.msg_id)` with the default `prefer_html=False`. For HTML-only emails (which Webflow's Stripe-generated email is), the adapter returns the short `bodyPreview` snippet. The "PDF" anchor's `href="https://invoice.stripe.com/.../pdf"` therefore never reaches `_try_fetch_pdf_from_body`'s regex.

**Fix #1 (minimum viable, revised after correctness review):** fetch both bodies and run URL extraction across HTML first, falling back to text. The classifier must still receive plaintext — for HTML-only emails we must **strip tags before passing to the classifier**, otherwise the prompt burns tokens on `<table>` markup, inline styles, and tracking pixels and accuracy degrades on the very vendor we're trying to support (Webflow). The user's case passes because the Stripe URL pattern is already on the allowlist.

```python
# execution/invoice/processor.py — _process_one
from html.parser import HTMLParser

class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
    def handle_data(self, data: str) -> None:
        self._parts.append(data)
    @property
    def text(self) -> str:
        return " ".join(p.strip() for p in self._parts if p.strip())

def _html_to_text(html: str) -> str:
    p = _HTMLToText()
    p.feed(html)
    return p.text

# in _process_one:
html_body, text_body = adapter.fetch_message_body_both(email_row.msg_id)
classifier_body = text_body or _html_to_text(html_body)  # <-- strip, do NOT pass raw HTML
# ...
if not pdf_attachments:
    pdf_bytes, fetch_outcome = _try_fetch_pdf_from_body(
        text_body=text_body, html_body=html_body, http_client=http_client
    )
```

**`_try_fetch_pdf_from_body` minimum-viable update:** scan HTML body first by running each existing `_PDF_URL_PATTERNS` regex via `finditer` (not `search`) — first match in HTML wins **only if** the matched URL's host is in `_STRIPE_HOSTS`/`_PADDLE_HOSTS` or the pattern explicitly targets that host; generic `.pdf` matches are deferred to last. This avoids the marketing-CDN-PDF first-match brittleness flagged by correctness review C8. Then fall back to the existing plaintext path.

```python
def _try_fetch_pdf_from_body(*, text_body: str, html_body: str,
                             http_client: SafeHttpClient) -> tuple[bytes | None, FetchOutcome | None]:
    # Pass 1: known-vendor patterns over HTML (Stripe, Paddle, etc.)
    for body in (html_body, text_body):
        if not body:
            continue
        for pattern in _PDF_URL_PATTERNS:
            for m in pattern.finditer(body):
                url = m.group(0).rstrip(".,;:)")
                outcome = fetch_invoice_pdf(url, client=http_client)
                if outcome.status == FetchStatus.OK and outcome.body:
                    return outcome.body, outcome
                if outcome.status == FetchStatus.NEEDS_MANUAL_DOWNLOAD:
                    return None, outcome
    return None, None
```

**Stretch fix #2 — DEFERRED.** v1 proposed an anchor-scoring + cap-5 + `INVOICE_LIKELY_HOSTS` allowlist for non-Stripe vendors. Per maintainability review and Stephen's tight-slice preference: this is speculative defense for vendors not yet reported as broken. Re-introduce when a second link-only vendor actually fails. Two specific reasons not to ship it now:

1. The `INVOICE_LIKELY_HOSTS` constant would have only 2 speculative entries on day 1 — premature abstraction.
2. Anchor-text scoring is fully attacker-controllable (per security review S2): a phishing email can craft `<a>Download Invoice PDF</a>` pointing at attacker.com. SSRF + magic-byte checks remain in place, so worst case is filing an attacker-supplied PDF as the user's invoice. We avoid taking on that risk surface until we have a second real vendor justifying it.

**Fix #3 (kept) — observability.** When an attempted URL fetch returns NEEDS_MANUAL_DOWNLOAD, write the URL into `manual_download_url` so the Exceptions tab gives the user a clickable link (column already exists per migration 005). **Security gate (per security review S3):** validate URL scheme is `https://` before persisting; reject `javascript:`, `data:`, and any non-http(s) scheme. Otherwise a malicious email could plant a `javascript:alert(1)` URL that becomes clickable XSS the moment a future UI renders it as `<a href={url}>`.

```python
def _safe_manual_download_url(url: str) -> str | None:
    from urllib.parse import urlparse
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme == "https" else None
```

**Fix #4 (kept) — every fetched URL still flows through `SafeHttpClient`.** SSRF, size cap, redirect cap (each hop re-validated — confirmed by reading `execution/shared/http.py:309-360`), and `require_pdf_magic=True` remain on. No new code path skips them.

## Technical Considerations

- **better-sqlite3 is sync.** Every server action that queries the DB blocks the Node event loop. With one connection (the singleton in `db.ts`), all actions serialize. Trim payloads first, optimize queries second; do not introduce a second connection — SQLite + WAL handles concurrent reads but better-sqlite3 still blocks per call.
- **Migrations are append-only.** Number new files `009_…`, `010_…`. Verify the migration runner applies on connect (pattern from `execution/shared/migrations/008_*`).
- **Schema field naming:** `last_exported_at` (not `last_downloaded_at`) — "export" is the user-facing verb in the UI; the data does not change after export, only metadata.
- **No new client deps.** Anchor-tag parsing in Python uses stdlib `html.parser`. UI badge uses existing `lucide-react`.
- **Webflow link safety:** signed S3 URLs Webflow generates are public-by-design and short-lived. SSRF allowlist still applies; verify the URL hostname before fetch.
- **Server Action timeouts:** Next.js does not impose a hard ceiling on server-action duration in dev, but browsers do (~5 min idle). The fix here is to make queries fast/light, not to tweak timeouts.

## System-Wide Impact

### Interaction graph

`/api/download` POST → `getInvoicesByIds` → for each invoice in parallel: `downloadFileFromDrive` (Google API) → archiver → ReadableStream → client downloads zip → **NEW** `markInvoicesExported(ids)` → SQLite UPDATE → next invoice list refresh shows badge.

`processor._process_one` → `adapter.fetch_message_body_both` (NEW path) → `_try_fetch_pdf_from_body` (NEW: HTML-aware) → `pdf_fetcher.fetch_invoice_pdf` (unchanged guardrails) → `filer.file_invoice` → DB row written.

### Error propagation

- Bug 1 fix: invalid IDs now log actual zod issue and return structured error body. Existing 500-class catch unchanged.
- Bug 2 fix: a failed export does NOT credit `last_exported_at` (only successful per-invoice appends are tracked). If `archive.finalize()` succeeds but the client connection drops mid-download, we will mark exports despite the user not receiving the zip — accept this trade-off; user can re-export by toggling "Skip already-exported" off.
- Bug 3 fix: AbortController cancels stale fetches with a clean `AbortError`; current toast logic differentiates this from a real failure.
- Bug 4 fix: failed candidate URLs continue down the priority list. If all fail, classify as `needs_manual_download` and store best-guess URL in `manual_download_url`.

### State lifecycle risks

- `last_exported_at` could be stale if the underlying Drive file was deleted. Acceptable — the UI shows "exported", not "file still exists".
- `export_count` increments idempotently; no orphaning risk.
- HTML body is now fetched but not persisted. No new state.

### API surface parity

- `getInvoicesByIds` and `getInvoices` and `getExceptionInvoices` all need the trimmed column projection — keep them in lockstep so the table component never receives a row missing `last_exported_at`.
- `fetchInvoices` server action signature gains `exported?` filter; update callers in `invoice-list.tsx`.

### Integration test scenarios (revised after testing review)

1. **Round-trip export marking.** Insert 3 invoices, POST `/api/download`, drain the zip, verify all 3 have `last_exported_at` set. POST a second time and verify `last_exported_at` advances (no `export_count` to assert).
2. **Partial archive failure.** Stub Drive download to throw on invoice #2 of 3; assert only invoices 1 and 3 have `last_exported_at` set.
3. **Client disconnect mid-stream.** Abort the request signal before `archive.on('end')` fires; assert NO invoices are credited (because we gate on `!request.signal.aborted`).
4. **Concurrent export of same invoice.** Fire two POSTs in parallel for the same invoice id; assert both complete and `last_exported_at` is the timestamp of the later completion (no lost update).
5. **List query trims `confidence_json`.** Structural assertion (per testing review): assert returned row has no `confidence_json` key. Replaces the previous brittle "< 500 KB" assertion that tested a non-existent failure mode.
6. **AbortController cancellation drops stale results.** Use vitest fake timers — fire `setFilters` past the 300 ms debounce window so two real requests are issued; assert only the second result reaches state. (Real fetch is mocked.)
7. **400 with bad IDs.** POST one valid 16-hex ID + one literal `"bad-id"`; assert 400 with `error: string` AND `issues: object` (lock both fields of the contract).
8. **Well-formed unknown ID.** POST a syntactically-valid 16-hex ID that does not exist in DB; assert defined contract (recommend: 200 with empty zip).
9. **Webflow HTML body — Stripe URL extraction.** Real-email fixture (scrubbed) with `<a href="https://invoice.stripe.com/i/acct_TESTACCOUNT0001/in_TESTINVOICE0001/pdf">PDF</a>` inside marketing markup. Process through `_process_one` with `httpx.MockTransport` returning a valid PDF; assert `outcome == "invoice"` and `drive_file_id` is populated.
10. **HTML body with zero anchors.** Plaintext-only fallback path still runs the regex; assert no crash and `outcome == "no_attachment"` if no PDF URL.
11. **HTML body with a non-invoice .pdf URL ahead of the invoice URL.** Confirm vendor-specific patterns (Stripe/Paddle) win over generic `.pdf` — i.e., `finditer` ordering is intentional, not accidental.
12. **SSRF defence on HTML extraction.** Email contains `<a href="http://169.254.169.254/">Invoice</a>`; assert request is rejected at the `validate_url` boundary, not fetched.
13. **`javascript:` URL never persisted.** Email contains `<a href="javascript:alert(1)">PDF</a>`; assert `manual_download_url` is left NULL, not populated with the dangerous URL.
14. **Redirect-chain SSRF.** Webflow URL that 302s to `http://10.0.0.1/`; assert rejection at the redirect hop (already validated by reading `execution/shared/http.py:309-360`, but pin it with a test).
15. **Classifier doesn't see raw HTML.** Set up an HTML-only email; assert the classifier prompt receives stripped plaintext, not `<table>` markup. (Mock the LLM client and snapshot the user-content payload.)
16. **`getExceptionInvoices` lockstep.** After Phase D refactor, assert it returns `InvoiceListRow[]` with the same trimmed shape (per api-contract review residual risk).

## Acceptance Criteria

### Functional

- [ ] POST `/api/download` with a list of valid 16-hex invoice IDs returns a zip; the four bugs in this plan reproduce no longer.
- [ ] After a successful export, all included invoices show an "Exported" badge in the table, with tooltip showing date and count.
- [ ] Filter `Export = Not exported` returns only un-exported invoices; `Exported` returns only exported ones.
- [ ] "Select all (N)" with "Skip already-exported" toggled ON skips invoices with `last_exported_at != NULL`.
- [ ] Searching "Webflow" returns results in under 2 seconds and never produces a delayed failure toast.
- [ ] An MS365 email containing only an HTML link to a Webflow invoice PDF is processed end-to-end into an `invoices` row with a populated `drive_file_id`.

### Non-functional

- [ ] No regression in download flow under load: zipping 50 invoices completes in under 60 s on dev hardware.
- [ ] No new SSRF or unbounded-fetch vulnerabilities introduced (`SafeHttpClient` still gates every external call).
- [ ] List server-action response payload reduced (sample Webflow query: < 500 KB vs current multi-MB).

### Quality gates

- [ ] `mypy execution/` clean.
- [ ] `ruff check execution/` clean.
- [ ] `pytest tests/` passes; new tests for bugs 1–4 included.
- [ ] `npm run typecheck` (web) clean.
- [ ] Manual verification: export → toggle filter → re-search → process a Webflow email.

## Implementation Phases

### Phase 0 — Test infrastructure (per testing review)

1. Add `vitest` + `@vitest/ui` + `@vitejs/plugin-react` to `web/package.json` devDeps. Co-locate tests as `*.test.ts` next to source.
2. Add `web/vitest.config.ts` with `environment: 'node'` for API-route tests.
3. Confirm `tests/fixtures/emails/` exists for Python; add `execution/dev/scrub_email_fixture.py` that redacts `acct_*`, `in_*`, signed-token query params (`token=`, `signature=`, `sig=`, `key=`, `auth=`).

### Phase A — Migration + types

1. Add `execution/shared/migrations/009_add_invoice_export_tracking.sql` — single line: `ALTER TABLE invoices ADD COLUMN last_exported_at TEXT;`
2. Split `InvoiceRow` into `InvoiceListRow` (trimmed) and `InvoiceRow extends InvoiceListRow` (full) in `web/src/lib/types.ts`.
3. Verify the runner picks up the new migration on next connect.

### Phase B — Bug 1 fix

1. Replace `.uuid()` with `/^[a-f0-9]{16}$/` in `web/src/app/api/download/route.ts`.
2. Improve 400 error body to include `issues`.
3. Add `web/src/app/api/download/route.test.ts` (vitest) covering: valid IDs (200), one-bad-one-good (400 with `issues`), empty array (400), too-many (400), well-formed-but-unknown (200 with empty zip — define and assert this contract).

### Phase C — Bug 2 fix

1. Add `web/src/lib/actions/exports.ts` with `markInvoicesExported` (NOT in `lib/queries/`, per layer separation).
2. Rewrite `/api/download/route.ts`:
   - replace `PassThrough → ReadableStream` with `Readable.toWeb(archive)`,
   - wire `request.signal.addEventListener("abort", () => archive.abort())`,
   - mark per-entry on the inner stream's `end` event, gated on `!request.signal.aborted`,
   - call `markInvoicesExported` from `archive.on("end", …)`.
3. Add `Exported` pill column in `invoice-table.tsx` using `lucide-react` `CheckCircle2`.
4. Add `Export` select (`All | Not exported | Exported`) in `invoice-filters.tsx` (no Select-All toggle).
5. Plumb `exported` through `InvoiceFilters` query path.

### Phase D — Bug 3 fix

1. Introduce `LIST_COLUMNS` projecting only display fields; update `getInvoices`, `getInvoicesByIds`, `getExceptionInvoices`. Update return type to `InvoiceListRow[]`.
2. Refactor `invoice-list.tsx` `useEffect`: destructure individual filter fields into deps; add `cancelled` flag for stale-result drop; document AbortController is cosmetic per Next.js #81418.
3. Add a one-PR `console.time("getInvoices")` to measure real query duration; defer the `idx_inv_vendor_active` index until profiling justifies it.
4. Verify with `EXPLAIN QUERY PLAN`.

### Phase E — Bug 4 fix (minimum viable)

1. Switch `processor.py` to `fetch_message_body_both`; pass HTML body to URL extraction.
2. Strip HTML to plaintext (`_html_to_text` via stdlib `html.parser`) before passing to classifier — avoid token waste and accuracy regression on HTML-only emails.
3. Update `_try_fetch_pdf_from_body` to scan HTML first via `finditer` across `_PDF_URL_PATTERNS`; vendor-specific patterns (Stripe, Paddle) take priority over generic `.pdf`.
4. Validate `https://` scheme before persisting any URL into `manual_download_url`.
5. Test fixture: real Webflow email from 2026-05-01 scrubbed via the script from Phase 0. Includes Stripe `invoice.stripe.com/.../pdf` URL.

### Phase F — Verification

1. Run the integration tests from "Integration test scenarios" below.
2. Manually exercise the four flows.
3. Update `directives/ingest_email.md` only if it currently contradicts the new HTML-body behaviour (per maintainability review — don't update unnecessarily).
4. Write a single short solution doc at `docs/solutions/integration-issues/invoice-export-and-link-pdf-fixes.md` capturing the three reusable learnings: (a) UUID-vs-hex schema mismatch, (b) better-sqlite3 + sync-Server-Action queue backup, (c) plaintext-body trap that hides HTML anchor URLs.

### Phase F — Verification

1. Run the integration tests from "Integration test scenarios" above.
2. Manually exercise the four flows.
3. Update `directives/ingest_email.md` with the Webflow path and the HTML-body learning.
4. Write a solution doc at `docs/solutions/integration-issues/invoice-export-and-link-pdf-fixes.md` capturing: (a) the UUID-vs-hex schema mismatch, (b) the better-sqlite3 + RSC payload pitfall, (c) the plaintext-body trap that hides HTML anchor URLs.

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| New `last_exported_at` column not visible to in-flight Node process | Low | Low | Restart dev server after migration; column is nullable so existing rows are valid |
| HTML parsing introduces a parser-level vulnerability | Low | High | stdlib `html.parser` is non-validating, no DTD/entity expansion (XXE-safe by construction). Code comment forbids future swap to `lxml` without `no_network=True, resolve_entities=False, load_dtd=False`. |
| Stripe-hosted invoice URL expires before fetch completes | Medium | Low | If fetch fails, fall back to `needs_manual_download` with the URL stored — user can retry via existing manual upload. Note: per Stripe docs, PDF URLs are explicitly NOT guaranteed stable — never cache, always re-extract from a fresh email |
| AbortController gives false impression that server-side work was cancelled | Medium | Low | Document in code comment that `AbortController` only drops stale client results. The Server Action runs to completion. Real throughput fix is column trim + stable-deps + better-sqlite3 query speed |
| Marking exports on partial-archive success can over-credit | Low | Low | Mark per-entry on inner stream's `end` event, gated on `!request.signal.aborted`. Documented trade-off in route comment. |
| Anchor-text scoring (deferred) is attacker-influenceable | n/a | n/a | Not shipping in this plan. When re-introduced, prefer same-origin / allowlisted hosts before generic; keep SSRF + magic-byte checks |
| `manual_download_url` could store a `javascript:` URL | Low | High | `_safe_manual_download_url` rejects non-`https://` schemes before persisting |
| `/api/download` lacks authentication (pre-existing) | Medium | Medium | NOT introduced by this plan. Out of scope but **flagged**: invoice_id has only ~64 bits of entropy and is not secret. File a follow-up to add session check |
| DNS-rebinding TOCTOU between `validate_url` and httpx connect (pre-existing) | Low | High | NOT introduced by this plan. The HTML-body fetch widens the volume of attacker-supplied URLs being fetched, so the latent risk weight rises. Track as a follow-up |
| Splitting `InvoiceRow` into list/detail variants breaks downstream readers | Low | Low | TypeScript will catch this at compile time once the type is split. Run `npm run typecheck` before commit |
| `EXPLAIN QUERY PLAN` shows worse plan than expected | Low | Low | Add server-side `console.time` to measure real query duration before deciding on the optional index. Defer index until profiling justifies it |

## Dependencies & Prerequisites

- No new npm or Python packages required.
- Migration runner in `execution/shared/db.py` must be picking up files in `execution/shared/migrations/` (verified — it does, per existing pattern).
- A test Webflow invoice email in the inbox is needed to validate Phase E end-to-end. The user has one from "today" per the original report.

## Resource Requirements

- ~half-day of focused engineering work for one developer (slightly tighter than v1 after scope cuts).
- Six commits on a feature branch (Phase 0 test infra + A migration + B–E fixes + F docs); one PR.

## Future Considerations

- The HTML-aware URL extraction introduced in Phase E becomes the foundation for handling other "link-only" vendors (Asana, ClickUp, niche SaaS). Each new host gets one entry in `INVOICE_LIKELY_HOSTS` and (if needed) one URL-shape regex.
- `export_count` opens the door to a "stale export" warning if invoices are re-fetched after they were already exported (relevant for finalized accountancy submissions).
- Consider replacing `LIKE '%foo%'` with FTS5 once we have > 10k invoices. Out of scope here.

## Documentation Plan

- Update `directives/ingest_email.md` with the HTML-body fetch behaviour and the Webflow flow.
- Add a new solution doc `docs/solutions/integration-issues/invoice-export-and-link-pdf-fixes.md` after merge (per the self-annealing loop).
- README delta in `web/README.md` for the new "Skip already-exported" UI affordance.

## Sources & References

### Internal references

- `web/src/app/api/download/route.ts:13` — UUID validator (Bug 1)
- `execution/invoice/filer.py:268` — invoice_id is sha256[:16] (Bug 1 root cause)
- `web/src/lib/queries/invoices.ts:53-59` — LIKE search clause (Bug 3)
- `web/src/lib/queries/invoices.ts:74-83` — `SELECT i.*` payload (Bug 3 root cause)
- `web/src/app/invoices/invoice-list.tsx:35-72` — `useEffect` and fetch (Bug 3 client side)
- `execution/invoice/processor.py:465` — `fetch_message_body` defaults to plaintext (Bug 4 root cause)
- `execution/invoice/processor.py:78-93` — `_PDF_URL_PATTERNS` allowlist (Bug 4)
- `execution/invoice/pdf_fetcher.py:47-67` — `LOGIN_GATED_HOSTS` (Bug 4 — extend, do not add Webflow here)
- `execution/shared/migrations/005_add_needs_manual_download.sql` — `manual_download_url` column already exists (Bug 4 — populate it)

### Related solutions

- `docs/solutions/integration-issues/fx-api-redirect-and-dashboard-fixes.md` — same theme: filter mismatch + silent failure.
- `docs/solutions/runtime-errors/ms365-thread-safe-http-client.md` — thread-safety pattern for HTTP clients in concurrent processing (relevant if Phase E ever runs in parallel workers).
- `docs/solutions/architecture-issues/layer-separation-enforcement.md` — `lib/queries/` is read-only; enforces the move of `markInvoicesExported` to `lib/actions/`.
- `docs/solutions/integration-issues/interface-mismatch-integration-testing.md` — confirms the `_PDF_URL_PATTERNS` infrastructure already exists; Bug 4 is an input-source problem (HTML body), not a missing pattern.
- `docs/solutions/patterns/stale-run-cleanup-pattern.md` — context for "silently fails minutes later" symptoms; reinforces the read-only-query rule for web actions.

### External references (cited by deepening agents)

- archiverjs/node-archiver — issues [#613](https://github.com/archiverjs/node-archiver/issues/613), [#571](https://github.com/archiverjs/node-archiver/issues/571), [#321](https://github.com/archiverjs/node-archiver/issues/321) — PassThrough hang + swallowed read errors → reason for `Readable.toWeb` rewrite.
- vercel/next.js — [#81418](https://github.com/vercel/next.js/issues/81418), [discussion #54516](https://github.com/vercel/next.js/discussions/54516), [discussion #48682](https://github.com/vercel/next.js/discussions/48682) — Server Actions cannot be aborted; `request.signal` works in Route Handlers.
- Node.js docs — [`Readable.toWeb()`](https://nodejs.org/api/stream.html) — preserves backpressure end-to-end.
- Stripe — [Invoice object](https://docs.stripe.com/api/invoices/object), [hosted invoice page](https://docs.stripe.com/invoicing/hosted-invoice-page) — PDF URL shape is NOT guaranteed stable.
- Next.js 15 docs — `web/node_modules/next/dist/docs/01-app/03-api-reference/05-config/01-next-config-js/serverActions.md` — confirms `bodySizeLimit` is request-only (corrects v1 diagnosis of Bug 3).

### Related plans

- `docs/plans/2026-04-18-002-feat-hosted-invoices-progress-ui-reliability-plan.md` — established the "needs_manual_download" surface that Phase E plugs into.
- `docs/plans/2026-04-19-002-feat-ux-improvements-fx-conversion-plan.md` — most recent web-side bug-fix plan; same shape as this one.
