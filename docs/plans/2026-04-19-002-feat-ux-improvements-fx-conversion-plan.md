---
title: "feat: UX improvements and automatic FX conversion"
type: feat
status: completed
date: 2026-04-19
---

# UX Improvements and Automatic FX Conversion

## Overview

Improve pipeline UX with better progress feedback, dismissible warnings, and automatic currency conversion during invoice processing.

## Problem Statement

1. **Stale warning too aggressive** - Shows "stuck" warning even when job is actively progressing (e.g., 1060/2458)
2. **Amounts show as "-"** - `amount_gross_gbp` only populates during reconciliation, leaving dashboard empty
3. **Confusing terminology** - "Reconciliation" implies bank statement matching but currently only does FX conversion

## Proposed Solution

### Phase 1: Stale Warning UX

**Files:** `web/src/app/dashboard/dashboard-content.tsx`

1. Track last progress update time, not just job start time
2. Only show stale warning if progress hasn't changed for 2+ minutes (fixed threshold)
3. Add "Dismiss" button that collapses warning into info icon
4. Store dismissed state in **localStorage keyed by run ID** (persists across refresh for that run)
5. Show tooltip with warning text on hover
6. Auto-reappear if progress truly stalls after dismissal (no update for 2+ minutes)

### Phase 2: Automatic FX Conversion During Processing

**Files:** 
- `execution/invoice/processor.py`
- `execution/invoice/filer.py`
- New: `execution/shared/fx.py`

1. Create FX rate fetcher using frankfurter.app (free, no API key)
2. **Use the invoice date for rate lookup**, not processing date
3. Cache rates in SQLite table (keyed by currency + date)
4. After extraction, convert `amount_gross` to GBP using historical rate
5. Populate `amount_gross_gbp` during filing, not reconciliation
6. Store the FX rate used for audit trail
7. Round converted amounts to **2 decimal places**

**Edge case handling:**

| Scenario | Behavior |
|----------|----------|
| Unknown currency (e.g., USDC, typo) | Leave `amount_gross_gbp` NULL, set `fx_error` field, surface in Needs Attention |
| Missing historical date (weekend/holiday) | Use nearest available date (frankfurter.app does this automatically) |
| API completely down | Continue processing, leave FX fields NULL, surface in Needs Attention |
| GBP invoice | rate = 1.0, no API call needed |

**Schema change:**
```sql
CREATE TABLE IF NOT EXISTS fx_rates (
    currency TEXT NOT NULL,
    rate_date TEXT NOT NULL,
    rate_to_gbp REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (currency, rate_date)
);

-- Add columns to invoices for audit
ALTER TABLE invoices ADD COLUMN fx_rate_used REAL;
ALTER TABLE invoices ADD COLUMN fx_error TEXT;
```

### Phase 3: Hide Reconciliation

**Files:** `web/src/app/dashboard/dashboard-content.tsx`

1. Hide "Run reconciliation" row entirely (placeholder until real reconciliation is built)
2. Bank statement reconciliation will be a separate feature
3. Remove from PIPELINE_COMMANDS array

## Acceptance Criteria

- [ ] Stale warning only shows when progress genuinely stalls (no change for 2+ minutes)
- [ ] Stale warning can be dismissed; dismissal persists across page refresh for that run
- [ ] Dashboard shows GBP amounts immediately after processing (no reconciliation needed)
- [ ] FX rates use invoice date, not processing date
- [ ] Unknown currencies don't block pipeline; they surface in Needs Attention
- [ ] Reconciliation row is hidden from pipeline UI

## Technical Considerations

- FX API: Use frankfurter.app (free, no key, reliable, supports historical rates back to 1999)
- Rate caching: Cache by (currency, date) pair. No TTL needed for historical rates (they don't change).
- Historical date fallback: frankfurter.app returns most recent trading day for weekends/holidays
- GBP invoices: rate = 1.0, no API call needed
- Decimal precision: Round to 2 decimal places at conversion time
- Backfill: Existing invoices get converted using their invoice dates (not today's rate)

## Implementation Units

### Unit 1: Stale Warning UX
- Track `lastProgressTime` in component state
- Update on each progress event
- Compare against current time, not start time
- Add dismiss button and info icon UI
- Store dismissed state in localStorage keyed by `stale-dismissed-${runId}`

### Unit 2: FX Rate Module
- Create `execution/shared/fx.py`
- Implement `get_rate_to_gbp(currency: str, date: str) -> tuple[Decimal | None, str | None]`
  - Returns (rate, None) on success
  - Returns (None, error_message) on failure
- SQLite cache keyed by (currency, date)
- Use frankfurter.app `/YYYY-MM-DD` endpoint for historical rates

### Unit 3: Integrate FX into Processing
- Modify `_write_invoice_row` in filer.py
- Extract invoice date from extracted data
- Call FX module to get rate for that date
- Populate `amount_gross_gbp` (rounded to 2dp), `fx_rate_used`, and `fx_error`

### Unit 4: Backfill Existing Invoices
- One-time migration script
- For each invoice with NULL `amount_gross_gbp`
- Fetch rate using invoice's date
- Update row with converted amount and rate used

### Unit 5: Hide Reconciliation
- Remove reconciliation from PIPELINE_COMMANDS array
- Clean up any reconciliation-specific UI code

### Unit 6: Surface FX Errors in Needs Attention
- Query for invoices where `fx_error IS NOT NULL`
- Add card: "X invoices missing GBP conversion"
- Link to filtered invoice list showing just those invoices

## Test Scenarios

1. Process USD invoice dated 2025-06-15 → `amount_gross_gbp` uses June 15 rate
2. Process GBP invoice → `amount_gross_gbp` = `amount_gross`, rate = 1.0
3. Process invoice with unknown currency "USDC" → NULL amount, fx_error populated, appears in Needs Attention
4. Process invoice dated on weekend → uses Friday's rate
5. FX API completely down → continues processing, NULL amounts, surfaces in Needs Attention
6. Stale warning dismissed → collapses to icon, persists on refresh
7. Stale warning dismissed, then true stall → reappears after 2+ minutes of no progress
8. Backfill existing invoices → uses each invoice's date, not today's rate

## Dependencies

- frankfurter.app API (free, no API key required)
- No new npm packages needed

## Out of Scope

- Bank statement reconciliation (will be separate feature)
- Manual FX rate override UI
- Multi-currency dashboard views
