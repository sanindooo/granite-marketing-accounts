-- Add indexes for web UI queries

-- Index for status-filtered invoice queries
-- Used by getInvoices when filtering by reconciliation state
CREATE INDEX IF NOT EXISTS idx_recon_inv_state
ON reconciliation_rows(invoice_id, state);

-- Note: idx_runs_op_started moved to 004_add_run_operation.sql
-- because it depends on the operation column added there.
