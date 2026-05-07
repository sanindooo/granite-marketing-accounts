---
title: "feat: Dashboard Polish and Inbox Preview"
type: feat
status: active
date: 2026-05-06
origin: docs/brainstorms/2026-05-06-dashboard-polish-and-inbox-preview-requirements.md
---

# feat: Dashboard Polish and Inbox Preview

## Summary

Two-phase implementation: Phase 1 wires up next-themes and applies Stripe-inspired polish to dashboard cards/metrics via Tailwind refinements. Phase 2 creates `/inbox` as a new route, reusing InvoiceTable's selectable-table pattern and extending the existing `dismissEmail` workflow with a `'rejected'` reason value.

---

## Problem Frame

The current pipeline controls work like a blind form — set filters, run sync, discover what came in after the fact. No visibility into which emails will be processed before committing. The dashboard is functional but lacks the professional, data-dense aesthetic of tools like Stripe Dashboard. (See origin: `docs/brainstorms/2026-05-06-dashboard-polish-and-inbox-preview-requirements.md`)

---

## Requirements

- R1. Dashboard cards, tables, and metrics follow Stripe Dashboard aesthetic: professional, data-dense, clear visual hierarchy
- R2. Typography, spacing, and color usage create a polished, high-end CRM feel
- R3. Visual refresh applies to main dashboard page only; invoices page polish is deferred
- R4. User can toggle between light and dark mode; preference persists across sessions
- R5. New `/inbox` page separate from the dashboard
- R6. Default view shows synced-but-unprocessed emails
- R7. Tabs allow switching between "unprocessed" and "all synced" views
- R8. Existing filter parameters available: sender search, date range
- R9. Checkboxes allow selecting individual emails, with select-all and select-none
- R10. "Process selected" triggers processing on checked emails only
- R11. "Reject" marks emails as dismissed; rejected emails excluded from future unprocessed views
- R12. Bulk reject available for multiple selected emails

**Origin actors:** A1 (Stephen — primary user)
**Origin flows:** F1 (Dashboard polish delivery), F2 (Inbox triage catch-up), F3 (Inbox triage targeted search)
**Origin acceptance examples:** AE1 (covers R6, R7), AE2 (covers R9, R10), AE3 (covers R11, R12), AE4 (covers R4)

---

## Scope Boundaries

- MS365 search with staging area for unsynced emails (valuable, deferred to future phase)
- Real-time inbox push notifications or webhooks
- Changes to Pipeline Controls on dashboard (inbox page is additive)
- Mobile-first responsive redesign
- Invoices page visual polish (Phase 1 focuses on dashboard only)
- Pagination for inbox (defer unless performance issues arise)

---

## Context & Research

### Relevant Code and Patterns

- `web/src/app/layout.tsx` — root layout where ThemeProvider will be added
- `web/src/app/globals.css` — CSS variables already configured for light/dark with OKLCH
- `web/src/components/nav.tsx` — navigation where theme toggle and inbox link appear
- `web/src/app/dashboard/dashboard-content.tsx` — dashboard implementation, Pipeline Controls
- `web/src/app/dashboard/needs-attention-card.tsx` — checkbox selection and bulk dismiss pattern
- `web/src/components/invoice-table.tsx` — selectable table pattern with @tanstack/react-table
- `web/src/components/invoice-filters.tsx` — URL-driven filter pattern with nuqs
- `web/src/lib/queries/dashboard.ts` — `dismissEmail`, `bulkDismissEmails`, `getPendingActions`
- `web/src/hooks/use-pipeline-stream.ts` — SSE streaming for pipeline commands

### Institutional Learnings

- **Effect dependencies**: Destructure primitive fields as `useEffect` dependencies, not entire objects. Add `cancelled` flag in cleanup to drop stale results. (from `docs/solutions/integration-issues/invoice-export-and-link-pdf-fixes.md`)
- **Query consistency**: Dashboard metrics must use same filters as detail views. Define filter predicates in shared constants. (from `docs/solutions/integration-issues/fx-api-redirect-and-dashboard-fixes.md`)
- **Layer separation**: `lib/queries/` = read-only. Writes in `lib/actions/` spawning CLI subprocess. (from `docs/solutions/architecture-issues/layer-separation-enforcement.md`)

### External References

- next-themes documentation (already installed as dependency)
- Stripe Dashboard as visual reference for aesthetic direction

---

## Key Technical Decisions

- **Theme via next-themes + localStorage**: Cookie approach adds SSR complexity not needed for single-user app; localStorage with `suppressHydrationWarning` is sufficient
- **Rejected = dismissed_reason 'rejected'**: Reuse existing `dismissed_at`/`dismissed_reason` columns rather than new schema — semantically the same dismissal workflow, just a different entry point
- **Process selected via --msg-ids filter**: Extend `processInvoices` CLI to accept optional `--msg-ids` filter rather than new command — smaller surface, reuses existing SSE streaming
- **Tabs for unprocessed/all toggle**: Visual counts useful for triage; tabs communicate state better than dropdown filter
- **Selection clears on action**: Optimistic clear on "Process selected" or "Reject" click — matches NeedsAttentionCard pattern, avoids stale selection state

---

## Open Questions

### Resolved During Planning

- **Rejected status value**: Use existing `dismissed_reason` with new value `'rejected'` — research confirmed schema supports this
- **Theme storage**: localStorage via next-themes default — single-user app doesn't need cookie SSR
- **Concurrent processing**: Reuse existing `fetchRunningJobs` check before allowing new processing — existing pattern handles this

### Deferred to Implementation

- **Exact Stripe-style refinements**: Specific spacing, typography, and color tweaks will be determined during implementation with visual iteration
- **Empty state copy**: Final wording for zero-result scenarios (no synced, all processed, all rejected) — will follow invoices page pattern

---

## Implementation Units

### U1. Wire up next-themes ThemeProvider

**Goal:** Enable light/dark mode infrastructure without visual changes yet

**Requirements:** R4

**Dependencies:** None

**Files:**
- Modify: `web/src/app/layout.tsx`
- Test: `web/src/app/layout.test.tsx`

**Approach:**
- Import `ThemeProvider` from next-themes
- Wrap children in ThemeProvider with `attribute="class"`, `defaultTheme="system"`, `enableSystem`
- Add `suppressHydrationWarning` to html element to avoid SSR mismatch warnings

**Patterns to follow:**
- Existing sonner.tsx already imports from next-themes (Toaster component)

**Test scenarios:**
- Happy path: Given ThemeProvider is mounted, when page loads, then no hydration warnings appear in console
- Happy path: Given system preference is dark, when page loads with defaultTheme="system", then .dark class is applied to html

**Verification:**
- No hydration errors on page load
- Browser dev tools show html element has correct class based on system preference

---

### U2. Add theme toggle to navigation

**Goal:** User can switch between light and dark mode

**Requirements:** R4

**Dependencies:** U1

**Files:**
- Modify: `web/src/components/nav.tsx`
- Create: `web/src/components/theme-toggle.tsx`
- Test: `web/src/components/theme-toggle.test.tsx`

**Approach:**
- Create ThemeToggle component using `useTheme` hook from next-themes
- Add sun/moon icon button that cycles through light/dark (skip system option for simplicity)
- Place toggle in nav bar, right side before any existing controls
- Use existing Button component with ghost variant

**Patterns to follow:**
- `web/src/components/ui/button.tsx` — existing button variants
- Icon usage from lucide-react (already a dependency)

**Test scenarios:**
- Happy path: Covers AE4. Given user is in light mode, when user clicks toggle, then dark mode class is applied and persists on refresh
- Happy path: Given user is in dark mode, when user clicks toggle, then light mode class is applied
- Edge case: Given localStorage is cleared, when page loads, then system preference is used

**Verification:**
- Toggle button visible in nav
- Click cycles between light/dark
- Preference survives page refresh

---

### U3. Dashboard visual polish — cards and metrics

**Goal:** Apply Stripe-inspired aesthetic to dashboard cards

**Requirements:** R1, R2, R3

**Dependencies:** U1, U2

**Files:**
- Modify: `web/src/app/dashboard/dashboard-content.tsx`
- Modify: `web/src/app/dashboard/needs-attention-card.tsx`
- Modify: `web/src/app/globals.css`

**Approach:**
- Refine card shadows, borders, and corner radius for cleaner look
- Adjust typography scale: use font-medium for labels, tabular-nums for metrics
- Increase information density: tighter spacing, clearer visual hierarchy
- Ensure both light and dark themes look polished
- **Fix Needs Attention table dark mode contrast**: Date column text is nearly invisible (dark text on dark background) — ensure all table text uses proper dark mode color variables
- Keep changes scoped to dashboard — do not touch invoices page

**Patterns to follow:**
- Stripe Dashboard as visual reference (data-dense, professional)
- Existing shadcn/ui Card component structure

**Test scenarios:**
- Test expectation: none — visual polish has no behavioral change; verification is visual inspection

**Verification:**
- Dashboard feels noticeably more polished and professional
- Both light and dark themes render correctly
- No visual regressions on NeedsAttentionCard functionality

---

### U4. Add inbox link to navigation

**Goal:** User can navigate to the new inbox page

**Requirements:** R5

**Dependencies:** U2

**Files:**
- Modify: `web/src/components/nav.tsx`

**Approach:**
- Add "Inbox" link between Dashboard and Invoices in nav
- Link points to `/inbox` route (page created in U5)
- Use same Link component and styling as existing nav items

**Patterns to follow:**
- Existing Dashboard and Invoices nav links in `nav.tsx`

**Test scenarios:**
- Happy path: Given user is on dashboard, when user clicks Inbox link, then user navigates to /inbox
- Happy path: Given user is on /inbox, then Inbox link shows active state

**Verification:**
- Inbox link visible in nav between Dashboard and Invoices
- Navigation works correctly

---

### U5. Create inbox page with email list

**Goal:** Display synced emails in a new /inbox route

**Requirements:** R5, R6, R7

**Dependencies:** U4

**Files:**
- Create: `web/src/app/inbox/page.tsx`
- Create: `web/src/app/inbox/inbox-content.tsx`
- Create: `web/src/lib/queries/inbox.ts`
- Create: `web/src/lib/actions/inbox.ts`
- Test: `web/src/lib/queries/inbox.test.ts`

**Approach:**
- Create page.tsx as Server Component that fetches initial data
- Create inbox-content.tsx as Client Component for interactivity
- Query emails table: unprocessed = `processed_at IS NULL AND dismissed_at IS NULL`
- All synced = `dismissed_at IS NULL` (includes processed)
- Use tabs component for unprocessed/all-synced toggle
- Display table with columns: from_addr, subject, received_at, outcome (if processed)
- Define `InboxEmailRow` type with trimmed columns (no large blobs per learnings)

**Patterns to follow:**
- `web/src/app/invoices/page.tsx` — Server Component pattern
- `web/src/app/invoices/invoices-content.tsx` — Client Component pattern
- `web/src/lib/queries/dashboard.ts` — query structure

**Test scenarios:**
- Happy path: Covers AE1. Given 15 synced emails (10 unprocessed, 5 processed), when viewing default tab, then 10 unprocessed shown
- Happy path: Covers AE1. Given same data, when switching to "all synced" tab, then all 15 shown
- Edge case: Given 0 synced emails, when viewing inbox, then empty state with helpful message shown
- Edge case: Given all emails are processed, when viewing unprocessed tab, then empty state shown

**Verification:**
- /inbox route loads without error
- Tabs switch between unprocessed and all views
- Email data displays correctly in table

---

### U6. Add filters to inbox page

**Goal:** User can filter emails by sender and date range

**Requirements:** R8

**Dependencies:** U5

**Files:**
- Modify: `web/src/app/inbox/inbox-content.tsx`
- Modify: `web/src/lib/queries/inbox.ts`

**Approach:**
- Add sender search input (debounced, matches from_addr with LIKE)
- Add date range picker for received_at filtering
- Use nuqs for URL-driven filter state
- Apply filters to both unprocessed and all-synced queries
- Destructure filter primitives as effect dependencies per learnings

**Patterns to follow:**
- `web/src/components/invoice-filters.tsx` — URL-driven filter pattern
- `web/src/hooks/use-debounced-search.ts` — debounce pattern

**Test scenarios:**
- Happy path: Given 10 emails from various senders, when user types "anthropic" in sender filter, then only Anthropic emails shown
- Happy path: Given emails from Jan-Mar, when user sets date range to Feb only, then only Feb emails shown
- Edge case: Given filter matches no emails, when applied, then empty state with "no matching emails" shown

**Verification:**
- Filters update URL query params
- List updates as filters change (with debounce on text input)
- Filters persist across page refresh

---

### U7. Add checkbox selection to inbox

**Goal:** User can select individual emails for bulk actions

**Requirements:** R9

**Dependencies:** U5

**Files:**
- Modify: `web/src/app/inbox/inbox-content.tsx`

**Approach:**
- Add selection state using Set<string> for msg_ids
- Add checkbox column to table (first column)
- Add select-all checkbox in header that toggles all visible emails
- Add select-none button to clear selection
- Selection is visual only at this point — actions come in U8/U9

**Patterns to follow:**
- `web/src/app/dashboard/needs-attention-card.tsx` — `selectedIds` state pattern
- `web/src/components/invoice-table.tsx` — @tanstack/react-table selection

**Test scenarios:**
- Happy path: Given 8 emails displayed, when user clicks checkbox on row 3, then row 3 is selected (checkbox checked)
- Happy path: Given 8 emails displayed, when user clicks select-all, then all 8 are selected
- Happy path: Given 5 emails selected, when user clicks select-none, then selection is cleared
- Edge case: Given select-all is checked and user unchecks one row, then select-all becomes unchecked

**Verification:**
- Checkboxes render and toggle correctly
- Select-all/select-none work as expected
- Selection state persists while navigating tabs (within same page load)

---

### U8. Implement reject action for selected emails

**Goal:** User can reject selected emails, excluding them from future unprocessed views

**Requirements:** R11, R12

**Dependencies:** U7

**Files:**
- Modify: `web/src/app/inbox/inbox-content.tsx`
- Modify: `web/src/lib/queries/inbox.ts`
- Modify: `web/src/lib/actions/inbox.ts`

**Approach:**
- Add "Reject" button that appears when emails are selected
- Call existing `bulkDismissEmails` pattern with `dismissed_reason = 'rejected'`
- Clear selection after rejection (optimistic)
- Show toast with count of rejected emails
- Refresh list to remove rejected emails from unprocessed view

**Patterns to follow:**
- `web/src/app/dashboard/needs-attention-card.tsx` — `handleBulkDismiss` pattern
- `web/src/lib/queries/dashboard.ts` — `bulkDismissEmails` function

**Test scenarios:**
- Happy path: Covers AE3. Given 8 unprocessed emails, when user selects 4 and clicks Reject, then those 4 marked dismissed with reason 'rejected'
- Happy path: Covers AE3. After rejection, when viewing unprocessed tab, then rejected emails no longer appear
- Edge case: Given user selects emails and clicks Reject, when action completes, then selection is cleared
- Error path: Given database error during rejection, when action fails, then toast shows error, selection preserved

**Verification:**
- Reject button appears when selection exists
- Rejection updates database with correct dismissed_reason
- Rejected emails excluded from unprocessed view immediately

---

### U9. Implement process-selected action

**Goal:** User can trigger processing on only the selected emails

**Requirements:** R10

**Dependencies:** U7

**Files:**
- Modify: `web/src/app/inbox/inbox-content.tsx`
- Modify: `execution/cli.py` (add --msg-ids filter to processInvoices)
- Modify: `web/src/lib/actions/inbox.ts`

**Approach:**
- Add "Process selected" button when emails are selected
- Extend `processInvoices` CLI command to accept `--msg-ids` JSON array filter
- Wire button to spawn CLI via server action, streaming progress via SSE
- Check `fetchRunningJobs` before allowing — disable button if processing already running
- Clear selection on action initiation
- Show progress inline or via toast (match existing Pipeline Controls UX)

**Patterns to follow:**
- `web/src/hooks/use-pipeline-stream.ts` — SSE streaming pattern
- `web/src/app/dashboard/dashboard-content.tsx` — Pipeline Controls integration
- `execution/cli.py` — existing CLI command structure

**Test scenarios:**
- Happy path: Covers AE2. Given 8 unprocessed emails, when user selects 3 and clicks Process selected, then only those 3 are processed
- Happy path: Covers AE2. After processing, when viewing inbox, then processed emails show in "all synced" but not "unprocessed"
- Edge case: Given processing is already running, when user opens inbox, then Process selected button is disabled
- Error path: Given processing fails for 1 of 3 emails, when action completes, then toast shows partial success

**Verification:**
- Process selected triggers CLI with correct msg_ids filter
- Only selected emails are processed
- SSE streaming shows progress

---

## System-Wide Impact

- **Interaction graph:** Theme toggle affects root layout and all pages. Inbox page adds new route that interacts with existing email/dismiss infrastructure.
- **Error propagation:** Inbox actions (reject, process) propagate errors via toasts. Processing errors follow existing Pipeline Controls error handling.
- **State lifecycle risks:** Selection state is client-side only — lost on page refresh. This is acceptable; users can re-select.
- **API surface parity:** processInvoices CLI gains --msg-ids filter; existing no-filter behavior unchanged (backwards compatible).
- **Integration coverage:** Process selected + SSE streaming needs integration test; reject action uses existing bulkDismissEmails which is already tested.
- **Unchanged invariants:** Pipeline Controls on dashboard remain unchanged. Existing dismiss workflow (from NeedsAttentionCard) continues to work.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Next.js 16 breaking changes may affect theme integration | Check `node_modules/next/dist/docs/` before implementation per AGENTS.md warning |
| Visual polish is subjective — "Stripe-style" may need iteration | Keep U3 scoped to refinements; iterate based on visual inspection rather than overengineering |
| Processing multiple selected emails may hit rate limits | Existing batch processing already handles this; reuse same patterns |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-06-dashboard-polish-and-inbox-preview-requirements.md](docs/brainstorms/2026-05-06-dashboard-polish-and-inbox-preview-requirements.md)
- Related code: `web/src/app/dashboard/needs-attention-card.tsx` (selection pattern)
- Related code: `web/src/hooks/use-pipeline-stream.ts` (SSE streaming)
- External docs: next-themes (theme infrastructure)
