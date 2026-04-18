---
date: 2026-04-18
topic: hosted-invoices-and-progress-ui
---

# Hosted Invoices & Progress UI

Builds on: `2026-04-18-invoice-management-ui-requirements.md`

## Problem Frame

**Email Sync Reliability:** The current MS Graph delta sync only captures emails from the moment tracking starts — historical emails from before the first sync are missed entirely. This causes legitimate invoices (with PDF attachments) to go undetected if they arrived before the user's first sync. Users lose confidence when they see invoices in their inbox that the system never captured.

**Hosted Invoices:** Some vendors (OpenAI, Anthropic, similar AI/SaaS tools) send charge notification emails but keep the actual invoice on their platform rather than attaching a PDF. These charges currently appear as unmatched transactions with no corresponding invoice record, or get skipped entirely. The user needs visibility: "you were charged £12, but there's no downloaded invoice for this" — and ideally, the system should auto-fetch the PDF when the vendor supports it.

**Progress UI:** Pipeline operations (sync emails, process invoices, reconciliation) can take minutes. The current "Running..." button state provides no feedback. Users need real-time visibility into what's happening, with the ability to see detailed logs and recover if something goes wrong. Operations should persist across page navigation so users don't have to babysit the tab.

## Requirements

### Email Sync Reliability

- R0a. Initial sync must backfill historical emails (default: current FY start date)
- R0b. Delta sync continues incrementally after initial backfill completes
- R0c. Dashboard shows sync coverage: earliest email date, total count, any gaps detected
- R0d. "Resync from date" option to re-fetch emails from a specific date without losing existing data

### Hosted Invoice Detection & Fetch

- R1. Detect charge notification emails that reference a vendor billing portal instead of attaching a PDF
- R2. For vendors with billing APIs (OpenAI, Anthropic, similar), attempt to auto-fetch the PDF invoice
- R3. If auto-fetch succeeds, create a normal Invoice record with the downloaded PDF filed to Drive
- R4. If auto-fetch fails or isn't available, create an Invoice record with status `needs_manual_download`
- R5. Show `needs_manual_download` invoices in the Exceptions view with a link to the vendor portal
- R6. Allow manual upload of a PDF to satisfy a `needs_manual_download` invoice
- R7. Extract charge amount, date, and vendor from the notification email to populate the invoice record even without the PDF

### Progress UI

- R8. CLI commands emit structured progress events (JSON lines to stderr) during execution
- R9. Server streams progress events to the browser via SSE
- R10. Dashboard shows real-time progress: current stage and count (e.g., "Processing invoice 5 of 12")
- R11. Expandable detail view shows full log stream (like watching CLI output)
- R12. Operations run server-side and persist across page navigation/refresh
- R13. Returning to dashboard reconnects to running operations and shows current progress
- R14. Progress events are buffered so reconnecting clients can catch up on missed events
- R15. Show clear error states with actionable messages when operations fail

## Success Criteria

- Charge notifications from OpenAI/Anthropic result in an invoice record (either with fetched PDF or flagged for manual download)
- User can see "5 invoices need manual download" in dashboard/exceptions view
- Running a sync with 50 emails shows live progress, not just "Running..."
- Refreshing mid-operation reconnects and shows current state
- Operations that fail mid-way show what succeeded and what failed

## Scope Boundaries

**In scope:**
- OpenAI and Anthropic billing API integration
- Generic "needs manual download" fallback for other vendors
- SSE-based progress streaming
- Server-side operation persistence

**Out of scope:**
- Browser-push notifications when operations complete (nice-to-have for later)
- Canceling running operations from UI (can kill from terminal if needed)
- Historical operation log beyond the current run

## Key Decisions

- **SSE over WebSocket**: SSE is simpler, unidirectional (server→client), and sufficient for progress updates. No bidirectional communication needed.
- **JSON lines to stderr**: Keep stdout clean for final JSON result; progress goes to stderr as newline-delimited JSON. Frontend parses the stream.
- **Server-side persistence**: Operations spawn as background processes with a run ID. Progress writes to a temporary file or SQLite table. Clients poll/stream by run ID.
- **Vendor-specific adapters**: Each billing API (OpenAI, Anthropic) gets its own fetch adapter. Generic fallback flags for manual download.

## Dependencies / Assumptions

- OpenAI and Anthropic billing APIs are accessible with existing credentials
- The classifier can distinguish "charge notification" emails from "actual invoice attached" emails
- Server has permissions to spawn background processes that outlive the HTTP request

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Technical] OpenAI billing API authentication — does it use the same API key as completions, or separate billing credentials?
- [Affects R2][Technical] Anthropic billing API — is there a programmatic way to fetch invoices, or only the console?
- [Affects R8][Technical] Exact progress event schema — what fields are needed for the frontend to render stage + count + detail?
- [Affects R12][Technical] Background process management — spawn with nohup, use a job queue, or something else?
- [Affects R14][Technical] Event buffer strategy — write to temp file, SQLite, or in-memory with TTL?

## Next Steps

→ `/ce:plan` for structured implementation planning
