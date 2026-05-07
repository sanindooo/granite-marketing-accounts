---
title: FX API Migration and Dashboard Query Alignment
category: integration-issues
date: 2026-04-19
tags: [fx-conversion, frankfurter-api, httpx, redirect, dashboard, fiscal-year, query-mismatch]
components: [execution/shared/fx.py, web/src/app/dashboard/fx-errors-card.tsx, web/src/app/api/pipeline/stream/route.ts, web/src/lib/queries/dashboard.ts]
symptoms: [fx-api-301-errors, missing-retry-button, pending-count-mismatch, cli-only-backfill]
---

# FX API Migration and Dashboard Query Alignment

## Problem Summary

Three related issues affecting FX conversion and dashboard metrics:
1. FX API requests failing with 301 errors due to upstream domain migration
2. No user-facing way to retry failed FX conversions from the dashboard
3. Pending emails count showing all-time totals instead of fiscal-year-filtered count

## Symptoms

- Error messages: "FX API returned 301 for USD→GBP on 2026-04-15"
- 50+ invoices stuck with `fx_error` column populated
- Dashboard showed CLI command "Run `granite db backfill-fx --force`" with no button
- Dashboard showed "354 pending" when processor found 0 emails for selected FY

## Investigation Steps

**FX API Issue:**
1. Users reported invoices stuck with `fx_error` column populated
2. Traced to `execution/shared/fx.py` making requests to `frankfurter.app`
3. Tested direct HTTP request: `curl -I https://api.frankfurter.app/2026-04-15`
4. Received 301 redirect to `api.frankfurter.dev/v1/`
5. Confirmed httpx defaults to `follow_redirects=False` (unlike requests library)

**Pending Count Issue:**
1. Dashboard showed 354 pending when viewing FY-2025-26
2. Processor reported 0 emails to process for FY-2025-26
3. SQL inspection revealed `pending_emails` CTE had no date filter
4. Verified: 354 emails were from Jan-Feb 2025 (FY-2024-25), not FY-2025-26

## Root Cause Analysis

### FX API (Problem 1)
- frankfurter.app migrated to `api.frankfurter.dev/v1/`
- Original domain returns 301 permanent redirect
- httpx's default `follow_redirects=False` differs from requests library behavior
- Result: all FX lookups failed with HTTP 301 status errors

### Retry Button (Problem 2)
- Pipeline stream API only supported 3 commands (syncEmails, processInvoices, runReconciliation)
- Type system didn't include `backfillFx`
- FX errors card displayed CLI instructions but had no interactive capability

### Pending Count (Problem 3)
- Dashboard `pending_emails` CTE: `WHERE processed_at IS NULL`
- Processor query: `WHERE processed_at IS NULL AND DATE(received_at) BETWEEN ? AND ?`
- Dashboard counted ALL pending; processor filtered by FY bounds
- Mismatch: 354 (all time) vs 0 (FY-2025-26)

## Solution

### Fix 1: FX API Redirect

**File:** `execution/shared/fx.py`

```python
# Before:
response = httpx.get(url, timeout=10.0)

# After:
response = httpx.get(url, timeout=10.0, follow_redirects=True)
```

### Fix 2: FX Retry Button

**Added command to pipeline API** (`web/src/app/api/pipeline/stream/route.ts`):
```typescript
const COMMANDS = {
  syncEmails: ["ingest", "email", "ms365"],
  processInvoices: ["ingest", "invoice", "process"],
  runReconciliation: ["reconcile", "run"],
  backfillFx: ["db", "backfill-fx"],  // NEW
} as const;
```

**Updated type** (`web/src/lib/types.ts`):
```typescript
export type PipelineCommand = "syncEmails" | "processInvoices" | "runReconciliation" | "backfillFx";
```

**Rewrote FX errors card** (`web/src/app/dashboard/fx-errors-card.tsx`):
- Uses `usePipelineStream` hook for SSE streaming
- Button with loading state and spinner animation
- Toast notifications for success/warning/error states
- Automatic `router.refresh()` after completion

### Fix 3: Pending Count FY Filter

**File:** `web/src/lib/queries/dashboard.ts`

```sql
-- Before:
pending_emails AS (
  SELECT COUNT(*) as count FROM emails WHERE processed_at IS NULL
)

-- After:
pending_emails AS (
  SELECT COUNT(*) as count FROM emails 
  WHERE processed_at IS NULL AND DATE(received_at) BETWEEN ? AND ?
)
```

Updated parameter binding to include FY bounds for pending emails query.

## Verification

1. **FX API:** `granite db backfill-fx --force` → 82 invoices converted, 0 errors
2. **Retry Button:** Click "Retry Conversion" → shows spinner, completes with toast
3. **Pending Count:** Select FY-2025-26 → shows 0 pending (matching processor)

## Prevention Strategies

### For HTTP Library Differences

1. **Explicit redirect handling:** Always specify `follow_redirects=True` for external APIs
2. **Use shared client:** Prefer `execution/shared/http.py` SafeHttpClient for untrusted URLs
3. **Document gotchas:** httpx ≠ requests in default behavior

```python
# Bad - relies on implicit behavior
response = httpx.get(url, timeout=10.0)

# Good - explicit about redirect handling
response = httpx.get(url, timeout=10.0, follow_redirects=True)
```

### For Dashboard Query Consistency

1. **Match filters exactly:** Dashboard metrics must use same filters as detail views
2. **Single source of truth:** Define filter predicates in shared constants
3. **Test invariants:** Dashboard count == list query count for same filter

```typescript
test("dashboard pending count equals processor pending count", async () => {
  const fy = "FY-2025-26";
  const metrics = getDashboardMetrics(fy);
  const processorCount = countPendingEmails(db, { fy_filter: fy });
  expect(metrics.pendingEmails).toBe(processorCount);
});
```

## Related Documentation

- [Layer Separation Enforcement](../architecture-issues/layer-separation-enforcement.md) - query consistency principles
- [Interface Mismatch Integration Testing](./interface-mismatch-integration-testing.md) - API integration patterns

## Supplementary Context

(auto memory [claude]) UK Ltd fiscal year runs Mar 1 → Feb 28/29, which explains why Jan-Feb 2025 emails fall in FY-2024-25, not FY-2025-26.
