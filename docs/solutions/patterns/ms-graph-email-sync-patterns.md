---
date: 2026-04-19
topic: ms-graph-email-sync
tags: [ms365, api, pagination, delta-sync]
---

# MS Graph Email Sync Patterns

## Problem

MS Graph API has multiple pagination and sync mechanisms that behave differently. Using the wrong approach leads to:
- Iterating through entire inbox (31K+ emails) when only fetching recent ones
- Missing emails during backfill operations
- Confusing progress messages that don't reflect actual work

## Key Patterns

### 1. Delta Sync vs Search Mode

**Delta Sync** (`/messages/delta`):
- Returns only emails since last watermark
- Efficient for incremental sync
- Returns `@odata.deltaLink` when exhausted (this IS the watermark)
- First delta query with no watermark returns ALL messages (paginated)

**Search Mode** (`/messages?$filter=receivedDateTime ge ...`):
- Returns emails matching filter criteria
- Good for one-off searches or backfill
- Does NOT provide a watermark for future delta sync

### 2. Pagination: $skip vs @odata.nextLink

**NEVER use $skip for MS Graph**:
- MS Graph doesn't support `$skip` reliably
- Returns 400 errors or wrong results

**ALWAYS use @odata.nextLink**:
```python
while True:
    response = client.get(url)
    yield from response.get("value", [])
    
    next_link = response.get("@odata.nextLink")
    if not next_link:
        break
    url = next_link
```

### 3. Backfill Pattern

When backfilling historical emails AND establishing delta sync:

```python
# Phase 1: Search for historical emails
for msg in search_messages(from_date=backfill_date):
    save_email(msg)

# Phase 2: Run delta sync to get watermark
# This MUST iterate through everything to get the deltaLink
for msg in delta_sync(skip_save=True):  # Already have these from Phase 1
    pass  # Just iterating to reach deltaLink

# Now we have a watermark for future incremental syncs
```

**Why Phase 2 iterates everything**: The deltaLink (watermark) is only returned AFTER consuming all pages. There's no shortcut.

### 4. Progress Messaging

Be explicit about what's happening:

```python
# Bad: "Scanned 31000 emails, 0 new"
# User thinks: "Why did it scan 31K if I asked for recent emails?"

# Good: "Backfill complete. Scanning inbox for delta watermark... (31000 scanned)"
# User understands: "Oh, it needs to do this to set up incremental sync"
```

## Gotchas

1. **First delta with no watermark = full inbox**: If you've never synced, the first delta query returns ALL messages. Plan for this.

2. **deltaLink expires**: After ~30 days of inactivity, deltaLink may expire. Handle gracefully by falling back to full sync.

3. **Deleted messages in delta**: Delta sync returns tombstones for deleted messages. Check for `@removed` property.

4. **Batch size**: MS Graph returns ~10-50 messages per page by default. Use `$top=999` for larger batches (max 999 for messages).

## References

- MS Graph delta query: https://learn.microsoft.com/en-us/graph/delta-query-messages
- Pagination: https://learn.microsoft.com/en-us/graph/paging
