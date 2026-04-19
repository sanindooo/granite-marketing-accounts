export type Result<T, E = ActionError> =
  | { ok: true; data: T }
  | { ok: false; error: E };

export interface ActionError {
  code: string;
  message: string;
  userMessage?: string;
}

export interface InvoiceRow {
  invoice_id: string;
  source_msg_id: string;
  vendor_id: string;
  vendor_name_raw: string;
  invoice_number: string;
  invoice_date: string;
  currency: string;
  amount_net: string | null;
  amount_vat: string | null;
  amount_gross: string;
  amount_gross_gbp: string | null;
  vat_rate: string | null;
  vat_number_supplier: string | null;
  reverse_charge: number;
  category: string;
  category_source: string;
  drive_file_id: string | null;
  drive_web_view_link: string | null;
  confidence_json: string | null;
  classifier_version: string;
  hash_schema_version: number;
  is_business: number | null;
  deleted_at: string | null;
  deleted_reason: string | null;
  vendor_name?: string;
}

export interface VendorRow {
  vendor_id: string;
  canonical_name: string;
  domain: string | null;
  default_category: string | null;
}

export interface ReconciliationRow {
  row_id: string;
  invoice_id: string | null;
  txn_id: string | null;
  fiscal_year: string;
  state: string;
  match_score: string;
  match_reason: string;
  user_note: string;
  cross_fy_flag: number;
  override_history: string;
  updated_at: string;
  last_run_id: string;
}

export interface RunRow {
  run_id: string;
  started_at: string;
  completed_at: string | null;
  operation: string;
  status: string;
  summary_json: string | null;
  error_message: string | null;
}

export interface CliOutput {
  status: "success" | "error";
  message?: string;
  user_message?: string;
  error_code?: string;
  data?: unknown;
}

export type InvoiceCategory =
  | "software"
  | "travel"
  | "meals"
  | "hardware"
  | "professional"
  | "advertising"
  | "utilities"
  | "other";

export type ReconciliationState =
  | "matched"
  | "unmatched"
  | "pending"
  | "ignored"
  | "split";

export type PipelineCommand = "syncEmails" | "processInvoices" | "runReconciliation";

export interface PipelineOptions {
  fiscalYear?: string;
  limit?: number;
  sender?: string;
  dateFrom?: string;
  dateTo?: string;
  backfillFrom?: string;
  reset?: boolean;
  rescan?: boolean;
  workers?: number;
  model?: "claude" | "openai";
}
