---
date: 2026-05-06
topic: dashboard-polish-and-inbox-preview
---

# Dashboard Polish and Inbox Preview

## Summary

Two-phase UI overhaul: refresh the dashboard to match Stripe's professional aesthetic with light/dark mode toggle, then add a dedicated Inbox page for triaging emails before processing — see what's synced, select what to process, and bulk-reject the rest.

---

## Problem Frame

The current pipeline controls work like a blind form: set filters, run sync, then discover what came in after the fact. There's no visibility into what emails will be processed before committing to a run. This creates friction in two common workflows:

1. **Targeted hunting** — when looking for a specific invoice (e.g., "Anthropic sent one yesterday"), there's no way to confirm it's there before processing
2. **Periodic catch-up** — when sweeping through accumulated emails, unwanted items get processed and then dismissed after the fact

The dashboard also lacks visual polish. The current implementation is functional but plain — it doesn't match the professional, data-dense aesthetic of tools like Stripe Dashboard that set expectations for financial software.

---

## Actors

- A1. **Stephen (primary user)**: Accountant managing invoices. Needs to efficiently find, process, and categorize invoice emails while rejecting non-invoice content.

---

## Key Flows

- F1. **Dashboard polish delivery**
  - **Trigger:** Phase 1 begins
  - **Actors:** A1
  - **Steps:** Apply Stripe-style visual refresh to dashboard cards, metrics, and tables. Add theme toggle for light/dark mode.
  - **Outcome:** Dashboard feels professional and data-dense; theme preference persists
  - **Covered by:** R1, R2, R3, R4

- F2. **Inbox triage (catch-up)**
  - **Trigger:** User navigates to Inbox page for periodic processing
  - **Actors:** A1
  - **Steps:** Page loads showing unprocessed emails by default. User scans the list, optionally filters by sender/date. Selects emails to process (or uses "select all"). Bulk-rejects emails that aren't invoices. Triggers processing on selected set.
  - **Outcome:** Only desired emails are processed; rejected emails are cached and won't resurface
  - **Covered by:** R5, R6, R7, R8, R9, R10

- F3. **Inbox triage (targeted search)**
  - **Trigger:** User is looking for a specific invoice
  - **Actors:** A1
  - **Steps:** Navigate to Inbox page. Use filters (sender search, date range) to narrow results. Locate the target email. Select it for processing.
  - **Outcome:** Specific invoice found and processed without touching other emails
  - **Covered by:** R5, R6, R7, R8

---

## Requirements

**Dashboard visual refresh**
- R1. Dashboard cards, tables, and metrics follow Stripe Dashboard aesthetic: professional, data-dense, clear visual hierarchy
- R2. Typography, spacing, and color usage create a polished, high-end CRM feel
- R3. Visual refresh applies to the main dashboard page; invoices page polish is deferred

**Theme support**
- R4. User can toggle between light and dark mode; preference persists across sessions

**Inbox page — display**
- R5. New `/inbox` page separate from the dashboard
- R6. Default view shows synced-but-unprocessed emails
- R7. Tabs or filter allow switching between "unprocessed" and "all synced" views
- R8. Existing filter parameters available: sender search, date range

**Inbox page — selection and actions**
- R9. Checkboxes allow selecting individual emails, with convenience actions for select-all and select-none
- R10. "Process selected" action triggers processing on checked emails only
- R11. "Reject" action marks emails as dismissed; rejected emails are cached and excluded from future unprocessed views
- R12. Bulk reject available for multiple selected emails

---

## Acceptance Examples

- AE1. **Covers R6, R7.** Given the Inbox page loads with 15 synced emails (10 unprocessed, 5 processed), when viewing the default tab, then only the 10 unprocessed emails are shown. Switching to "all synced" shows all 15.

- AE2. **Covers R9, R10.** Given 8 unprocessed emails are displayed, when user checks 3 specific emails and clicks "Process selected", then only those 3 are sent for processing; the other 5 remain unprocessed.

- AE3. **Covers R11, R12.** Given 8 unprocessed emails are displayed, when user selects 4 and clicks "Reject", then those 4 are marked as dismissed and no longer appear in the unprocessed view. On next visit, they remain excluded.

- AE4. **Covers R4.** Given user is in light mode, when user toggles to dark mode and refreshes the page, then dark mode persists.

---

## Success Criteria

- User can see exactly which emails will be processed before committing to a run
- Rejected emails never resurface in unprocessed views, reducing repeated dismissal work
- Dashboard feels noticeably more polished and professional — comparable to Stripe's aesthetic
- Theme toggle works reliably and preference persists

---

## Scope Boundaries

- MS365 search with staging area for unsynced emails (valuable, deferred to future phase)
- Real-time inbox push notifications or webhooks
- Changes to Pipeline Controls on dashboard (inbox page is additive, not a replacement)
- Mobile-first responsive redesign
- Invoices page visual polish (Phase 1 focuses on dashboard only)

---

## Key Decisions

- **Polish first, then inbox:** Establish the design language on the existing dashboard before building new features, so the inbox page can use consistent patterns from day one
- **Separate page, not modal:** Inbox preview deserves its own `/inbox` route rather than cramming into the dashboard
- **Reject = cached dismissal:** Rejected emails stay in the database with a dismissed status rather than being deleted, preventing re-sync on future searches

---

## Dependencies / Assumptions

- Existing shadcn/ui component library provides the foundation for polish; no new design system adoption required
- The `emails` table can accommodate a dismissed/rejected status (or already has one via the existing dismiss workflow)
- Existing filter parameters (sender, date range) from Pipeline Controls can be reused on the inbox page

---

## Outstanding Questions

### Deferred to Planning

- [Affects R11][Technical] What status value should rejected emails receive? Confirm whether the existing `email_feedback` table or a new column on `emails` is the right approach.
- [Affects R4][Technical] Where should theme preference be stored — localStorage, database, or cookie?
