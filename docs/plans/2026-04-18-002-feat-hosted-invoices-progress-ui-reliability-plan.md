---
title: "feat: Hosted Invoices, Progress UI & Email Sync Reliability"
type: feat
status: active
date: 2026-04-18
origin: docs/brainstorms/2026-04-18-hosted-invoices-and-progress-ui-requirements.md
---

# Hosted Invoices, Progress UI & Email Sync Reliability

## Overview

Three interconnected improvements to the invoice management system:

1. **Email Sync Reliability** — Fix the gap where historical emails (pre-first-sync) are never captured
2. **Progress UI** — Real-time progress streaming for long-running pipeline operations
3. **Hosted Invoice Detection** — Track and auto-fetch invoices from vendor portals (where possible)

Priority order: Reliability → Progress UI → Hosted Invoices. Users can't trust new features if basic sync is broken.

## Problem Statement

**Root cause discovered:** MS Graph delta queries only capture emails from the moment tracking starts. Emails received before the first sync (e.g., Relume, Railway, Atlassian invoices from April 11-12) are missed entirely because the first sync happened on April 13+.

**Current state:**
- Delta sync works for new emails but misses historical ones
- No backfill mechanism exists
- Progress feedback is just "Running..." with no detail
- Vendor portal invoices (OpenAI, Anthropic) have no handling at all

## Technical Approach

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Web Dashboard                            │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Sync Status  │  │ Progress     │  │ Exceptions View        │ │
│  │ (coverage,   │  │ (SSE stream) │  │ (needs_manual_download)│ │
│  │  gaps)       │  │              │  │                        │ │
│  └──────────────┘  └──────────────┘  └────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
         │                    │                     │
         ▼                    ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Route Handlers (SSE)                          │
│  /api/pipeline/stream - spawns CLI, pipes stderr to SSE         │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      granite CLI                                 │
│  - emit_progress() to stderr (NDJSON)                           │
│  - emit_success/emit_error() to stdout (final JSON)             │
│  - search_inbox() for historical backfill                       │
│  - fetch_since() for incremental delta                          │
└─────────────────────────────────────────────────────────────────┘
```

### Implementation Phases

#### Phase 1: Email Sync Reliability [Critical - Day 1]

Fix the fundamental issue: historical emails aren't captured.

**Tasks:**

1. **Add `--backfill-from` flag to `ingest email ms365`**
   - File: `execution/cli.py:306-410`
   - Uses `search_inbox()` with date filter
   - Dedups against existing emails
   - Runs before delta sync on first use

2. **Auto-detect first run and prompt for backfill**
   - If no watermark exists and no `--initial` flag, warn user
   - Suggest: "No prior sync found. Run with `--backfill-from 2026-03-01` to capture historical invoices"

3. **Add sync coverage to dashboard metrics**
   - File: `web/src/lib/queries/dashboard.ts`
   - Query: `MIN(received_at)`, `MAX(received_at)`, `COUNT(*)`
   - Show on dashboard: "Synced: 101 emails from Apr 13 - Apr 18"

4. **Add "Resync from date" option in Pipeline Controls**
   - File: `web/src/app/dashboard/dashboard-content.tsx`
   - Date picker that calls `granite ingest email ms365 --from <date>`

**Acceptance criteria:**
- [ ] Running `granite ingest email ms365 --backfill-from 2026-04-01` captures Relume, Railway, Atlassian invoices
- [ ] Dashboard shows sync coverage (earliest date, count)
- [ ] First-run without watermark suggests backfill

#### Phase 2: Progress UI Infrastructure [Day 2-3]

Enable real-time progress streaming from CLI to browser.

**Tasks:**

1. **Add `emit_progress()` helper**
   - File: `execution/shared/errors.py`
   - Output: JSON line to stderr
   - Schema: `{"event": "progress", "stage": str, "current": int, "total": int, "detail": str}`

2. **Instrument CLI commands with progress events**
   - File: `execution/cli.py`
   - `ingest email ms365`: emit per-batch progress
   - `ingest invoice process`: emit per-invoice progress
   - `reconcile run`: emit per-phase progress

3. **Create SSE Route Handler**
   - File: `web/src/app/api/pipeline/[command]/route.ts`
   - Runtime: `nodejs` (required for `child_process`)
   - Spawn CLI, pipe stderr through `ReadableStream`
   - Parse NDJSON lines, emit as SSE events

4. **Add run tracking table**
   - Migration: `003_runs_tracking.sql`
   - Schema: `run_id, command, status, started_at, progress_json, result_json`
   - Enables reconnection after page refresh

5. **Update dashboard to consume SSE**
   - File: `web/src/app/dashboard/dashboard-content.tsx`
   - Replace `runPipelineCommand()` with SSE connection
   - Show progress bar with current/total
   - Expandable log view for detail messages

**SSE Event Format:**
```typescript
// Progress event
event: progress
data: {"stage": "classify", "current": 5, "total": 20, "detail": "Anthropic invoice..."}

// Done event
event: done
data: {"exitCode": 0, "result": {...}}

// Error event
event: error
data: {"message": "Auth expired", "code": "NEEDS_REAUTH"}
```

**Acceptance criteria:**
- [ ] Running "Process invoices" shows live progress (5/12, 6/12, ...)
- [ ] Expandable log shows detailed messages
- [ ] Refreshing page reconnects to running operation
- [ ] Errors show actionable messages

#### Phase 3: Hosted Invoice Detection [Day 4-5]

Handle invoices that live on vendor portals.

**Research findings:**
- OpenAI/Anthropic billing APIs: Can fetch usage/cost data, but **NO API for invoice PDF download**
- Both require Admin API keys (separate from completion API keys)
- Invoices can only be downloaded manually or via email

**Adjusted approach:** Since auto-fetch isn't possible for OpenAI/Anthropic, focus on:
1. Detecting "charge notification" emails (no PDF attachment but mentions payment)
2. Extracting charge details from email body
3. Flagging as `needs_manual_download` with link to vendor portal

**Tasks:**

1. **Extend classifier to detect charge notifications**
   - File: `execution/invoice/prompts/classifier.md`
   - Add classification: `charge_notification` (no PDF, but contains payment info)
   - Signals: mentions "charged", "payment processed", links to "billing history"

2. **Add `EmailOutcome.charge_notification`**
   - File: `execution/shared/types.py`
   - Distinct from `invoice` (has PDF) and `neither` (not financial)

3. **Extract charge details from notification emails**
   - File: `execution/invoice/extractor.py`
   - Parse: amount, date, vendor, billing portal URL
   - Create Invoice record with `drive_file_id=NULL`, `needs_manual_download=True`

4. **Add `needs_manual_download` column to invoices**
   - Migration: `004_needs_manual_download.sql`
   - Boolean flag + `portal_url` column

5. **Show needs-manual-download in Exceptions view**
   - File: `web/src/app/invoices/page.tsx`
   - Filter: "Needs Manual Download"
   - Show vendor portal link for each

6. **Allow manual PDF upload to satisfy a flagged invoice**
   - Endpoint: `POST /api/invoices/[id]/upload`
   - Upload PDF → file to Drive → clear `needs_manual_download` flag

**Acceptance criteria:**
- [ ] OpenAI "Your account has been funded" email creates invoice with `needs_manual_download`
- [ ] Exceptions view shows "5 invoices need manual download"
- [ ] Clicking vendor link opens billing portal
- [ ] Uploading PDF clears the flag

## System-Wide Impact

### Interaction Graph

```
User clicks "Sync emails"
  → Dashboard calls /api/pipeline/syncEmails (SSE)
    → Route Handler spawns `granite ingest email ms365`
      → CLI emits progress to stderr (NDJSON)
        → Route Handler parses, emits SSE events
          → Dashboard updates progress bar
      → CLI emits final JSON to stdout
        → Route Handler emits "done" SSE event
          → Dashboard refreshes metrics
```

### Error & Failure Propagation

| Error | Source | Handling |
|-------|--------|----------|
| `AuthExpiredError` | MS Graph 401 | CLI emits error JSON, SSE sends `error` event with `NEEDS_REAUTH` code, Dashboard shows toast with reauth instructions |
| SSE disconnect | Browser/network | `EventSource` auto-reconnects with `Last-Event-ID` header, server replays buffered events |
| CLI crash | Process dies | Route Handler detects close, emits `error` event, Dashboard shows "Operation failed" |
| Partial completion | Error mid-batch | CLI commits per-batch, progress persisted in runs table, resumable |

### State Lifecycle Risks

- **Run state orphaning:** If server restarts mid-operation, `runs` table may have stale "running" entries. Add TTL-based cleanup (mark as "failed" if no heartbeat in 5 min).
- **Progress buffer overflow:** Limit buffer to last 100 events per run. Oldest events dropped if client never reconnects.

## Acceptance Criteria

### Functional Requirements

- [ ] Historical emails captured via backfill command
- [ ] Dashboard shows sync coverage (date range, count)
- [ ] Real-time progress for all pipeline operations
- [ ] Reconnection works after page refresh
- [ ] Charge notification emails create flagged invoices
- [ ] Exceptions view shows needs-manual-download invoices

### Non-Functional Requirements

- [ ] SSE latency < 500ms from CLI emit to browser render
- [ ] Progress events don't block main CLI execution
- [ ] No memory leaks from long-running SSE connections

### Quality Gates

- [ ] Tests for backfill deduplication logic
- [ ] Tests for SSE reconnection with `Last-Event-ID`
- [ ] Mock mode for charge notification extraction

## Dependencies & Prerequisites

- MS365 OAuth already working (existing)
- Google Drive filing already working (existing)
- `child_process.spawn` available in Node.js runtime (confirmed)
- Next.js Route Handlers support streaming (confirmed)

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| MS Graph search API rate limits | Medium | Backfill blocked | Add retry with exponential backoff, batch requests |
| SSE connection timeouts in production | Medium | Lost progress | Heartbeat every 30s, auto-reconnect on client |
| Classifier misses charge notifications | High initially | False negatives | Start with strict patterns, tune based on feedback |

## Sources & References

### Origin

- **Origin document:** [docs/brainstorms/2026-04-18-hosted-invoices-and-progress-ui-requirements.md](../brainstorms/2026-04-18-hosted-invoices-and-progress-ui-requirements.md)
- Key decisions: SSE over WebSocket, JSON lines to stderr, server-side run persistence

### Internal References

- MS365 adapter with `search_inbox`: `execution/adapters/ms365.py:322`
- Current pipeline invocation: `web/src/lib/actions/pipeline.ts:68`
- Existing streaming pattern: `web/src/app/api/download/route.ts`
- Email outcome types: `execution/shared/types.py:34`

### External References

- [Next.js SSE with Route Handlers](https://nextjslaunchpad.com/article/nextjs-server-sent-events)
- [MS Graph delta query behavior](https://learn.microsoft.com/en-us/graph/delta-query-overview)
- OpenAI/Anthropic billing APIs: Usage data only, no invoice PDF download

### Research Findings

- OpenAI Usage API: Available (`/v1/organization/costs`), requires Admin API key, **no invoice PDF endpoint**
- Anthropic Usage API: Available (`/v1/organizations/cost_report`), requires Admin API key (`sk-ant-admin...`), **no invoice PDF endpoint**
- Both providers email invoices to billing address — these can be captured via email sync

## Next Steps

1. **Immediate:** Run `granite ingest email ms365 --from 2026-04-01` to capture missing invoices
2. **Phase 1:** Implement backfill flag and sync coverage display
3. **Phase 2:** Add progress streaming infrastructure
4. **Phase 3:** Implement charge notification detection
