-- Add operation tracking for pipeline runs
-- Supports SSE reconnection and last-run queries

-- Add new columns for operation tracking
ALTER TABLE runs ADD COLUMN operation TEXT;
ALTER TABLE runs ADD COLUMN completed_at TEXT;

-- Backfill operation from run_id prefix pattern (e.g., "email-20260418..." -> "ingest_email")
-- This UPDATE is safe to run even if backfill already happened (WHERE operation IS NULL)
UPDATE runs
SET operation = CASE
    WHEN run_id LIKE 'email-%' THEN 'ingest_email'
    WHEN run_id LIKE 'invoice-%' THEN 'ingest_invoice'
    WHEN run_id LIKE 'recon-%' THEN 'reconcile'
    WHEN run_id LIKE 'match-%' THEN 'reconcile'
    WHEN run_id LIKE 'pipeline-%' THEN 'reconcile'
    ELSE 'unknown'
END
WHERE operation IS NULL;

-- Backfill completed_at from ended_at for completed runs
UPDATE runs
SET completed_at = ended_at
WHERE completed_at IS NULL AND ended_at IS NOT NULL;

-- Index for active run lookup (SSE reconnection)
CREATE INDEX IF NOT EXISTS idx_runs_active
ON runs(operation, status)
WHERE status = 'running';

-- Index for dashboard last-runs queries (moved from 003)
-- Used by getLastRuns to quickly find most recent run per operation
CREATE INDEX IF NOT EXISTS idx_runs_op_started
ON runs(operation, started_at DESC);
