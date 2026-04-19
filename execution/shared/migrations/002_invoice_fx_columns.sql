-- Migration 002: Add FX audit columns to invoices
-- Supports automatic currency conversion during invoice processing

ALTER TABLE invoices ADD COLUMN fx_rate_used TEXT;
ALTER TABLE invoices ADD COLUMN fx_error TEXT;

CREATE INDEX IF NOT EXISTS idx_invoices_fx_error ON invoices(fx_error) WHERE fx_error IS NOT NULL;
