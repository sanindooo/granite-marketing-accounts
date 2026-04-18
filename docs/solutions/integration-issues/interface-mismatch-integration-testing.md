---
title: "Invoice Pipeline Integration Bug Fixes"
category: integration-issues
date: 2026-04-18
tags:
  - ms365
  - invoice-processing
  - pydantic
  - sqlite
  - sql-injection
  - date-parsing
  - type-safety
  - performance
components:
  - execution/invoice/processor.py
  - execution/adapters/ms365.py
  - execution/cli.py
  - execution/shared/migrations
severity: high
---

## Problem Description

During end-to-end testing of the invoice processing pipeline (MS365 → classify → extract → file to Drive), multiple integration bugs surfaced. The bugs clustered at module boundaries where data crosses from one domain to another.

**Symptoms:**
- `ValueError: Invalid isoformat string: '2026-04-17 11:56:04+00:00'`
- `TypeError: ExtractorInput.__init__() got an unexpected keyword argument 'pdf_base64'`
- `AttributeError: 'FieldConfidence' object has no attribute 'get'`
- `TypeError: file_invoice() got an unexpected keyword argument 'google'`
- MS Graph 400 error on attachment fetch
- SQL injection vulnerability in LIMIT clause
- LIKE wildcards bypassing vendor search filter
- Full table scan on pending emails query

## Root Cause Analysis

The bugs share three underlying root causes:

### 1. Interface Mismatch Between Module Boundaries

Scripts were written with assumptions about function signatures that didn't match reality. Development was incremental — extractor and filer modules built before processor — and the processor author relied on mental model of the API instead of verifying actual signatures.

### 2. Pydantic Model vs. Dict Mental Model Confusion

The codebase mixes Pydantic models with raw dicts from API responses. When working with multiple data layers (raw HTTP JSON, Pydantic validation, SQLite rows), it's easy to forget which layer you're in.

### 3. External API Format Assumption Violations

MS Graph returns non-standard datetime format. OData annotations look like fields but aren't selectable. SQL wildcards pass through LIKE without escaping.

## Solution

### 1. Date Parsing Normalization

MS Graph returns space-separated timestamps instead of ISO 8601's `T` separator.

```python
def _parse_received_date(received_at: str) -> date:
    """Handles both "T" and space-separated formats from MS Graph."""
    normalized = received_at.replace(" ", "T").replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).date()
```

### 2. Interface Alignment

Verify actual class signatures, not mental models.

```python
# Correct ExtractorInput construction
extractor_input = ExtractorInput(
    subject=email_row.subject,
    sender=email_row.from_addr,
    source_text=source_text,
    email_received_date=received_date,
)

# Correct file_invoice call (positional, not keyword)
filed = file_invoice(google, conn, filer_input)
```

### 3. Pydantic Attribute Access

Pydantic models use attribute access, not dict `.get()`.

```python
# Wrong: dict-style access
extraction.field_confidence.get("invoice_number", 0.0)

# Correct: attribute access
extraction.field_confidence.invoice_number
```

### 4. OData Annotation Handling

`@odata.type` is a response annotation, not a selectable field.

```python
# Wrong: include @odata.type in $select
params={"$select": "id,name,contentType,size,@odata.type"}

# Correct: read from response body after fetch
params={"$select": "id,name,contentType,size"}
odata_type = raw.get("@odata.type", "")
```

### 5. SQL Parameterization

Never interpolate values into SQL, even for LIMIT.

```python
# Wrong: f-string interpolation
query += f" LIMIT {limit}"

# Correct: parameterized query
query += " LIMIT ?"
params = (limit,)
cursor = conn.execute(query, params)
```

### 6. LIKE Wildcard Escaping

Escape user input before LIKE searches.

```python
escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
query += " WHERE name LIKE ? ESCAPE '\\'"
params = [f"%{escaped}%"]
```

### 7. Partial Index for NULL Filtering

Add index that matches the WHERE clause exactly.

```sql
CREATE INDEX IF NOT EXISTS idx_emails_unprocessed
ON emails(received_at) WHERE processed_at IS NULL;
```

### 8. Regex Hoisting to Module Level

Compile patterns once at module load, not per invocation.

```python
_PDF_URL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"https?://[^\s<>\"']+\.pdf\b",
        r"https?://pay\.stripe\.com/[^\s<>\"']+",
    ]
)
```

## Prevention Strategies

### Interface Drift

- Use `@dataclass(frozen=True, slots=True)` for DTOs — immutability surfaces errors earlier
- Run `mypy --strict` on interface boundaries
- Define DTOs in shared `types.py` that both caller and callee import

### Dict vs. Object Confusion

- Use `model_dump()` explicitly when dict access is needed
- After Pydantic validation, only use `model.field_name`, never `model.get()`
- Annotate function parameters with `dict[str, Any]` vs `SomeModel` to make distinction clear

### External API Responses

- Wrap all external API parsing in dedicated `_parse_*` functions
- Always use `.get()` with defaults: `raw.get("field", "")`
- Document known API quirks in module docstrings

### SQL Security

- Enable ruff rule `S608` (catches f-string SQL)
- Establish project convention: ALL SQL uses `?` placeholders
- Type-validate at boundaries before passing to execute()

### Database Performance

- Document "hot path" queries in migration comments
- Run `EXPLAIN QUERY PLAN` in tests for critical queries
- Use partial indexes for common filters (e.g., `WHERE x IS NULL`)

## Key Insight

**When integrating multiple systems (APIs, databases, AI models), bugs cluster at boundaries where data crosses from one domain to another.**

Validate/normalize at every boundary crossing:

1. **API responses:** Normalize datetime formats, validate schema before consuming
2. **Function calls:** Verify signatures in actual module, not mental model
3. **Data models:** Know whether you're handling Pydantic model (attributes) or dict (`.get()`)
4. **SQL queries:** Parameterize ALL interpolated values; escape wildcards for LIKE
5. **Performance:** Hoist expensive operations to module level; add indexes for WHERE patterns

## Files Changed

- `execution/invoice/processor.py` — Date parsing, interface alignment, SQL parameterization, regex hoisting
- `execution/adapters/ms365.py` — Attachment fetch @odata.type fix, dead code removal
- `execution/cli.py` — LIKE escaping, date exception handling
- `execution/shared/migrations/002_add_unprocessed_index.sql` — Partial index

## Related

- Plan: `docs/plans/2026-04-17-001-feat-accounting-assistant-pipeline-plan.md`
- Execution script standards: `CLAUDE.md` (SQL parameterization, regex hoisting requirements)
