-- Explicit domain blocking for email processing
-- User can choose to block all emails from a specific sender domain

CREATE TABLE IF NOT EXISTS blocked_domains (
    domain TEXT PRIMARY KEY,
    blocked_at TEXT NOT NULL DEFAULT (datetime('now')),
    blocked_reason TEXT  -- Optional note from user
);
