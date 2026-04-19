---
title: "feat: Parallel Invoice Processing with Model Selection"
type: feat
status: active
date: 2026-04-19
origin: docs/brainstorms/2026-04-19-parallel-invoice-processing-requirements.md
---

# feat: Parallel Invoice Processing with Model Selection

## Overview

Transform the sequential email processing pipeline into a concurrent worker pool pattern, reducing processing time by ~5x. Add model selection (Claude/OpenAI) to give users cost control.

Currently, processing 100 emails takes ~100 serial API round-trips. With 5 concurrent workers, this drops to ~20 batches of parallel work.

## Problem Statement / Motivation

Invoice processing is the slowest part of the pipeline. Each email blocks on:
- MS Graph API calls (fetch body, fetch attachments)
- Claude API calls (classify, extract)
- Google Drive upload
- Database commit

These operations are I/O-bound and independent per email, making them ideal for parallelization.

Additionally, Claude API costs are significant for bulk processing. OpenAI's GPT-4o-mini is ~6x cheaper with comparable accuracy for classification tasks.

## Proposed Solution

1. **Parallel processing**: Wrap `_process_one()` in ThreadPoolExecutor with configurable worker count
2. **Thread-safe budget**: Add `threading.Lock` to budget tracker
3. **Connection-per-worker**: Use `threading.local()` for SQLite connections
4. **Model abstraction**: Create `LLMClient` protocol supporting Claude and OpenAI
5. **CLI flags**: Add `--workers N` and `--model claude|openai`
6. **Web UI controls**: Add workers slider and model dropdown to Pipeline Controls

## Technical Approach

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLI: granite ingest invoice process          │
│                        --workers 5 --model openai                   │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     process_pending_emails()                        │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                  ThreadPoolExecutor(max_workers=N)           │   │
│  │                                                              │   │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐         │   │
│  │  │Worker 1 │  │Worker 2 │  │Worker 3 │  │Worker 4 │  ...    │   │
│  │  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘         │   │
│  │       │            │            │            │               │   │
│  │       ▼            ▼            ▼            ▼               │   │
│  │  _process_one() with thread-local SQLite connection          │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────┐  ┌─────────────────┐                          │
│  │ SharedBudget    │  │ AtomicProgress  │                          │
│  │ (Lock-protected)│  │ (Lock-protected)│                          │
│  └─────────────────┘  └─────────────────┘                          │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         LLMClient Protocol                          │
│  ┌─────────────────────┐       ┌─────────────────────┐             │
│  │    ClaudeClient     │       │    OpenAIClient     │             │
│  │  (existing, wrapped)│       │    (new)            │             │
│  └─────────────────────┘       └─────────────────────┘             │
└─────────────────────────────────────────────────────────────────────┘
```

### Implementation Phases

#### Phase 1: Thread-Safe Budget Tracker (Foundation)

**Goal:** Make budget tracking safe for concurrent workers.

**Files:**
- `execution/shared/budget.py` (new)

**Implementation:**

```python
# execution/shared/budget.py
import threading
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Protocol

@dataclass
class LLMCall:
    """Record of a single LLM API call."""
    model: str
    input_tokens: int
    output_tokens: int
    cost_gbp: Decimal
    stage: str  # "classify" | "extract"

class SharedBudget:
    """Thread-safe budget tracker for concurrent workers."""
    
    def __init__(self, ceiling_gbp: Decimal) -> None:
        self._lock = threading.Lock()
        self._ceiling_gbp = ceiling_gbp
        self._spent_gbp = Decimal("0.0000")
        self._calls: list[LLMCall] = []
    
    def reserve(self, estimated_gbp: Decimal) -> None:
        """Check if estimated cost fits within remaining budget.
        
        Raises BudgetExceededError if ceiling would be exceeded.
        """
        with self._lock:
            if self._spent_gbp + estimated_gbp > self._ceiling_gbp:
                from execution.shared.errors import BudgetExceededError
                raise BudgetExceededError(
                    spent=self._spent_gbp,
                    ceiling=self._ceiling_gbp,
                    requested=estimated_gbp,
                )
    
    def record(self, call: LLMCall) -> None:
        """Record a completed call and update spent amount."""
        with self._lock:
            self._spent_gbp += call.cost_gbp
            self._calls.append(call)
    
    @property
    def spent_gbp(self) -> Decimal:
        with self._lock:
            return self._spent_gbp
    
    @property
    def remaining_gbp(self) -> Decimal:
        with self._lock:
            return self._ceiling_gbp - self._spent_gbp
    
    def stats(self) -> dict:
        with self._lock:
            return {
                "spent_gbp": str(self._spent_gbp),
                "ceiling_gbp": str(self._ceiling_gbp),
                "calls": len(self._calls),
            }
```

**Success criteria:**
- [ ] Budget checks are atomic (no race between check and record)
- [ ] `reserve()` raises `BudgetExceededError` when ceiling exceeded
- [ ] Thread-safe property accessors

---

#### Phase 2: LLM Client Abstraction Layer

**Goal:** Unified interface for Claude and OpenAI APIs.

**Files:**
- `execution/shared/llm_client.py` (new)
- `execution/shared/openai_client.py` (new)
- `execution/shared/claude_client.py` (modify)

**Implementation:**

```python
# execution/shared/llm_client.py
from typing import Protocol, TypeVar
from decimal import Decimal
from pydantic import BaseModel

T = TypeVar('T', bound=BaseModel)

class LLMClient(Protocol):
    """Protocol for LLM API clients."""
    
    def complete(
        self,
        system_prompt: str,
        user_content: str,
        response_model: type[T],
        stage: str,
        max_tokens: int = 4096,
    ) -> tuple[T, "LLMCall"]:
        """Send a completion request and return parsed response + usage."""
        ...
    
    @property
    def budget(self) -> "SharedBudget":
        """Shared budget tracker."""
        ...
```

```python
# execution/shared/openai_client.py
from openai import OpenAI
from decimal import Decimal
from typing import TypeVar, Type
from pydantic import BaseModel

from execution.shared.budget import SharedBudget, LLMCall
from execution.shared.secrets import require

# Pricing per million tokens (April 2026)
GPT4O_MINI_INPUT_COST = Decimal("0.15")  # USD
GPT4O_MINI_OUTPUT_COST = Decimal("0.60")  # USD
USD_TO_GBP = Decimal("0.79")

T = TypeVar('T', bound=BaseModel)

class OpenAIClient:
    """OpenAI API client with budget tracking."""
    
    def __init__(self, budget: SharedBudget, max_retries: int = 5) -> None:
        api_key = require("openai", "api_key")
        self._client = OpenAI(api_key=api_key, max_retries=max_retries)
        self._budget = budget
    
    def complete(
        self,
        system_prompt: str,
        user_content: str,
        response_model: Type[T],
        stage: str,
        max_tokens: int = 4096,
    ) -> tuple[T, LLMCall]:
        # Estimate cost for budget check
        estimated_tokens = len(system_prompt + user_content) // 4
        estimated_cost = self._estimate_cost(estimated_tokens, max_tokens // 2)
        self._budget.reserve(estimated_cost)
        
        # Make API call
        completion = self._client.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format=response_model,
            max_tokens=max_tokens,
        )
        
        # Parse and record
        message = completion.choices[0].message
        if not message.parsed:
            raise ValueError(f"Model refused: {message.refusal}")
        
        usage = completion.usage
        actual_cost = self._calculate_cost(usage.prompt_tokens, usage.completion_tokens)
        
        call = LLMCall(
            model="gpt-4o-mini",
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost_gbp=actual_cost,
            stage=stage,
        )
        self._budget.record(call)
        
        return message.parsed, call
    
    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        input_cost = (Decimal(input_tokens) / 1_000_000) * GPT4O_MINI_INPUT_COST
        output_cost = (Decimal(output_tokens) / 1_000_000) * GPT4O_MINI_OUTPUT_COST
        return (input_cost + output_cost) * USD_TO_GBP
    
    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return self._estimate_cost(input_tokens, output_tokens)
    
    @property
    def budget(self) -> SharedBudget:
        return self._budget
```

**Modify existing ClaudeClient:**
- Inject `SharedBudget` instead of creating its own
- Implement `LLMClient` protocol

**Success criteria:**
- [ ] Both clients implement `LLMClient` protocol
- [ ] Pricing calculations correct for each provider
- [ ] Pydantic structured outputs work with both
- [ ] Budget tracking unified across providers

---

#### Phase 3: Parallel Processing Engine

**Goal:** Process N emails concurrently with ThreadPoolExecutor.

**Files:**
- `execution/invoice/processor.py` (modify)

**Implementation:**

```python
# In processor.py - new parallel processing function

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from execution.shared.db import connect

# Thread-local storage for SQLite connections
_thread_local = threading.local()

def _get_thread_connection(db_path: Path | None) -> sqlite3.Connection:
    """Get or create a SQLite connection for the current thread."""
    if not hasattr(_thread_local, 'conn'):
        _thread_local.conn = connect(db_path)
    return _thread_local.conn


def process_pending_emails_parallel(
    db_path: Path | None,
    *,
    adapter: Ms365Adapter,
    llm_client: LLMClient,
    google: GoogleClients,
    classifier_prompt: LoadedPrompt,
    extractor_prompt: LoadedPrompt,
    tmp_root: Path,
    workers: int = 5,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> ProcessStats:
    """Process pending emails using concurrent workers."""
    
    # Query pending emails (single connection for reads)
    main_conn = connect(db_path)
    emails = list(_pending_emails(main_conn, batch_size=batch_size, limit=limit))
    total = len(emails)
    
    # Thread-safe stats
    stats_lock = threading.Lock()
    stats = ProcessStats()
    completed_count = 0
    
    def process_worker(email_row: EmailRow) -> tuple[Outcome, FiledInvoice | None]:
        """Worker function - runs in thread pool."""
        nonlocal completed_count
        
        # Get thread-local connection
        conn = _get_thread_connection(db_path)
        
        # Process the email
        outcome, invoice = _process_one(
            conn=conn,
            email_row=email_row,
            adapter=adapter,
            llm_client=llm_client,
            google=google,
            classifier_prompt=classifier_prompt,
            extractor_prompt=extractor_prompt,
            http_client=SafeHttpClient(),  # One per worker
            tmp_root=tmp_root,
        )
        
        # Commit per-email (preserves partial progress)
        _update_email_outcome(conn, email_row.msg_id, outcome)
        
        # Update progress atomically
        with stats_lock:
            completed_count += 1
            if on_progress:
                on_progress(completed_count, total, f"Processed: {outcome}")
        
        return outcome, invoice
    
    # Process with thread pool
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_email = {
            executor.submit(process_worker, email): email
            for email in emails
        }
        
        for future in as_completed(future_to_email):
            email = future_to_email[future]
            try:
                outcome, invoice = future.result()
                
                with stats_lock:
                    stats.processed += 1
                    if outcome == "invoice":
                        stats.classified_invoice += 1
                        if invoice and invoice.outcome != FilerOutcome.DUPLICATE_RESEND:
                            stats.filed += 1
                    elif outcome == "receipt":
                        stats.classified_receipt += 1
                        if invoice:
                            stats.filed += 1
                    # ... handle other outcomes
                    
            except BudgetExceededError:
                # Stop submitting new work
                executor.shutdown(wait=False, cancel_futures=True)
                break
            except Exception as e:
                with stats_lock:
                    stats.errors += 1
                    stats.error_details.append({"msg_id": email.msg_id, "error": str(e)})
    
    stats.cost_gbp = llm_client.budget.spent_gbp
    return stats
```

**Success criteria:**
- [ ] Processing time scales ~linearly with worker count (up to API limits)
- [ ] Per-email commits preserved (crash recovery works)
- [ ] Budget exhaustion stops new submissions gracefully
- [ ] Errors in one worker don't crash others

---

#### Phase 4: CLI Integration

**Goal:** Add `--workers` and `--model` flags to CLI.

**Files:**
- `execution/cli.py` (modify)

**Implementation:**

```python
@ingest_invoice_app.command("process")
def ingest_invoice_process(
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    budget: Annotated[str, typer.Option("--budget")] = "2.00",
    backfill: Annotated[bool, typer.Option("--backfill")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    tmp_root: Annotated[Path | None, typer.Option("--tmp")] = None,
    workers: Annotated[int, typer.Option("--workers", help="Number of concurrent workers (1-20).")] = 5,
    model: Annotated[str, typer.Option("--model", help="LLM provider: claude or openai.")] = "claude",
) -> None:
    """Classify, extract, and file pending emails as invoices.
    
    Use --workers to process multiple emails in parallel (default: 5).
    Use --model openai for cheaper bulk processing (GPT-4o-mini).
    """
    # Validate workers range
    if not 1 <= workers <= 20:
        emit_error(ValueError("--workers must be between 1 and 20"))
        return
    
    if model not in ("claude", "openai"):
        emit_error(ValueError("--model must be 'claude' or 'openai'"))
        return
    
    # Initialize shared budget
    budget_gbp = BACKFILL_BUDGET_GBP if backfill else Decimal(budget)
    shared_budget = SharedBudget(ceiling_gbp=budget_gbp)
    
    # Initialize LLM client based on model selection
    if model == "openai":
        from execution.shared.openai_client import OpenAIClient
        llm_client = OpenAIClient(budget=shared_budget)
    else:
        from execution.shared.claude_client import ClaudeClient
        llm_client = ClaudeClient(budget=shared_budget)
    
    # Use parallel processing when workers > 1
    if workers > 1:
        stats = process_pending_emails_parallel(
            db_path,
            adapter=adapter,
            llm_client=llm_client,
            google=google,
            classifier_prompt=classifier_prompt,
            extractor_prompt=extractor_prompt,
            tmp_root=resolved_tmp,
            workers=workers,
            limit=limit,
            on_progress=progress_callback,
        )
    else:
        # Fall back to sequential for workers=1
        stats = process_pending_emails(...)
    
    emit_success({
        "run_id": run_id,
        "workers": workers,
        "model": model,
        "processed": stats.processed,
        # ... rest of stats
    })
```

**Success criteria:**
- [ ] `--workers 5` processes 5 emails concurrently
- [ ] `--model openai` uses GPT-4o-mini
- [ ] Output includes `workers` and `model` in JSON
- [ ] Invalid values emit clear error messages

---

#### Phase 5: Web UI Controls

**Goal:** Expose workers and model settings in Pipeline Controls.

**Files:**
- `web/src/app/api/pipeline/stream/route.ts` (modify)
- `web/src/lib/types.ts` (modify)
- `web/src/app/dashboard/dashboard-content.tsx` (modify)

**Implementation:**

```typescript
// web/src/lib/types.ts - Add to PipelineOptions
export interface PipelineOptions {
  fiscalYear?: string;
  limit?: number;
  workers?: number;      // NEW: 1-20, default 5
  model?: "claude" | "openai";  // NEW: default "claude"
  // ... existing fields
}
```

```typescript
// web/src/app/api/pipeline/stream/route.ts - Add to schema and buildArgs
const commandSchema = z.object({
  command: z.enum(["syncEmails", "processInvoices", "runReconciliation"]),
  fiscalYear: z.string().regex(/^FY-\d{4}-\d{2}$/).optional(),
  limit: z.number().int().min(1).max(100).optional(),
  workers: z.number().int().min(1).max(20).optional(),  // NEW
  model: z.enum(["claude", "openai"]).optional(),        // NEW
});

function buildArgs(command: string, options: PipelineOptions): string[] {
  const args: string[] = [];
  // ... existing args
  
  if (options.workers && command === "processInvoices") {
    args.push("--workers", String(options.workers));
  }
  if (options.model && command === "processInvoices") {
    args.push("--model", options.model);
  }
  
  return args;
}
```

```tsx
// web/src/app/dashboard/dashboard-content.tsx - Add controls
// In the "Show filters" accordion section, near the limit input:

<div className="flex gap-4">
  <div className="flex-1">
    <Label htmlFor="workers">Workers</Label>
    <Input
      id="workers"
      type="number"
      min={1}
      max={20}
      value={options.workers ?? 5}
      onChange={(e) => setOptions({...options, workers: parseInt(e.target.value) || 5})}
    />
    <p className="text-xs text-muted-foreground">Concurrent emails (1-20)</p>
  </div>
  
  <div className="flex-1">
    <Label htmlFor="model">Model</Label>
    <Select
      value={options.model ?? "claude"}
      onValueChange={(v) => setOptions({...options, model: v as "claude" | "openai"})}
    >
      <SelectTrigger>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="claude">Claude (Default)</SelectItem>
        <SelectItem value="openai">OpenAI (Cheaper)</SelectItem>
      </SelectContent>
    </Select>
  </div>
</div>
```

**Success criteria:**
- [ ] Workers slider (1-20) appears in Pipeline Controls
- [ ] Model dropdown (Claude/OpenAI) appears in Pipeline Controls
- [ ] Settings passed to CLI when processing invoices
- [ ] Controls only shown for "Process Invoices" operation

---

#### Phase 6: OpenAI Key Setup

**Goal:** Document and implement OpenAI API key storage.

**Files:**
- `directives/setup.md` (modify)
- `execution/cli.py` (add `ops store-openai-key` command)

**Implementation:**

Add to setup directive:
```markdown
### OpenAI API Key (Optional - for cheaper bulk processing)

```bash
# Store OpenAI API key in Keychain
keyring set granite-accounts/openai api_key
# Paste your OpenAI API key when prompted
```

Or use the CLI:
```bash
granite ops store-key openai
# Follow interactive prompt
```
```

**Success criteria:**
- [ ] OpenAI key stored in Keychain at `granite-accounts/openai`
- [ ] `--model openai` works when key is present
- [ ] Clear error message when key is missing

---

## System-Wide Impact

### Interaction Graph

```
User → Web UI → /api/pipeline/stream → CLI spawn
                                         │
                                         ▼
                              process_pending_emails_parallel()
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
        Worker Thread 1            Worker Thread 2            Worker Thread N
              │                          │                          │
              ▼                          ▼                          ▼
    thread-local SQLite         thread-local SQLite         thread-local SQLite
              │                          │                          │
              └──────────────────────────┼──────────────────────────┘
                                         │
                                         ▼
                                  SharedBudget (Lock)
                                         │
                          ┌──────────────┴──────────────┐
                          ▼                              ▼
                    ClaudeClient                   OpenAIClient
```

### Error & Failure Propagation

| Error Type | Layer | Handling |
|------------|-------|----------|
| `BudgetExceededError` | Worker | Stop new submissions, let in-flight complete |
| `RateLimitError` (429) | LLM Client | SDK auto-retry with exponential backoff |
| `ConfigError` (missing key) | CLI startup | Fail fast before spawning workers |
| Network timeout | Worker | Per-worker retry, mark email as error |
| SQLite lock contention | Worker | WAL mode handles; 30s busy_timeout |

### State Lifecycle Risks

| Risk | Mitigation |
|------|------------|
| Partial batch completion | Per-email commits; crash recovery works |
| Budget overrun with concurrent checks | Atomic reserve() with Lock |
| Orphaned thread-local connections | ThreadPoolExecutor handles cleanup |
| Stale run if process killed | Existing stale-run cleanup at CLI startup |

### API Surface Parity

| Capability | CLI | Web UI |
|------------|-----|--------|
| Set worker count | `--workers N` | Workers input |
| Select model | `--model claude\|openai` | Model dropdown |
| Set budget | `--budget X` | Not exposed (uses default) |
| Set limit | `--limit N` | Limit input (existing) |

---

## Acceptance Criteria

### Functional Requirements

- [ ] `granite ingest invoice process --workers 5` processes 5 emails concurrently
- [ ] `granite ingest invoice process --model openai` uses GPT-4o-mini
- [ ] Web UI workers slider controls concurrency
- [ ] Web UI model dropdown switches providers
- [ ] Budget ceiling enforced across all concurrent workers
- [ ] Per-email commits preserve partial progress on crash

### Non-Functional Requirements

- [ ] Processing time scales ~linearly with workers (up to API rate limits)
- [ ] No data corruption under concurrent writes
- [ ] Clear error messages for missing API keys
- [ ] Progress reporting remains accurate

### Quality Gates

- [ ] Unit tests for `SharedBudget` thread safety
- [ ] Integration test: 10 emails with 5 workers
- [ ] Manual test: Budget exhaustion mid-batch
- [ ] Manual test: Web UI → CLI parameter flow

---

## Success Metrics

| Metric | Baseline (sequential) | Target (5 workers) |
|--------|----------------------|-------------------|
| 50 emails processing time | ~15 minutes | ~3 minutes |
| API cost per 100 emails (Claude) | ~£2.00 | ~£2.00 (same) |
| API cost per 100 emails (OpenAI) | N/A | ~£0.35 |

---

## Dependencies & Prerequisites

- OpenAI API key (stored in Keychain) for `--model openai`
- `openai` Python package added to dependencies
- Existing MS Graph, Claude, and Google integrations working

---

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Race condition in budget tracking | Medium | High | ThreadLock with atomic operations |
| SQLite contention under load | Low | Medium | WAL mode + connection-per-worker |
| OpenAI prompts less accurate | Medium | Medium | Test extraction quality before bulk use |
| Rate limits with high worker count | Low | Low | Default workers=5 is conservative |

---

## Sources & References

### Origin

- **Origin document:** [docs/brainstorms/2026-04-19-parallel-invoice-processing-requirements.md](../brainstorms/2026-04-19-parallel-invoice-processing-requirements.md)
- Key decisions: ThreadPoolExecutor over asyncio, shared budget with Lock, model abstraction layer

### Internal References

- Budget tracking: `execution/shared/claude_client.py:106-133`
- Current processor: `execution/invoice/processor.py:130-234`
- CLI command: `execution/cli.py:728-851`
- Web UI route: `web/src/app/api/pipeline/stream/route.ts`
- Layer separation: `docs/solutions/architecture-issues/layer-separation-enforcement.md`

### External References

- ThreadPoolExecutor: https://docs.python.org/3/library/concurrent.futures.html
- OpenAI Python SDK: https://github.com/openai/openai-python
- OpenAI structured outputs: https://developers.openai.com/api/docs/guides/structured-outputs
