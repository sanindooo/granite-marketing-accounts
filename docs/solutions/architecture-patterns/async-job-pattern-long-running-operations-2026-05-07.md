---
title: Async Job Pattern for Long-Running API Operations
date: 2026-05-07
category: architecture-patterns
module: reconciliation
problem_type: architecture_pattern
component: background_job
severity: medium
applies_when:
  - "API operation takes more than 30 seconds"
  - "User should be able to navigate away during processing"
  - "Progress feedback improves UX"
  - "Operation involves external APIs or file processing"
tags:
  - async
  - background-jobs
  - api-design
  - user-experience
  - polling
  - detached-worker
---

# Async Job Pattern for Long-Running API Operations

## Context

When users upload many files (e.g., 100 invoice PDFs) through a web interface, synchronous or SSE-based processing blocks browser navigation. Users cannot use other parts of the application while waiting, creating a poor experience for operations that take more than 30 seconds.

The original SSE-based approach held the HTTP connection open for the entire duration. While it provided real-time progress updates, navigating away killed the connection and lost all progress.

## Guidance

Use the **async job pattern** to decouple long-running operations from the HTTP request lifecycle.

### Architecture Overview

1. **API route** receives request, creates job record, spawns detached worker, returns `jobId` immediately with HTTP 202
2. **Background worker** executes the actual work, updates job record with progress
3. **Frontend** polls job status endpoint, shows toast notifications for progress/completion
4. **User** navigates freely while processing continues

### Implementation

**1. Jobs table (SQLite)**

```sql
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, running, complete, failed
    progress_current INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    progress_message TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

**2. API route (Next.js) — fire and forget**

```typescript
import { spawn } from "child_process";
import { randomUUID } from "crypto";

export async function POST(request: Request) {
  const jobId = `job_${randomUUID().slice(0, 8)}`;
  
  // Create job record
  db.prepare(`
    INSERT INTO jobs (job_id, job_type, status, created_at, updated_at)
    VALUES (?, ?, 'pending', datetime('now'), datetime('now'))
  `).run(jobId, "bulk_upload");

  // Spawn detached worker
  const worker = spawn(pythonPath, ["worker.py", jobId, ...args], {
    cwd: projectRoot,
    detached: true,
    stdio: "ignore",
  });
  worker.unref();  // Allow parent to exit independently

  return new Response(
    JSON.stringify({ status: "accepted", jobId }),
    { status: 202 }  // 202 Accepted, not 200 OK
  );
}
```

**3. Background worker (Python)**

```python
def run_job(job_id: str, file_paths: list[str]):
    conn = get_db_connection()
    
    update_job(conn, job_id, status="running", progress_message="Starting...")
    
    try:
        proc = subprocess.Popen(
            ["python", "cli.py", "process", *file_paths],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Stream progress from stderr (keeps stdout clean for result JSON)
        for line in proc.stderr:
            event = json.loads(line)
            if event.get("event") == "progress":
                update_job(
                    conn, job_id,
                    progress_current=event["current"],
                    progress_total=event["total"],
                    progress_message=event.get("message", "")
                )
        
        proc.wait()
        
        if proc.returncode == 0:
            result = proc.stdout.read()
            update_job(conn, job_id, status="complete", result_json=result)
        else:
            update_job(conn, job_id, status="failed", error_message=proc.stderr.read())
    
    except Exception as e:
        update_job(conn, job_id, status="failed", error_message=str(e))
    finally:
        conn.close()
```

**4. Job status endpoint**

```typescript
// GET /api/jobs/[jobId]/route.ts
export async function GET(request: Request, { params }: { params: { jobId: string } }) {
  const job = db.prepare(`SELECT * FROM jobs WHERE job_id = ?`).get(params.jobId);
  
  if (!job) {
    return new Response(JSON.stringify({ error: "Job not found" }), { status: 404 });
  }
  
  return new Response(JSON.stringify({
    jobId: job.job_id,
    status: job.status,
    progress: { current: job.progress_current, total: job.progress_total },
    message: job.progress_message,
    result: job.result_json ? JSON.parse(job.result_json) : null,
    error: job.error_message,
  }));
}
```

**5. Frontend polling with toast notifications**

```typescript
const activeJobs = new Map<string, { toastId: string }>();

useEffect(() => {
  if (activeJobs.size === 0) return;
  
  const pollInterval = setInterval(async () => {
    for (const [jobId, { toastId }] of activeJobs) {
      const response = await fetch(`/api/jobs/${jobId}`);
      const job = await response.json();
      
      if (job.status === "running") {
        toast.loading(`Processing ${job.progress.current}/${job.progress.total}...`, { id: toastId });
      } else if (job.status === "complete") {
        toast.success(`Processed ${job.result.count} items`, { id: toastId });
        activeJobs.delete(jobId);
        onSuccess?.();
      } else if (job.status === "failed") {
        toast.error(`Failed: ${job.error}`, { id: toastId });
        activeJobs.delete(jobId);
      }
    }
  }, 2000);
  
  return () => clearInterval(pollInterval);
}, [activeJobs.size]);
```

## Why This Matters

- **User freedom**: Users can navigate, start other tasks, even close the tab and return later
- **Resilience**: Polling recovers from network blips; SSE connections drop on navigation
- **Simplicity**: SQLite as message queue avoids Redis/external dependencies at this scale
- **Visibility**: Progress tracking gives users confidence the system is working

The key insight: `spawn()` with `detached: true` plus `worker.unref()` means the child process continues running even after the HTTP response completes. The database becomes the coordination point between the API, worker, and frontend.

## When to Apply

**Use this pattern when:**
- Operation takes more than 30 seconds
- User should be able to navigate away during processing
- Progress feedback improves UX
- Operation involves external APIs or file processing that could fail partway through

**Do not use when:**
- Operation completes in under 5 seconds (just await it)
- Result is needed immediately to render the response
- Simple form submissions with quick database writes

## Examples

### Before: SSE blocking pattern

```typescript
// API holds connection open for entire duration
export async function POST(request: Request) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      for (const file of files) {
        const result = await processFile(file);  // Could take minutes
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(result)}\n\n`));
      }
      controller.close();
    }
  });
  return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
}
```

**Problems:**
- Browser navigation kills the connection and loses progress
- User stuck watching spinner, cannot use other features
- Connection timeout risks on long operations
- No recovery if network drops

### After: Async job pattern

```typescript
// API returns immediately, work happens in background
export async function POST(request: Request) {
  const jobId = createJob();
  spawnDetachedWorker(jobId, files);
  return new Response(JSON.stringify({ jobId }), { status: 202 });
}
```

**Benefits:**
- User closes dialog, continues using app
- Progress visible via toast notifications
- Can navigate away and return — job status persists
- Worker handles its own cleanup and error recovery

## Related

- `docs/solutions/patterns/stale-run-cleanup-pattern.md` — cleanup for jobs stuck in "running" state
- `docs/solutions/architecture-issues/layer-separation-enforcement.md` — web spawns CLI for mutations
