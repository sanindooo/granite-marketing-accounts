-- Add partial index for active invoices by date
-- Improves dashboard metrics query performance by filtering on deleted_at IS NULL
-- before scanning invoice_date range.

CREATE INDEX IF NOT EXISTS idx_inv_active_date
ON invoices(invoice_date)
WHERE deleted_at IS NULL;
