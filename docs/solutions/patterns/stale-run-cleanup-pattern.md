---
date: 2026-04-19
topic: stale-run-cleanup
tags: [architecture, background-jobs, cleanup]
---

# Stale Run Cleanup Pattern

## Problem

Long-running pipeline jobs (email sync, invoice processing) can become stale if:
- Process crashes mid-execution
- User closes terminal/browser
- Network interruption
- System restart

Stale runs show as "running" forever, confusing users and blocking new runs.

## Solution: Threshold-Based Auto-Cleanup

Mark runs as "interrupted" if they've been running longer than a reasonable threshold (e.g., 1 hour for email sync).

### Where to Place Cleanup Logic

**Preferred: CLI command startup (Layer 3)**
```python
def cleanup_stale_runs():
    one_hour_ago = datetime.now() - timedelta(hours=1)
    db.execute("""
        UPDATE runs SET status = 'interrupted'
        WHERE status = 'running' AND started_at < ?
    """, (one_hour_ago.isoformat(),))

@app.command("sync")
def sync_emails():
    cleanup_stale_runs()  # Clean before starting
    # ... rest of command
```

**Why**: 
- Cleanup happens at known entry points
- No polling overhead
- Single responsibility (CLI handles all mutations)
- Easy to audit/log

**Anti-pattern: Cleanup in web read paths**
```typescript
// DON'T DO THIS
function getLastRuns() {
    db.prepare("UPDATE runs SET status = 'interrupted' WHERE ...").run();
    return db.prepare("SELECT * FROM runs").all();
}
```

**Why not**:
- Violates read-only principle for web layer
- Runs on every poll (wasteful)
- Hard to audit/log
- Blurs architectural boundaries

### Threshold Selection

| Job Type | Reasonable Threshold | Rationale |
|----------|---------------------|-----------|
| Email sync | 1 hour | Even large inboxes finish in <30 min |
| Invoice processing | 2 hours | Batch of 500+ invoices with LLM calls |
| Reconciliation | 30 min | Pure DB operations |

### Status Values

```sql
-- Clean status hierarchy
'pending'     -- Queued, not started
'running'     -- In progress
'completed'   -- Finished successfully
'failed'      -- Finished with error
'interrupted' -- Auto-marked as stale
'cancelled'   -- User-cancelled
```

## Displaying Stale Runs

Show running jobs prominently, but with stale indicator:

```typescript
const isStale = (run: Run) => {
    if (run.status !== 'running') return false;
    const started = new Date(run.startedAt);
    const staleThreshold = 60 * 60 * 1000; // 1 hour
    return Date.now() - started.getTime() > staleThreshold;
};

// In UI
{isStale(run) && <Badge variant="warning">May be stale</Badge>}
```

## Recovery

When a run is marked interrupted, users should be able to:
1. See what was partially completed (via stats_json)
2. Re-run the command (idempotent operations preferred)
3. Cancel if truly stuck

## References

- Architecture: `CLAUDE.md` (3-layer architecture)
- Related todo: #016 (remove web layer writes)
