# Agent Instructions

> This file is mirrored across CLAUDE.md, AGENTS.md, and GEMINI.md so the same instructions load in any AI environment.

You operate within a 3-layer architecture that separates concerns to maximize reliability. LLMs are probabilistic, whereas most business logic is deterministic and requires consistency. This system fixes that mismatch.

## The 3-Layer Architecture

**Layer 1: Directive (What to do)**
- Basically just SOPs written in Markdown, live in `directives/`
- Define the goals, inputs, tools/scripts to use, outputs, and edge cases
- Natural language instructions, like you'd give a mid-level employee

**Layer 2: Orchestration (Decision making)**
- This is you. Your job: intelligent routing.
- Read directives, call execution tools in the right order, handle errors, ask for clarification, update directives with learnings
- You're the glue between intent and execution. E.g you don't try scraping websites yourself—you read `directives/scrape_website.md` and come up with inputs/outputs and then run `execution/scrape_single_site.py`

**Layer 3: Execution (Doing the work)**
- Deterministic Python scripts in `execution/`
- Environment variables, api tokens, etc are stored in `.env`
- Handle API calls, data processing, file operations, database interactions
- Reliable, testable, fast. Use scripts instead of manual work.

**Why this works:** if you do everything yourself, errors compound. 90% accuracy per step = 59% success over 5 steps. The solution is push complexity into deterministic code. That way you just focus on decision-making.

## Operating Principles

**1. Check for tools first**
Before writing a script, check `execution/` per your directive. Only create new scripts if none exist.

**2. Self-anneal when things break**
- Read error message and stack trace
- Fix the script and test it again (unless it uses paid tokens/credits/etc—in which case you check w user first)
- Update the directive with what you learned (API limits, timing, edge cases)
- Example: you hit an API rate limit → you then look into API → find a batch endpoint that would fix → rewrite script to accommodate → test → update directive.

**3. Update directives as you learn**
Directives are living documents. When you discover API constraints, better approaches, common errors, or timing expectations—update the directive. But don't create or overwrite directives without asking unless explicitly told to. Directives are your instruction set and must be preserved (and improved upon over time, not extemporaneously used and then discarded).

## Self-annealing loop

Errors are learning opportunities. When something breaks:
1. Fix it
2. Update the tool
3. Test tool, make sure it works
4. Update directive to include new flow
5. If the learning is reusable beyond this one directive, write a solution doc in `docs/solutions/` so future plans can find it
6. System is now stronger

## File Organization

**Deliverables vs Intermediates:**
- **Deliverables**: Google Sheets, Google Slides, or other cloud-based outputs that the user can access
- **Intermediates**: Temporary files needed during processing

**Directory structure:**
- `.tmp/` - All intermediate files (dossiers, scraped data, temp exports). Never commit, always regenerated.
- `execution/` - Python scripts (the deterministic tools)
- `directives/` - SOPs in Markdown (the instruction set)
- `docs/plans/` - Feature implementation plans (created by compound engineering workflow)
- `docs/solutions/` - Documented learnings and fixes (searchable by future plans)
- `docs/brainstorms/` - Early-stage thinking and exploration
- `todos/` - Ephemeral review findings (per-PR, not committed)
- `.env` - Environment variables and API keys
- `credentials.json`, `token.json` - Google OAuth credentials (required files, in `.gitignore`)

**Key principle:** Local files are only for processing. Deliverables live in cloud services (Google Sheets, Slides, etc.) where the user can access them. Everything in `.tmp/` can be deleted and regenerated.

## Compound Engineering (Optional Workflow)

This project has the Every Compound Engineering plugin installed. It provides a Plan → Work → Review → Compound workflow for building new features. All commands are interactive—they pause and ask for user input at key decision points. Nothing gets written to disk without confirmation.

**When to use it:**
- Building new features or significant enhancements → use `/workflows:plan` first
- Brainstorming an approach → use `/workflows:brainstorm`
- After completing a feature → use `/workflows:compound` to record learnings
- Before merging → use `/workflows:review` for multi-agent code review

**When NOT to use it:**
- Running existing directives (just follow the directive)
- Quick fixes, config changes, one-line patches
- Executing existing scripts in `execution/`
- When the user explicitly says to skip it

**How it connects to the 3-layer architecture:**
- Plans in `docs/plans/` should reference which directives and execution scripts they'll use or create
- Solutions in `docs/solutions/` complement directive updates—directives get the operational fix, solutions get the broader learning
- The self-annealing loop still applies: fix → update tool → test → update directive. Compound adds: → also write a solution doc if the learning is reusable

**On first run:** There are no existing docs/plans/solutions yet. That's fine—the plugin creates directories on demand. The first `/workflows:plan` will research the codebase (including `directives/` and `execution/`) and produce the first plan. Learnings accumulate from there.

**Configuration:** See `compound-engineering.local.md` at the project root for review agent roster and project-specific context passed to all review agents.

## Execution Script Standards

When creating scripts in `execution/`, follow these standards. They were learned across multiple build phases where the same vulnerability classes kept recurring because scripts were written from scratch without a hardened template.

### Agent-Native Output (Must-Have)

Scripts called by agents must produce deterministic, parseable output on all code paths:

- All success paths emit structured JSON to stdout
- All error paths emit JSON with `status`, `error_code`, `message` to stdout before `sys.exit(1)`
- Exit codes: 0 on success, non-zero on error
- Zero results is a valid success: emit `{"status": "success", "count": 0}`, not silence
- If composable (subcommands calling each other): suppress intermediate stdout so only one JSON document per execution

### Security (Must-Have)

- **Path sandboxing**: All file path arguments must resolve under `PROJECT_ROOT/.tmp/`. Validate with `Path.resolve()` before any file operation.
- **SSRF protection**: Validate URLs before HTTP requests — block private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.0.0/16`) and non-HTTP schemes.
- **Formula injection**: Sanitize strings before writing to Google Sheets — prefix with `'` if value starts with `=`, `+`, `-`, or `@`.
- **Injection at substitution boundaries**: Validate ALL interpolated values at the point of substitution, regardless of source (user data, API responses, AI-generated content).
- **File permissions**: Write sensitive files (tokens, PII) with `0o600` via `os.open()` + `os.fdopen()`, not default umask.
- **Response size limits**: Enforce `MAX_RESPONSE_SIZE` on all external HTTP responses.
- **External API response validation**: Validate response schema before consuming — never trust that required keys exist.

### Performance (Should-Have)

- **Hoist expensive resources**: Create API clients, DB connections, HTTP sessions once and pass as parameters — not inside per-item loops.
- **Regex at module level**: Compile all regex patterns as module-level constants, not inside functions.
- **Batch API calls**: Collect all data first, then make one API call — not one call per item.
- **TTL for caches**: Any file that accumulates entries (seen URLs, processed IDs) needs a TTL-based pruning mechanism.

### API Integration Patterns

- **Rate limiting**: Use explicit retry counters in `while` loops — never `continue` in `for` loops for retries.
- **Budget tracking**: Track API credit usage through state files. Add `--dry-run` flags for cost-free testing.
- **Mock mode**: Every script that calls a paid API should support `--mock` for testing without API cost.
- **Incremental saves**: For paid API operations, save results after each item to prevent data loss on crash.
- **Batch discovery**: During self-annealing, look for batch endpoints that replace N+1 individual calls.

## Database Migrations — Hard Rules

`execution/shared/migrations/*.sql` files are **immutable once committed**. Every applied migration's SHA-256 is stored in `schema_migrations`, and `apply_migrations` refuses to start if any byte changed. That includes comments, whitespace, and trailing newlines — the runner cannot tell SQL from prose.

- **Never edit any existing file in `execution/shared/migrations/`.** Not the SQL, not the comments. If you need to change something, write a new `0NN_*.sql`.
- **Rollback notes belong in `execution/shared/migrations/README.md`**, not inside the migration files. The README is not checksummed.
- **`tests/test_migrations_immutable.py` pins every committed hash.** The pre-commit hook (`.githooks/pre-commit`, enabled via `git config core.hooksPath .githooks`) runs that test before any commit lands. If it fails, the only correct fix is to revert the file — don't update the pinned hash unless the file was genuinely never applied to any database (rare).

This rule exists because we hit it: editing a deployed migration to add a "ROLLBACK:" comment block wedged the local pipeline with a checksum mismatch the runner couldn't auto-recover from. The historical incident is in the README.

## Summary

You sit between human intent (directives) and deterministic execution (Python scripts). Read instructions, make decisions, call tools, handle errors, continuously improve the system.

Be pragmatic. Be reliable. Self-anneal.