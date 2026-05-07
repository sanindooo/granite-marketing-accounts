---
date: 2026-04-19
topic: railway-deployment
---

# Railway Cloud Deployment

## Problem Frame

The accounting assistant system works well locally but needs cloud deployment for:
- Access from any device without running local services
- Scheduled pipeline runs (email ingestion, reconciliation) without manual intervention
- Foundation for eventual SaaS product

## Requirements

- R1. Deploy Next.js frontend and Python CLI to Railway as a single service (or two coordinated services)
- R2. Persist SQLite database across deployments using Railway persistent volume
- R3. Move secrets from macOS Keychain to Railway environment variables (MS365, Google OAuth, Anthropic/OpenAI keys)
- R4. Schedule recurring pipeline runs (daily email sync, invoice processing)
- R5. Expose the web dashboard publicly with basic auth or similar protection

## Success Criteria

- Dashboard accessible from any browser without local setup
- `granite reconcile run` executes successfully on Railway
- Pipeline runs automatically on schedule without manual triggers
- Data persists across redeploys

## Scope Boundaries

- NOT migrating from SQLite to Turso/Postgres (deferred to SaaS phase)
- NOT implementing multi-tenancy or user accounts
- NOT setting up custom domain (can use Railway's default subdomain initially)

## Key Decisions

- **Railway monolith over split architecture**: Simpler deployment, minimal code changes. Turso migration is straightforward later since it's SQLite-wire-compatible.
- **Persistent volume for SQLite**: Railway supports this; avoids DB migration overhead now.
- **Secrets in env vars**: Standard cloud pattern; Keychain code needs conditional path for cloud vs local.

## Dependencies / Assumptions

- Railway account already exists
- OAuth refresh tokens can be bootstrapped locally then stored as env vars (or re-authed via Railway's console if needed)
- better-sqlite3 compiles on Railway's Linux environment (should work, but verify)

## Outstanding Questions

### Deferred to Planning

- [Affects R1][Technical] Single Dockerfile with both Node + Python, or two separate Railway services?
- [Affects R3][Needs research] How to handle OAuth flows that require browser redirect (Monzo, Google) in headless cloud environment?
- [Affects R4][Technical] Railway cron jobs vs external scheduler (e.g., GitHub Actions triggering Railway endpoint)?
- [Affects R5][Technical] Basic auth at Nginx/middleware level, or Railway's built-in auth if available?

## Next Steps

→ `/ce:plan` for structured implementation planning
