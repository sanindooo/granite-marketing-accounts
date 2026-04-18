-- Add indexes for web UI queries

-- Index for status-filtered invoice queries
-- Used by getInvoices when filtering by reconciliation state
CREATE INDEX IF NOT EXISTS idx_recon_inv_state
ON reconciliation_rows(invoice_id, state);

-- Index for dashboard last-runs queries
-- Used by getLastRuns to quickly find most recent run per operation
CREATE INDEX IF NOT EXISTS idx_runs_op_started
ON runs(operation, started_at DESC);
