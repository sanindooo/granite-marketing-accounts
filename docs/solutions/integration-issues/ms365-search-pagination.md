---
title: MS Graph $search on /me/messages forbids manual $skip — must follow @odata.nextLink
category: integration-issues
date: 2026-05-01
tags: [ms-graph, ms365, pagination, search, http-400, vendor-search]
components: [execution/adapters/ms365.py]
symptoms: [ms-graph-400-on-vendor-rescan, sync-fails-immediately, scanning-flaky]
---

# MS Graph `$search` on `/me/messages` is incompatible with manual `$skip`

## Problem Summary

Clicking **Sync emails** with vendor=Webflow + Re-scan toggled on produced an immediate "MS Graph returned unexpected status 400" toast. The CLI process exited; nothing was synced. This had been intermittent across the project's history.

## Root Cause

`search_inbox(sender="Webflow")` built `$search="from:webflow"` plus a manually-incremented `$skip` parameter:

```python
# old
while pages_fetched < max_pages:
    params = {**base_params, "$skip": str(pages_fetched * self._page_size)}
    response = client.get(url, params=params, headers=headers)
```

Per MS Graph docs ([api-reference/v1.0/api/user-list-messages.md](https://learn.microsoft.com/en-us/graph/api/user-list-messages)):

> "Do not try to extract the `$skip` value from the `@odata.nextLink` URL to manipulate responses. This API uses the `$skip` value to keep count of all the items it has gone through in the user's mailbox to return a page of message-type items."

In other words, Graph's internal `$skip` is **its own scan cursor** (how many items it has examined while applying `$search`), not a page offset. Sending `$skip = page_count * page_size` lies to the server and causes 400 responses on subsequent pages — and sometimes immediately. The exact 400 message body says `InvalidRequest: $skip is not supported with $search`.

The `$filter`-only path (date range, no sender) does not have this problem because Graph's mailbox-scan model maps cleanly onto `$skip`. There is a separate documented bug where `@odata.nextLink` with a datetime-`$filter` returns duplicates and misses results, which is why commit `f9eccfe` originally moved everything to manual `$skip`. That fix was correct for the `$filter` case but should not have been applied to the `$search` case.

## Fix

Branch the pagination strategy on which path is in play:

- **`$search` path** (sender filter): follow `@odata.nextLink` verbatim. Never construct `$skip`. Set `ConsistencyLevel: eventual` on **every** request — the header is not auto-carried into nextLink follow-ups.
- **`$filter` path** (date-only): keep manual `$skip` paging plus the dedup safety net (preserves the f9eccfe workaround).

```python
next_url: str | None = base_url if use_search else None

while pages_fetched < max_pages:
    if use_search:
        if next_url is None:
            break
        if next_url == base_url:
            request_url, request_params = base_url, base_params
        else:
            request_url, request_params = next_url, {}  # nextLink carries params
    else:
        request_url = base_url
        request_params = {**base_params, "$skip": str(pages_fetched * page_size)}

    response = client.get(request_url, params=request_params, headers=headers)
    # ... process payload ...

    if use_search:
        next_url = payload.get("@odata.nextLink")
        if next_url is None:
            break
```

## Reusable Learnings

### 1. MS Graph pagination is endpoint-dependent and parameter-dependent

The same endpoint `/me/messages` accepts manual `$skip` for `$filter` queries but rejects it for `$search` queries. Don't assume a single pagination strategy works for one endpoint. Always read the per-API docs section on optional query parameters and **specifically the paging notes**.

### 2. `ConsistencyLevel: eventual` does not carry forward

Per docs:
> "The ConsistencyLevel header required for advanced queries against directory objects isn't included by default in subsequent page requests. It must be set explicitly in subsequent pages."

Same applies to `/me/messages` `$search`. Set the header on **every** call when `$search` is in play.

### 3. Generic error surfaces hide the real Graph error

The original `_raise_for_graph_status` correctly attached the Graph response body to the exception's `details["body"]`, but only the top-level `f"MS Graph returned unexpected status {status}"` made it to the UI toast. The body — which contained the precise diagnostic ("`$skip is not supported with $search`") — was only visible in the dev server's stderr.

Future work: surface the Graph `error.code` and `error.message` in user-facing toasts when safe.

## Test Coverage Added

Eleven tests pin the contract in `tests/test_ms365.py::TestSearchInboxSenderPath` and `::TestSearchInboxFilterPath`:

- `test_first_request_uses_search_param_with_normalised_value` — sender normalisation + ConsistencyLevel header set
- `test_paginates_via_nextLink_not_manual_skip` — multi-page nextLink follow, no manual $skip
- `test_consistency_level_header_set_on_every_page` — header carried explicitly into follow-ups
- `test_terminates_when_no_nextLink` — single-page responses end cleanly
- `test_400_response_raises_schema_violation_with_body` — Graph error body is preserved
- `test_dedupes_repeated_msg_ids_across_pages` — safety net against Graph echoing duplicates
- `test_filters_dates_in_python_when_combined_with_search` — date filter applied in Python (HTTP request never carries `$filter` alongside `$search`)
- `test_respects_max_pages` — infinite-loop guard
- `test_webflow_rescan_regression` — direct reproduction: handler returns 400 if `$skip` + `$search` are sent together; new code passes
- `test_filter_only_uses_skip_pagination` — preserves the f9eccfe workaround for date-only queries
- `test_filter_only_does_not_set_consistency_level_header` — header is opt-in to the $search path
- `test_no_filters_returns_recent_inbox_with_orderby` — sanity check for the no-filter case

## Related solutions

- `docs/solutions/runtime-errors/ms365-thread-safe-http-client.md` — separate MS365 reliability fix (concurrent HTTP client SSL crashes).
- `docs/solutions/integration-issues/invoice-export-and-link-pdf-fixes.md` — the four-bug PR that uncovered this; Bug 4 (HTML body extraction) only matters once sync actually delivers Webflow emails to the processor.

## References

- [MS Graph: user-list-messages](https://learn.microsoft.com/en-us/graph/api/user-list-messages) — paging note about `$skip` semantics.
- [MS Graph: query-parameters](https://learn.microsoft.com/en-us/graph/query-parameters) — `$search` constraints; ConsistencyLevel not auto-carried.
- Project history: commit `f9eccfe fix(ms365): use $skip pagination to avoid duplicate emails` — original motivation for `$skip` in the `$filter` path.
