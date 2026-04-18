-- Migration 002: Add partial index for unprocessed emails query
-- Optimizes the _pending_emails() query in processor.py which filters
-- on processed_at IS NULL and orders by received_at.

CREATE INDEX IF NOT EXISTS idx_emails_unprocessed
ON emails(received_at) WHERE processed_at IS NULL;
