-- Track when an invoice was last included in a successful PDF export (zip download).
-- NULL means never exported. Set by the web /api/download endpoint per-invoice
-- after archiver finishes reading the entry's stream and the request was not aborted.
--
-- ROLLBACK (SQLite >= 3.35):
--   ALTER TABLE invoices DROP COLUMN last_exported_at;
--   DELETE FROM schema_migrations WHERE version = '009_add_invoice_export_tracking';
-- For older SQLite: rebuild the table via CREATE invoices_new AS SELECT (omit
-- the column), DROP invoices, RENAME invoices_new TO invoices, recreate
-- indexes. Then DELETE the schema_migrations row.
-- Re-apply gotcha: the runner skips files whose version is already in
-- schema_migrations, so the DELETE above is mandatory before re-applying.

ALTER TABLE invoices ADD COLUMN last_exported_at TEXT;
