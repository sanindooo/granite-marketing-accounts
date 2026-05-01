-- Vendor search uses LIKE on vendors.canonical_name. Without a NOCASE-
-- collation index SQLite cannot index a LIKE comparison even with a
-- prefix-only pattern, so every keystroke scans the joined (invoices,
-- vendors) rowset. Add the index that makes the prefix LIKE plan use the
-- index for O(log n) lookup. invoices.invoice_number is rarely searched and
-- the existing idx_invoices on invoice_date covers the common workload —
-- skip a second FTS table until invoice search becomes the hot path.
--
-- Rollback: DROP INDEX IF EXISTS idx_vendors_canonical_name_nocase;
CREATE INDEX IF NOT EXISTS idx_vendors_canonical_name_nocase
  ON vendors(canonical_name COLLATE NOCASE);
