-- When an invoice URL extracted from an email body can't be auto-fetched
-- (login-gated host, expired URL, 4xx from upstream), persist the URL on
-- the email so the Needs Attention dashboard can render it as a clickable
-- link. The user clicks → logs in / downloads → uploads the PDF via the
-- existing Upload PDF button.
--
-- Distinct from invoices.manual_download_url (migration 005) which only
-- gets populated AFTER an invoices row exists — we need a place to land
-- the URL when no invoices row was ever created.

ALTER TABLE emails ADD COLUMN manual_download_url TEXT;
