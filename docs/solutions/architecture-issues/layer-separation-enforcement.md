---
date: 2026-04-19
topic: layer-separation-enforcement
tags: [architecture, 3-layer, mutations, server-actions, agent-native]
related:
  - ../patterns/stale-run-cleanup-pattern.md
  - ../patterns/ms-graph-email-sync-patterns.md
---

# Layer Separation Enforcement

Three related architectural violations discovered during the invoice web UI feature work, all stemming from confusion about the 3-layer architecture boundaries.

## The 3-Layer Architecture

Per CLAUDE.md:
- **Layer 1 (Directives)**: SOPs in Markdown defining what to do
- **Layer 2 (Orchestration)**: LLM agent making decisions, calling tools
- **Layer 3 (Execution)**: Deterministic Python scripts handling all mutations

The web UI sits between users and Layer 3. It should be a **read-only view** that triggers CLI commands for mutations.

---

## Issue 1: Web Layer Performing Database Writes

### Problem

The web layer (`dashboard.ts`) had UPDATE statements for stale run cleanup embedded in read functions:

```typescript
// BAD: getLastRuns() was doing writes
export function getLastRuns(): LastRun[] {
  // This UPDATE was inside a "read" function
  const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  db.prepare(
    `UPDATE runs SET status = 'interrupted'
     WHERE status = 'running' AND started_at < ?`
  ).run(oneHourAgo);  // <-- WRITE in a read path
  
  // Then the actual read...
}
```

### Root Cause

Pragmatic shortcut: "The cleanup needs to happen before displaying data, and the DB connection is right here." This violates separation because:
- Web layer accumulates write responsibilities
- Same cleanup logic gets duplicated in CLI
- Audit logging happens in CLI but not web

### Solution

Remove UPDATEs from web queries. CLI already handles cleanup at command startup via `_begin_run()`:

```python
# execution/cli.py - cleanup happens at CLI layer
def _begin_run(conn, *, kind: str, operation: str) -> str:
    cleaned = _cleanup_stale_runs(conn, operation=operation)
    if cleaned > 0:
        emit_progress("cleanup", cleaned, cleaned, f"Cleaned up {cleaned} stale run(s)")
    # ... create new run
```

Web layer is now read-only:

```typescript
// GOOD: getLastRuns() is read-only
export function getLastRuns(): LastRun[] {
  // Read-only query; stale cleanup happens in CLI layer
  const rows = db.prepare(`...SELECT...`).all();
  // ...
}
```

---

## Issue 2: Server Actions Fetching Localhost

### Problem

The `cancelRun` server action was making an HTTP request to its own server:

```typescript
// BAD: Server action fetching localhost
export async function cancelRun(operation: string) {
  const response = await fetch("http://localhost:3000/api/pipeline/cancel", {
    method: "POST",
    body: JSON.stringify({ operation }),
  });
  return response.json();
}
```

### Root Cause

Treating server actions like browser code. Server actions run server-side and have direct access to the database—they don't need HTTP round-trips to themselves.

### Why This Breaks

- **Port hardcoding**: Fails if server runs on different port
- **Self-loop overhead**: Unnecessary network round-trip
- **Production deployment**: May have different hostname/port
- **Error handling**: HTTP errors obscure underlying DB errors

### Solution

Server actions should call query functions directly:

```typescript
// GOOD: Direct import
export async function cancelRun(operation: string) {
  const { cancelRunningJobs } = await import("@/lib/queries/dashboard");
  const cancelled = cancelRunningJobs(operation);
  return { ok: true, data: { cancelled } };
}
```

---

## Issue 3: CLI Missing UI Capabilities (Agent-Native Parity)

### Problem

Web UI could cancel runs and list running jobs, but CLI couldn't. This breaks the agent-native principle: **any action a user can take via UI, an agent should be able to take via CLI**.

### Root Cause

Feature added to web first without considering CLI parity. The 3-layer architecture requires CLI-first development.

### Solution

Added `granite runs` command group:

```bash
granite runs list              # List recent runs
granite runs list --running    # Show only running jobs  
granite runs cancel <op>       # Cancel running jobs for operation
granite runs status <run_id>   # Get detailed run status
granite runs cleanup           # Clean up stale runs
```

All commands output JSON for agent consumption:

```json
{"status": "success", "count": 3, "runs": [...]}
```

---

## Prevention Strategies

### For Web Layer Writes

1. **Grep gate**: `grep -r "\.run\(" web/src/lib/queries/` should return zero results
2. **Code review**: Any UPDATE/INSERT/DELETE in web/src/ requires explicit justification
3. **Convention**: `lib/queries/` = read-only, mutations via CLI subprocess

### For Server Action Anti-Patterns

1. **Lint rule**: Flag `fetch("http://localhost` in files with `"use server"`
2. **Pattern**: Server actions call `lib/queries/` for reads, spawn CLI for writes
3. **Detection**: `grep -r "fetch.*localhost" web/src/lib/actions/` should be empty

### For Agent-Native Parity

1. **CLI-first**: Implement new capabilities in CLI first
2. **Feature matrix**: Maintain table mapping CLI commands to web UI equivalents
3. **Checklist**: Before adding UI feature, verify CLI command exists

---

## Checklist for New Features

When adding a web UI feature:

- [ ] Does equivalent CLI command exist?
- [ ] Does CLI command output JSON?
- [ ] Does web layer spawn CLI or call read-only queries?
- [ ] If web needs to write, is CLI subprocess used?
- [ ] No `fetch(localhost)` in server actions?
- [ ] No UPDATE/INSERT/DELETE in `lib/queries/`?

---

## Related Documentation

- [Stale Run Cleanup Pattern](../patterns/stale-run-cleanup-pattern.md) — Where cleanup logic belongs
- [MS Graph Email Sync Patterns](../patterns/ms-graph-email-sync-patterns.md) — API-specific patterns
- CLAUDE.md — The 3-layer architecture definition
