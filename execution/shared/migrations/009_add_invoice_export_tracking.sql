-- Track when an invoice was last included in a successful PDF export (zip download).
-- NULL means never exported. Set by the web /api/download endpoint per-invoice
-- after archiver finishes reading the entry's stream and the request was not aborted.

ALTER TABLE invoices ADD COLUMN last_exported_at TEXT;
