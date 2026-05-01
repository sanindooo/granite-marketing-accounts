-- Persist the error message text so the Needs Attention dashboard can
-- show *why* an email errored, not just the error_code category.
--
-- All writes MUST go through `execution.shared.error_message.prepare_error_message`
-- which redacts secrets (Bearer tokens, signed URLs, emails) and truncates
-- to ERROR_MESSAGE_CAP (2000) chars. SQLite TEXT has no native length limit
-- and ALTER TABLE … ADD CHECK is unsupported, so the cap is enforced at
-- the application layer; do not bypass the helper.
--
-- ROLLBACK (SQLite >= 3.35):
--   ALTER TABLE emails DROP COLUMN error_message;
--   DELETE FROM schema_migrations WHERE version = '010_add_email_error_message';
-- For older SQLite: rebuild emails_new without the column, swap, recreate
-- indexes, then DELETE the schema_migrations row.
-- Re-apply gotcha: schema_migrations row must be deleted first or the
-- runner skips the file as already-applied.

ALTER TABLE emails ADD COLUMN error_message TEXT;
