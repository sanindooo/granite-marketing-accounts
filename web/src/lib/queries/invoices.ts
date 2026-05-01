import { db } from "../db";
import type { InvoiceListRow, InvoiceRow, VendorRow } from "../types";
import { fyBoundsOrAll } from "../fiscal";

function escapeLike(input: string): string {
  return input.replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_");
}

// Trimmed projection for list/table consumers — omits confidence_json and
// other detail-only blobs to keep RSC payloads small. Detail page uses i.*.
const LIST_COLUMNS = `
  i.invoice_id, i.source_msg_id, i.vendor_id, i.vendor_name_raw,
  i.invoice_number, i.invoice_date, i.currency,
  i.amount_net, i.amount_vat, i.amount_gross, i.amount_gross_gbp,
  i.vat_rate, i.category, i.category_source,
  i.drive_file_id, i.drive_web_view_link,
  i.last_exported_at,
  i.deleted_at,
  v.canonical_name as vendor_name
`;

export interface InvoiceFilters {
  fy?: string;
  vendor?: string;
  category?: string;
  status?: "matched" | "unmatched" | "pending" | "all";
  search?: string;
  dateFrom?: string;
  dateTo?: string;
  exported?: "yes" | "no";
  limit?: number;
}

export function getInvoices(filters: InvoiceFilters = {}): InvoiceListRow[] {
  const conditions: string[] = ["i.deleted_at IS NULL"];
  const params: (string | number | null)[] = [];

  if (filters.fy) {
    const bounds = fyBoundsOrAll(filters.fy);
    if (bounds) {
      conditions.push("i.invoice_date BETWEEN ? AND ?");
      params.push(bounds.start, bounds.end);
    }
    // If "all", no date filter is added
  }

  if (filters.vendor) {
    conditions.push("i.vendor_id = ?");
    params.push(filters.vendor);
  }

  if (filters.category) {
    conditions.push("i.category = ?");
    params.push(filters.category);
  }

  if (filters.dateFrom) {
    conditions.push("i.invoice_date >= ?");
    params.push(filters.dateFrom);
  }

  if (filters.dateTo) {
    conditions.push("i.invoice_date <= ?");
    params.push(filters.dateTo);
  }

  if (filters.search) {
    // Prefix-only LIKE so SQLite can use idx_vendors_canonical_name_nocase
    // (migration 012). Leading-wildcard LIKE forces a full table scan, which
    // is what was causing the multi-second hang on "webflow" searches as the
    // invoices table grew. Substring matches now require typing a leading
    // word boundary — acceptable trade for predictable latency.
    const escaped = `${escapeLike(filters.search)}%`;
    conditions.push(
      "(v.canonical_name LIKE ? ESCAPE '\\' COLLATE NOCASE OR i.invoice_number LIKE ? ESCAPE '\\' COLLATE NOCASE)"
    );
    params.push(escaped, escaped);
  }

  if (filters.exported === "yes") {
    conditions.push("i.last_exported_at IS NOT NULL");
  } else if (filters.exported === "no") {
    conditions.push("i.last_exported_at IS NULL");
  }

  if (filters.status && filters.status !== "all") {
    conditions.push(`
      EXISTS (
        SELECT 1 FROM reconciliation_rows r
        WHERE r.invoice_id = i.invoice_id AND r.state = ?
      )
    `);
    params.push(filters.status);
  }

  const limit = filters.limit ?? 500;
  params.push(limit);

  const sql = `
    SELECT ${LIST_COLUMNS}
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE ${conditions.join(" AND ")}
    ORDER BY i.invoice_date DESC
    LIMIT ?
  `;

  return db.prepare(sql).all(...params) as InvoiceListRow[];
}

export function getInvoiceById(invoiceId: string): InvoiceRow | null {
  const sql = `
    SELECT i.*, v.canonical_name as vendor_name
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE i.invoice_id = ?
  `;
  return (db.prepare(sql).get(invoiceId) as InvoiceRow) ?? null;
}

export function getInvoicesByIds(invoiceIds: string[]): InvoiceListRow[] {
  if (invoiceIds.length === 0) return [];
  const placeholders = invoiceIds.map(() => "?").join(",");
  const sql = `
    SELECT ${LIST_COLUMNS}
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE i.invoice_id IN (${placeholders})
  `;
  return db.prepare(sql).all(...invoiceIds) as InvoiceListRow[];
}

export function getVendors(): VendorRow[] {
  return db.prepare("SELECT * FROM vendors ORDER BY canonical_name").all() as VendorRow[];
}

export function getCategories(): string[] {
  const rows = db
    .prepare(
      "SELECT DISTINCT category FROM invoices WHERE deleted_at IS NULL ORDER BY category"
    )
    .all() as { category: string }[];
  return rows.map((r) => r.category);
}

export function getInvoiceCount(fy?: string): number {
  if (fy) {
    const bounds = fyBoundsOrAll(fy);
    if (bounds) {
      const row = db
        .prepare(
          "SELECT COUNT(*) as count FROM invoices WHERE deleted_at IS NULL AND invoice_date BETWEEN ? AND ?"
        )
        .get(bounds.start, bounds.end) as { count: number };
      return row.count;
    }
  }
  const row = db
    .prepare("SELECT COUNT(*) as count FROM invoices WHERE deleted_at IS NULL")
    .get() as { count: number };
  return row.count;
}

export function getExceptionInvoices(fy?: string): InvoiceListRow[] {
  const conditions: string[] = ["i.deleted_at IS NULL"];
  const params: (string | number)[] = [];

  if (fy) {
    const bounds = fyBoundsOrAll(fy);
    if (bounds) {
      conditions.push("i.invoice_date BETWEEN ? AND ?");
      params.push(bounds.start, bounds.end);
    }
  }

  conditions.push(`
    EXISTS (
      SELECT 1 FROM emails e
      WHERE e.msg_id = i.source_msg_id AND e.outcome = 'error'
    )
  `);

  const sql = `
    SELECT ${LIST_COLUMNS}
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE ${conditions.join(" AND ")}
    ORDER BY i.invoice_date DESC
    LIMIT 500
  `;

  return db.prepare(sql).all(...params) as InvoiceListRow[];
}
