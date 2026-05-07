-- Add needs_manual_download flag to transactions table for reconciliation.
-- When matching finds an email that requires manual portal download (login-gated PDF),
-- this flag is set so the user can see which transactions need manual attention.

ALTER TABLE transactions ADD COLUMN needs_manual_download INTEGER NOT NULL DEFAULT 0;

-- Index for finding transactions needing manual download
CREATE INDEX IF NOT EXISTS idx_txn_needs_manual
ON transactions(needs_manual_download)
WHERE needs_manual_download = 1 AND deleted_at IS NULL;
