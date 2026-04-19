-- Add dismissal tracking for emails
-- Allows users to mark false positives as "not invoice" and clear from Needs Attention

ALTER TABLE emails ADD COLUMN dismissed_at TEXT;
ALTER TABLE emails ADD COLUMN dismissed_reason TEXT;  -- 'not_invoice', 'resolved', 'duplicate'

-- Index for fast filtering of non-dismissed pending actions
CREATE INDEX IF NOT EXISTS idx_emails_pending_not_dismissed
ON emails(outcome, dismissed_at)
WHERE outcome IN ('needs_manual_download', 'error', 'no_attachment') AND dismissed_at IS NULL;

-- Store dismissal feedback for learning
CREATE TABLE IF NOT EXISTS email_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT NOT NULL REFERENCES emails(msg_id),
    feedback_type TEXT NOT NULL,  -- 'dismiss', 'confirm_invoice', 'reclassify'
    feedback_value TEXT NOT NULL, -- 'not_invoice', 'is_invoice', etc.
    from_addr TEXT NOT NULL,      -- Denormalized for pattern learning
    subject TEXT NOT NULL,        -- Denormalized for pattern learning
    sender_domain TEXT,           -- Extracted domain for vendor matching
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_email_feedback_domain
ON email_feedback(sender_domain);
