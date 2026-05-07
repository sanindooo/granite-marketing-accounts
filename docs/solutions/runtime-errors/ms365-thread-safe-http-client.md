---
title: MS365 Adapter Thread-Local HTTP Client Pattern
category: runtime-errors
date: 2026-04-19
tags: [threading, ssl, httpx, thread-safety, concurrent-processing, sigabrt, malloc-error]
components: [execution/adapters/ms365.py]
symptoms: [ssl-threading-crash, sigabrt, malloc-zone-error, worker-thread-crash]
---

# MS365 Adapter Thread-Local HTTP Client Pattern

## Problem Summary

The MS365 email adapter shared a single httpx.Client across ThreadPoolExecutor workers, causing SSL memory corruption and Python crashes during concurrent invoice processing.

## Symptoms

- Python crashed with `SIGABRT` (Abort trap: 6) during invoice processing
- macOS crash report showed `malloc_zone_error` in `ssl3_release_read_buffer`
- Crash occurred intermittently with 5 workers processing emails
- Stack trace showed multiple threads in SSL_read operations simultaneously

## Investigation Steps

1. User reported Python crashes during `granite ingest invoice process --workers 5`
2. Found crash report in `~/Library/Logs/DiagnosticReports/Python-2026-04-19-*.ips`
3. Crash analysis revealed:
   - Exception: `EXC_CRASH / SIGABRT`
   - Trigger: `malloc_zone_error` → `abort()` in `ssl3_release_read_buffer`
   - Thread 2 (faulting) was freeing SSL buffer while other threads read
4. Code review found `Ms365Adapter._http` shared across all worker threads
5. httpx.Client is documented as NOT thread-safe for concurrent requests

## Root Cause Analysis

Original implementation created a single shared HTTP client:

```python
class Ms365Adapter:
    def __init__(self, *, auth, http=None, ...):
        self._http = http  # Single shared instance

    def _client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        self._http = httpx.Client(...)  # Created once, shared by all threads
        return self._http
```

Problems:
1. **Race condition:** Multiple threads calling `_client()` when `_http` is `None`
2. **SSL corruption:** httpx.Client connection pool has SSL contexts that aren't thread-safe
3. **Memory corruption:** One thread freed SSL buffer while another was reading
4. **Cleanup impossible:** No tracking of thread-created clients

The crash occurred because SSL connections were being torn down by one thread while another thread was still reading from the same socket.

## Solution

Implement thread-local HTTP clients with proper cleanup tracking:

**File:** `execution/adapters/ms365.py`

```python
import threading

class Ms365Adapter:
    """MS Graph inbox adapter.

    Thread-safe: each thread gets its own httpx.Client via thread-local storage.
    """

    def __init__(
        self,
        *,
        auth: Ms365Auth,
        http: httpx.Client | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._auth = auth
        self._injected_http = http              # For test injection
        self._batch_size = batch_size
        self._page_size = page_size
        self._local = threading.local()         # Thread-local storage
        self._clients_lock = threading.Lock()   # Protects client list
        self._thread_clients: list[httpx.Client] = []  # Track for cleanup

    def _client(self) -> httpx.Client:
        # Injected client bypasses thread-local (tests)
        if self._injected_http is not None:
            return self._injected_http

        # Check thread-local storage
        client = getattr(self._local, "client", None)
        if client is not None:
            return client

        # Create new client for this thread
        import httpx
        client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0, read=120.0))
        self._local.client = client
        
        # Register for cleanup - thread-safe
        with self._clients_lock:
            self._thread_clients.append(client)
        return client

    def close(self) -> None:
        if self._injected_http is not None:
            self._injected_http.close()
            self._injected_http = None
        # Close ALL thread-local clients
        with self._clients_lock:
            for client in self._thread_clients:
                client.close()
            self._thread_clients.clear()
```

## Design Decisions

1. **`threading.local()`:** Each thread gets isolated httpx.Client via `self._local.client`
2. **Client list with lock:** Tracks all created clients for proper cleanup
3. **Injected client passthrough:** Test fixtures can still inject a single mock client
4. **Lazy import:** `import httpx` inside method avoids import-time dependency

## Verification

1. Run: `granite ingest invoice process --workers 5` for 100+ emails
2. Monitor: No SSL errors or crashes in logs
3. Verify: `adapter.close()` cleans up all thread clients
4. Test: Existing test suite passes (injected client path works)

## Prevention Strategies

### Thread-Safe Adapter Pattern

When using httpx.Client with ThreadPoolExecutor:

```python
import threading

class ThreadSafeAdapter:
    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._clients: list[httpx.Client] = []
    
    def _client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(...)
            self._local.client = client
            with self._lock:
                self._clients.append(client)
        return client
    
    def close(self):
        with self._lock:
            for c in self._clients:
                c.close()
            self._clients.clear()
```

### Pre-flight Checklist for ThreadPoolExecutor

When introducing concurrent workers, verify:

- [ ] All HTTP clients are thread-local or passed per-task
- [ ] All SQLite connections are thread-local
- [ ] Shared mutable state protected by `threading.Lock`
- [ ] Resource cleanup handles all thread-created resources

### Threading Safety Rules

| Resource | Thread-Safe? | Pattern |
|----------|--------------|---------|
| httpx.Client | NO | Use `threading.local()` |
| sqlite3.Connection | NO | Use `threading.local()` |
| Anthropic client | YES | Can share |
| SharedBudget | YES | Has internal Lock |

### Test Case

```python
def test_concurrent_access_no_ssl_crash():
    """Regression: shared httpx.Client crashes under concurrent access."""
    adapter = Ms365Adapter(auth=mock_auth)
    
    def worker(i: int) -> str:
        client = adapter._client()
        return f"worker-{i}-ok"
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(50)]
        results = [f.result() for f in as_completed(futures)]
    
    assert len(results) == 50
    adapter.close()
```

## Related Documentation

- [MS Graph Email Sync Patterns](../patterns/ms-graph-email-sync-patterns.md) - MS365 adapter patterns
- [Parallel Invoice Processing Plan](../../plans/2026-04-19-001-feat-parallel-invoice-processing-plan.md) - thread-safety patterns for SQLite

## Applicability

This pattern applies to any adapter that:
- Uses httpx.Client (or any non-thread-safe HTTP client)
- Is shared across ThreadPoolExecutor workers
- Needs proper resource cleanup

Consider applying to other adapters if concurrent processing is added:
- WiseAdapter
- MonzoAdapter
- OpenAI client wrappers (if not using official thread-safe client)
