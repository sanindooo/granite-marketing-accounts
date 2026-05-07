---
title: Railway Cloud Deployment
type: feat
status: deferred
date: 2026-04-19
origin: docs/brainstorms/2026-04-19-railway-deployment-requirements.md
---

# Railway Cloud Deployment

## Overview

Deploy the accounting assistant (Next.js dashboard + Python CLI) to Railway as a monolith with persistent SQLite storage. This is a personal-use deployment with architecture decisions that allow future SaaS migration.

**Note:** This plan is intentionally loose. The codebase will continue to evolve before deployment. At execution time, analyze the repository's current state to determine exact implementation details.

## Key Decisions (from origin)

- **Railway monolith** over split Vercel/Railway architecture — simpler, fewer moving parts
- **SQLite with persistent volume** — defer Turso migration to SaaS phase
- **Secrets in env vars** — move from macOS Keychain to Railway environment variables
- **Bootstrap OAuth locally** — Google/Monzo require browser callbacks; auth locally, copy tokens to Railway

## High-Level Approach

### Phase 1: Containerization

Create a Dockerfile that runs both services:
- Python 3.11+ for the `granite` CLI
- Node.js 20+ for the Next.js dashboard
- Single container with supervisor/process manager, OR two Railway services sharing a volume

**At execution time, analyze:**
- Current Python dependencies in `pyproject.toml`
- Node dependencies in `web/package.json` (especially native modules like `better-sqlite3`)
- Whether the system has grown to warrant separate services

### Phase 2: Secrets Abstraction

Modify `execution/shared/secrets.py` to support env var fallback on non-darwin platforms:
- Keep Keychain for local dev (darwin)
- Read from `GRANITE_*` env vars on Linux/Railway

**At execution time, analyze:**
- Current secrets namespace and key structure
- Which adapters are in use and their credential requirements
- Any new auth flows added since this plan

### Phase 3: OAuth Token Bootstrap

Document a workflow for initial OAuth setup:
1. Run auth flows locally (MS365 device flow, Google InstalledAppFlow, Monzo callback)
2. Export tokens to env vars or files
3. Configure Railway to use those tokens

**At execution time, analyze:**
- Which OAuth flows are actually in use
- Token refresh mechanisms and expiry patterns
- Whether any flows have been converted to service accounts

### Phase 4: Database Persistence

Configure Railway persistent volume:
- Mount at `/data` or similar
- Set `GRANITE_DB=/data/pipeline.db`
- Ensure migrations run on startup

**At execution time, analyze:**
- Current migration state and any new migrations
- WAL mode compatibility with Railway's filesystem
- Backup/restore requirements

### Phase 5: Scheduled Runs

Set up recurring pipeline execution:
- Railway cron job, OR
- External trigger (GitHub Actions calling Railway endpoint)

**At execution time, analyze:**
- Which pipeline commands need scheduling
- Frequency requirements
- Error notification preferences

### Phase 6: Web UI Protection

Add basic auth or similar to protect the dashboard:
- Middleware-level auth, OR
- Railway's built-in auth if available

## Pre-Deployment Checklist

Before executing this plan, verify:

- [ ] System is feature-complete enough for cloud use
- [ ] All adapters (MS365, Monzo, Wise, Amex) working locally
- [ ] Invoice processing pipeline stable
- [ ] Reconciliation engine producing correct output
- [ ] Dashboard showing accurate data

## Known Gotchas (from docs/solutions/)

1. **localhost fetches in server actions** — grep for `fetch.*localhost` and fix before deploying
2. **Thread-safe HTTP clients** — ensure `httpx.Client` uses `threading.local()` pattern for concurrent workers
3. **Stale run cleanup** — container restarts leave orphaned "running" jobs; auto-cleanup exists but verify it works
4. **httpx redirect handling** — use `follow_redirects=True` explicitly for external APIs

## Migration Path to SaaS

When ready to support multiple users:
1. Replace SQLite with Turso (libSQL) — wire-compatible, minimal query changes
2. Add user authentication (Clerk, Auth.js, etc.)
3. Tenant isolation in database schema
4. Split into separate services if load requires

## Success Criteria

- Dashboard accessible from any browser without local setup
- `granite reconcile run` executes successfully on Railway
- Pipeline runs automatically on schedule
- Data persists across redeploys

## Sources

- **Origin document:** [docs/brainstorms/2026-04-19-railway-deployment-requirements.md](../brainstorms/2026-04-19-railway-deployment-requirements.md)
- **Learnings:** Thread-safe HTTP clients (`docs/solutions/runtime-errors/ms365-thread-safe-http-client.md`), layer separation (`docs/solutions/architecture-issues/layer-separation-enforcement.md`)
