---
date: 2026-04-19
topic: parallel-invoice-processing
---

# Parallel Invoice Processing

## Problem Frame

Invoice processing is strictly sequential — each email waits for the previous one to fully complete before starting. For a batch of 100 emails, this means ~100 serial API round-trips to MS Graph, 100-200 Claude API calls, and ~100 Google Drive uploads. Processing takes minutes when it could take seconds.

The `process_pending_emails` function in `execution/invoice/processor.py` iterates through emails one at a time, blocking on each email's full pipeline (fetch body → classify → fetch attachments → extract → categorize → file → commit).

## Requirements

- R1. Process multiple emails concurrently using a worker pool pattern
- R2. Add `--workers N` flag to `granite ingest invoice process` to control concurrency level (default: 5)
- R3. Preserve per-email commit semantics so partial progress survives crashes
- R4. Maintain budget enforcement across concurrent workers (shared budget tracker)
- R5. Report progress accurately during parallel execution (processed count, not just started count)
- R6. Handle rate limit errors gracefully with backoff, without crashing the entire batch
- R7. Expose workers setting in the web UI (Pipeline Controls section)
- R8. Add model selection (`--model claude|openai`) to switch between Claude and OpenAI for classification/extraction
- R9. Expose model selection in the web UI alongside the workers setting

## Success Criteria

- Processing 50 emails with `--workers 5` completes in ~1/5 the time of sequential processing
- No regression in data correctness (same invoices filed, same extractions)
- Budget ceiling still enforced — workers stop when budget exhausted
- Progress reporting remains useful (shows actual completions, not just starts)
- OpenAI mode achieves comparable extraction accuracy at lower cost
- Web UI controls work end-to-end (workers slider, model dropdown → CLI → processing)

## Scope Boundaries

- Not implementing MS Graph $batch API (separate optimization, lower ROI)
- Not implementing pipeline parallelism within a single email (pre-fetching while classifying)
- Not adding async/await throughout — using ThreadPoolExecutor for minimal code change
- Not rewriting all prompts for OpenAI — use same prompts with minor format adjustments
- Web UI changes limited to Pipeline Controls section (workers input, model dropdown)

## Key Decisions

- **ThreadPoolExecutor over asyncio**: The existing codebase is synchronous. ThreadPoolExecutor wraps existing `_process_one` with minimal refactoring. Asyncio would require rewriting all I/O paths.
- **Shared budget tracker with thread-safe updates**: The budget tracker needs thread-safe increment. Use a lock around budget updates.
- **Default workers = 5**: Conservative default that stays within MS Graph (10k requests/10min) and API rate limits. User can increase via flag.
- **Graceful degradation on rate limits**: If a worker hits a rate limit, back off and retry. Don't fail the entire batch.
- **Model abstraction layer**: Create a unified interface that wraps both Claude and OpenAI clients. The classifier and extractor prompts work with either model.
- **OpenAI as cost-effective default for bulk processing**: OpenAI (GPT-4o-mini or GPT-4o) is significantly cheaper than Claude for classification/extraction tasks. User can choose per-run.

## Dependencies / Assumptions

- MS Graph API can handle 5-10 concurrent requests per user without rate limiting
- Claude API rate limits (typically 60+ requests/min) can handle 5-10 concurrent classification/extraction calls
- SQLite can handle concurrent writes from multiple threads (WAL mode should already be enabled)
- OpenAI API key will be stored in Keychain alongside Claude key
- OpenAI models (GPT-4o-mini, GPT-4o) can handle the same extraction prompts with comparable accuracy

## Outstanding Questions

### Deferred to Planning

- [Affects R4][Technical] Should we use `threading.Lock` or `queue.Queue` for coordinating budget checks?
- [Affects R6][Needs research] What are the exact Claude API rate limits for Haiku vs Sonnet? May affect default worker count.
- [Affects R5][Technical] How should progress callbacks work with concurrent processing? Atomic counter with periodic reporting?
- [Affects R8][Technical] Which OpenAI models to use? GPT-4o-mini for classification, GPT-4o for extraction?
- [Affects R8][Technical] How to handle prompt format differences between Claude and OpenAI? Same prompts or model-specific variants?
- [Affects R7, R9][Technical] How should the web UI pass workers/model to the CLI? Query params to the API route that spawns the process?

## Next Steps

→ `/ce:plan` for structured implementation planning
