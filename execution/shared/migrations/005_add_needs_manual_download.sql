-- Add needs_manual_download flag for hosted invoice tracking
-- When the processor can't auto-fetch a PDF (e.g., OpenAI billing portal),
-- it sets this flag so the user can manually upload the PDF later.

ALTER TABLE invoices ADD COLUMN needs_manual_download INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invoices ADD COLUMN manual_download_url TEXT;

-- Index for finding invoices needing manual attention
CREATE INDEX IF NOT EXISTS idx_invoices_needs_manual
ON invoices(needs_manual_download)
WHERE needs_manual_download = 1 AND deleted_at IS NULL;
