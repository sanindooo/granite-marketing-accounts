-- Migration 001: initial schema
-- See docs/plans/2026-04-17-001-feat-accounting-assistant-pipeline-plan.md
-- § Technical Approach → ERD for the authoritative data model.

-- ============================================================================
-- Core entity tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS emails (
    msg_id              TEXT PRIMARY KEY,                   -- provider-stable
    source_adapter      TEXT NOT NULL,                      -- ms365|gmail|imap
    message_id_header   TEXT,
    received_at         TEXT NOT NULL,                      -- ISO 8601 UTC
    from_addr           TEXT NOT NULL,
    subject             TEXT NOT NULL,
    processed_at        TEXT,
    classifier_version  TEXT,
    outcome             TEXT,                               -- invoice|receipt|statement|neither|error|pending|duplicate_resend
    error_code          TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_received_at ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_emails_outcome ON emails(outcome);

CREATE TABLE IF NOT EXISTS vendors (
    vendor_id           TEXT PRIMARY KEY,                   -- slug
    canonical_name      TEXT NOT NULL,
    domain              TEXT,
    default_category    TEXT
);
CREATE INDEX IF NOT EXISTS idx_vendors_domain ON vendors(domain);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id             TEXT PRIMARY KEY,
    source_msg_id          TEXT NOT NULL REFERENCES emails(msg_id) ON DELETE RESTRICT,
    vendor_id              TEXT NOT NULL REFERENCES vendors(vendor_id) ON DELETE RESTRICT,
    vendor_name_raw        TEXT NOT NULL,
    invoice_number         TEXT NOT NULL,                   -- synth surrogate if missing
    invoice_date           TEXT NOT NULL,                   -- ISO date
    currency               TEXT NOT NULL,
    amount_net             TEXT,                            -- Decimal as text
    amount_vat             TEXT,
    amount_gross           TEXT NOT NULL,
    amount_gross_gbp       TEXT,                            -- derived via fx
    vat_rate               TEXT,
    vat_number_supplier    TEXT,
    reverse_charge         INTEGER NOT NULL DEFAULT 0,
    category               TEXT NOT NULL,
    category_source        TEXT NOT NULL,                   -- llm|user|rule|hint
    drive_file_id          TEXT,
    drive_web_view_link    TEXT,
    confidence_json        TEXT,                            -- JSON blob
    classifier_version     TEXT NOT NULL,
    hash_schema_version    INTEGER NOT NULL DEFAULT 1,
    is_business            INTEGER,                         -- NULL until matched
    deleted_at             TEXT,
    deleted_reason         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_vendor_number
    ON invoices(vendor_id, invoice_number)
    WHERE invoice_number IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_inv_vendor_date ON invoices(vendor_id, invoice_date);
CREATE INDEX IF NOT EXISTS idx_inv_date ON invoices(invoice_date);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id                   TEXT PRIMARY KEY,
    account                  TEXT NOT NULL,                 -- amex|wise|monzo
    txn_type                 TEXT NOT NULL,                 -- purchase|income|transfer|refund
    booking_date             TEXT NOT NULL,                 -- ISO date, Europe/London civil
    description_raw          TEXT NOT NULL,
    description_canonical    TEXT NOT NULL,
    currency                 TEXT NOT NULL,
    amount                   TEXT NOT NULL,                 -- native Decimal
    amount_gbp               TEXT NOT NULL,                 -- always populated
    fx_rate                  TEXT,
    status                   TEXT NOT NULL DEFAULT 'settled', -- pending|settled|reversed|email_preview
    provider_auth_id         TEXT,
    source                   TEXT NOT NULL,                 -- csv|api|email_parse
    category                 TEXT,                          -- e.g. bank_fee
    hash_schema_version      INTEGER NOT NULL DEFAULT 1,
    deleted_at               TEXT,
    deleted_reason           TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_date_amt ON transactions(booking_date, amount_gbp);
CREATE INDEX IF NOT EXISTS idx_txn_account_status ON transactions(account, status);
CREATE INDEX IF NOT EXISTS idx_txn_pending ON transactions(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_txn_provider_auth ON transactions(provider_auth_id)
    WHERE provider_auth_id IS NOT NULL;

-- ============================================================================
-- Reconciliation — rows own state, links own the physical join
-- ============================================================================

CREATE TABLE IF NOT EXISTS reconciliation_rows (
    row_id             TEXT PRIMARY KEY,
    invoice_id         TEXT REFERENCES invoices(invoice_id) ON DELETE SET NULL,
    txn_id             TEXT REFERENCES transactions(txn_id) ON DELETE SET NULL,
    fiscal_year        TEXT NOT NULL,
    state              TEXT NOT NULL,
    match_score        TEXT NOT NULL,                       -- Decimal
    match_reason       TEXT NOT NULL DEFAULT '',
    user_note          TEXT NOT NULL DEFAULT '',
    cross_fy_flag      INTEGER NOT NULL DEFAULT 0,
    override_history   TEXT NOT NULL DEFAULT '',            -- append-only JSONL
    updated_at         TEXT NOT NULL,
    last_run_id        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recon_fy_state ON reconciliation_rows(fiscal_year, state);
CREATE INDEX IF NOT EXISTS idx_recon_inv ON reconciliation_rows(invoice_id);
CREATE INDEX IF NOT EXISTS idx_recon_txn ON reconciliation_rows(txn_id);

-- Auto-generated rowid serves as primary key; uniqueness enforced
-- via a partial-expression index (SQLite allows expressions in UNIQUE
-- indexes but not in PRIMARY KEY constraints).
CREATE TABLE IF NOT EXISTS reconciliation_links (
    row_id                 TEXT NOT NULL REFERENCES reconciliation_rows(row_id) ON DELETE CASCADE,
    invoice_id             TEXT REFERENCES invoices(invoice_id) ON DELETE SET NULL,
    txn_id                 TEXT REFERENCES transactions(txn_id) ON DELETE SET NULL,
    allocated_amount_gbp   TEXT NOT NULL,
    link_kind              TEXT NOT NULL                    -- full|partial|split_invoice|split_txn|transfer_pair
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_links_triple
    ON reconciliation_links(row_id, COALESCE(invoice_id, ''), COALESCE(txn_id, ''));
CREATE INDEX IF NOT EXISTS idx_links_invoice ON reconciliation_links(invoice_id);
CREATE INDEX IF NOT EXISTS idx_links_txn ON reconciliation_links(txn_id);

-- ============================================================================
-- Pending→settled link table keyed on provider auth IDs
-- ============================================================================

CREATE TABLE IF NOT EXISTS pending_link (
    provider_auth_id   TEXT PRIMARY KEY,
    account            TEXT NOT NULL,
    pending_txn_id     TEXT REFERENCES transactions(txn_id) ON DELETE SET NULL,
    settled_txn_id     TEXT REFERENCES transactions(txn_id) ON DELETE SET NULL,
    first_seen         TEXT NOT NULL,
    settled_at         TEXT,
    ambiguous          INTEGER NOT NULL DEFAULT 0
);

-- ============================================================================
-- FX cache
-- ============================================================================

CREATE TABLE IF NOT EXISTS fx_rates (
    date       TEXT NOT NULL,
    from_ccy   TEXT NOT NULL,
    to_ccy     TEXT NOT NULL,
    rate       TEXT NOT NULL,                               -- Decimal, 6dp
    source     TEXT NOT NULL DEFAULT 'ecb',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, from_ccy, to_ccy)
);

-- ============================================================================
-- Configuration + run-state tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS fiscal_year_sheets (
    fiscal_year      TEXT PRIMARY KEY,
    spreadsheet_id   TEXT NOT NULL,
    drive_folder_id  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    finalized_at     TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    status       TEXT NOT NULL,                             -- running|ok|partial|failed
    stats_json   TEXT NOT NULL DEFAULT '{}',
    cost_gbp     TEXT NOT NULL DEFAULT '0.00'
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);

CREATE TABLE IF NOT EXISTS reauth_required (
    source          TEXT PRIMARY KEY,
    detected_at     TEXT NOT NULL,
    resolved_at     TEXT,
    last_retry_at   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS id_migrations (
    table_name     TEXT NOT NULL,
    old_id         TEXT NOT NULL,
    new_id         TEXT NOT NULL,
    migrated_at    TEXT NOT NULL,
    PRIMARY KEY (table_name, old_id)
);

CREATE TABLE IF NOT EXISTS watermarks (
    source             TEXT PRIMARY KEY,
    last_watermark     TEXT NOT NULL,
    last_success_at    TEXT NOT NULL,
    last_emit_count    INTEGER NOT NULL DEFAULT 0,
    expected_cadence_hours INTEGER NOT NULL DEFAULT 24
);

CREATE TABLE IF NOT EXISTS vendor_category_hints (
    vendor_id         TEXT NOT NULL REFERENCES vendors(vendor_id) ON DELETE CASCADE,
    category          TEXT NOT NULL,
    confirmed_count   INTEGER NOT NULL DEFAULT 0,
    last_confirmed_at TEXT,
    PRIMARY KEY (vendor_id, category)
);
