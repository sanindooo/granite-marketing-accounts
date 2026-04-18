-- Add operation tracking for pipeline runs
-- Supports SSE reconnection and last-run queries

ALTER TABLE runs ADD COLUMN operation TEXT;
ALTER TABLE runs ADD COLUMN completed_at TEXT;

-- Backfill operation from run_id prefix pattern (e.g., "email-20260418..." -> "ingest_email")
UPDATE runs
SET operation = CASE
    WHEN run_id LIKE 'email-%' THEN 'ingest_email'
    WHEN run_id LIKE 'invoice-%' THEN 'ingest_invoice'
    WHEN run_id LIKE 'recon-%' THEN 'reconcile'
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
