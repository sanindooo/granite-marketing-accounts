---
date: 2026-04-19
topic: open-ideation
focus: null
---

# Ideation: Open-Ended Project Improvements

## Codebase Context

**Project Shape:**
- Python 3.11 backend + Next.js 15 dashboard for UK Ltd accounting
- 3-layer architecture: Directives (Markdown SOPs) / Orchestration (AI agent) / Execution (Python scripts)
- Ingests invoices from email (MS365), classifies with Claude, reconciles against banks (Amex/Wise/Monzo), outputs to Google Sheets per fiscal year

**Known Pain Points:**
- cli.py is 84KB monolith (2361 lines)
- Only 3 directives vs rich execution layer
- 15 pending P1-P3 todos (N+1 queries, missing validation)
- Untracked solution docs in docs/solutions/

**Past Learnings:**
- Thread-safety issues with httpx/sqlite3 in ThreadPoolExecutor (fixed in 9177a2c)
- MS Graph pagination: never use $skip, always @odata.nextLink
- Dashboard count mismatch (354 vs 0) due to FY filter divergence
- Agent-native parity: any UI action must have CLI equivalent
- External APIs migrate without warning (frankfurter.app → api.frankfurter.dev)
- Monzo 90-day SCA cliff causes permanent data loss

## Ranked Ideas

### 1. Rules-First Classification Before LLM
**Description:** Add a rules engine that handles known vendor patterns deterministically (regex on subject, sender domain, attachment names) before escalating to Claude. Only novel/ambiguous emails use LLM classification.

**Rationale:** Direct cost reduction. Repeat vendors like Amazon, Google, Stripe send predictable invoices. Regex is free; Claude API credits are not. Budget tracking already exists; this reduces burn rate 70-80% for repeat vendors.

**Downsides:** Rules need maintenance when vendors change email formats. Two code paths (rules vs LLM) to debug.

**Confidence:** 85%
**Complexity:** Medium
**Status:** Unexplored

### 2. Proactive Token Expiry Warnings
**Description:** Before any MS365/Google/Monzo command runs, check token expiry and emit a structured warning if within 14 days of expiration. Surface reauth_required entries at command startup.

**Rationale:** Monzo's 90-day SCA cliff causes permanent data loss. Current healthcheck is passive — users must remember to run it. This prevents mid-workflow auth failures and the catastrophic Monzo cliff.

**Downsides:** Minor startup latency checking token files.

**Confidence:** 90%
**Complexity:** Low
**Status:** Unexplored

### 3. Inline FX Fetching During Invoice Processing
**Description:** Make FX rate fetching synchronous during invoice extraction instead of requiring a separate `granite db backfill-fx` command. Retry 3x with exponential backoff; mark as error only after failure.

**Rationale:** The two-step process exists because FX was retrofitted. Now that FX is integrated, the separate backfill is a manual step users forget. One command should process everything.

**Downsides:** Adds network dependency during processing; offline processing no longer possible.

**Confidence:** 80%
**Complexity:** Low
**Status:** Unexplored

### 4. Dry-Run Reconciliation Mode
**Description:** Add `--dry-run` flag to reconciliation that loads all data, runs matching, and outputs a simulation without committing. Shows what would match, what would remain unmatched, and projected P&L.

**Rationale:** Accounting is high-stakes. Wrong matches are expensive to fix. A preview mode catches errors before they propagate to Google Sheets.

**Downsides:** Slight code duplication for simulation vs real path.

**Confidence:** 85%
**Complexity:** Low
**Status:** Unexplored

### 5. Batch Mode for External API Scripts
**Description:** Audit execution scripts for N+1 API patterns and refactor to batch. Priority targets: MS365 email body fetches, FX rate lookups (batch by date range), Google Drive metadata operations.

**Rationale:** N+1 queries are explicitly a P1-P3 todo. Each unnecessary API call adds latency and rate limit risk. Batching reduces both latency and thread contention.

**Downsides:** Requires per-script audit to identify actual N+1 patterns.

**Confidence:** 75%
**Complexity:** Medium
**Status:** Unexplored

### 6. Delta Sync Health CLI Command
**Description:** Add `granite ops sync-health` that shows MS Graph deltaLink age with warnings before the ~30-day expiration cliff. Proactively warn users that running sync soon preserves incremental mode.

**Rationale:** deltaLink expiration is silent failure — users discover it when they see "31000 scanned" on next run. Warning at day 20 gives 10 days to act.

**Downsides:** One more ops command to remember.

**Confidence:** 80%
**Complexity:** Low
**Status:** Unexplored

### 7. Filter Predicate Single-Source-of-Truth
**Description:** Extract query filter predicates (FY bounds, pending status, deleted flags) into shared constants consumed by both web queries and CLI commands. Add tests verifying dashboard counts match CLI counts.

**Rationale:** The 354 vs 0 pending count mismatch (documented in fx-api-redirect-and-dashboard-fixes.md) was caused by missing FY filter in dashboard. A shared predicate prevents drift.

**Downsides:** Adds cross-layer coupling between TypeScript and Python.

**Confidence:** 75%
**Complexity:** Medium
**Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | CLI decomposition (84KB split) | Already has Typer sub-app structure; file split is aesthetic, not functional |
| 2 | Directive expansion (3→12+) | Premature; write on-demand when agent fails, not preemptively |
| 3 | Auto-generate directives from CLI | CLI docstrings describe flags, not business workflows; wrong abstraction |
| 4 | Directive-to-test generation | Directives are SOPs for agents, not test specs; forced bidirectional sync creates coupling |
| 5 | Solution doc search index | 6 docs total; grep takes <100ms; index is premature optimization |
| 6 | Cross-link solutions to directives | Nice-to-have but low-impact; agents can grep during self-annealing |
| 7 | Scheduled reauth reminders | Proactive warning at command time is sufficient |
| 8 | MS365 email notifications for tokens | Circular dependency: if MS365 token expired, can't send notification |
| 9 | Protocol-based BankAdapter | 3 adapters; formal Protocol adds type-checking overhead for premature abstraction |
| 10 | Generic adapter with pluggable parsers | Bank APIs too different (SCA, OAuth, CSV); false abstraction |
| 11 | FX cache with offline fallback | Rare scenario; introduces stale-rate bugs |
| 12 | Property-based integration tests | Property-based is expensive; happy-path contract tests sufficient |
| 13 | Web layer as orchestration | Contradicts CLI-first architecture in CLAUDE.md |
| 14 | Replace Sheets with native UI | Sheets is free and works; custom UI is months of scope creep |
| 15 | Event-driven daemon with webhooks | Process invoices weekly, not real-time; wildly over-engineered |
| 16 | Remove delta API | Performance regression; delta sync avoids re-processing thousands of emails |
| 17 | Ephemeral reconciliation | Breaks audit trail; accounting needs persistence |
| 18 | Vendor portal sync (headless) | Brittle, maintenance-heavy; manual CSV takes 2 minutes |
| 19 | VAT registry with HMRC validation | <50 vendors; validate manually when adding |
| 20 | Multi-entity support | One company; build when you have two (YAGNI) |
| 21 | Cross-FY reconciliation view | Year-end edge case; query two FYs manually in rare case |
| 22 | Worker crash recovery | Already fixed in commit 9177a2c (thread-local HTTP clients) |
| 23 | Budget-aware resumption | Merged concept; checkpoint visibility covers this |
| 24 | Operation-specific stale timeouts | Already documented in stale-run-cleanup-pattern.md |
| 25 | API health pre-checks | Just let it fail and report error; pre-flight adds complexity |
| 26 | PDF URL extraction in classifier | Regex is deterministic and free; LLM increases cost/latency |
| 27 | SQLite connection pool | Single-user, single-threaded; solves problem you don't have |
| 28 | Directive-to-CLI registry | 3 directives; you know the commands; registry is bureaucracy |
| 29 | CLI/web API parity schema | Fix specific divergence; schema-generation is over-engineering |
| 30 | Domain-expert review roster | Single user is the domain expert; multi-agent review is for teams |

## Session Log
- 2026-04-19: Initial ideation — 48 raw ideas generated across 6 frames, 7 survivors after adversarial filtering
