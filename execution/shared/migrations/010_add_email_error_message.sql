-- Persist the error message text so the Needs Attention dashboard can
-- show *why* an email errored, not just the error_code category.
-- Truncated to 2000 chars on write (see _update_email_outcome).

ALTER TABLE emails ADD COLUMN error_message TEXT;
