import { db } from "../db";
import type { InvoiceRow, VendorRow } from "../types";
import { fyBounds } from "../fiscal";

function escapeLike(input: string): string {
  return input.replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_");
}

export interface InvoiceFilters {
  fy?: string;
  vendor?: string;
  category?: string;
  status?: "matched" | "unmatched" | "pending" | "all";
  search?: string;
  dateFrom?: string;
  dateTo?: string;
  limit?: number;
}

export function getInvoices(filters: InvoiceFilters = {}): InvoiceRow[] {
  const conditions: string[] = ["i.deleted_at IS NULL"];
  const params: (string | number | null)[] = [];

  if (filters.fy) {
    const { start, end } = fyBounds(filters.fy);
    conditions.push("i.invoice_date BETWEEN ? AND ?");
    params.push(start, end);
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
    const escaped = `%${escapeLike(filters.search)}%`;
    conditions.push(
      "(v.canonical_name LIKE ? ESCAPE '\\' OR i.invoice_number LIKE ? ESCAPE '\\')"
    );
    params.push(escaped, escaped);
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
    SELECT i.*, v.canonical_name as vendor_name
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE ${conditions.join(" AND ")}
    ORDER BY i.invoice_date DESC
    LIMIT ?
  `;

  return db.prepare(sql).all(...params) as InvoiceRow[];
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

export function getInvoicesByIds(invoiceIds: string[]): InvoiceRow[] {
  if (invoiceIds.length === 0) return [];
  const placeholders = invoiceIds.map(() => "?").join(",");
  const sql = `
    SELECT i.*, v.canonical_name as vendor_name
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE i.invoice_id IN (${placeholders})
  `;
  return db.prepare(sql).all(...invoiceIds) as InvoiceRow[];
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
    const { start, end } = fyBounds(fy);
    const row = db
      .prepare(
        "SELECT COUNT(*) as count FROM invoices WHERE deleted_at IS NULL AND invoice_date BETWEEN ? AND ?"
      )
      .get(start, end) as { count: number };
    return row.count;
  }
  const row = db
    .prepare("SELECT COUNT(*) as count FROM invoices WHERE deleted_at IS NULL")
    .get() as { count: number };
  return row.count;
}

export function getExceptionInvoices(fy?: string): InvoiceRow[] {
  const conditions: string[] = ["i.deleted_at IS NULL"];
  const params: (string | number)[] = [];

  if (fy) {
    const { start, end } = fyBounds(fy);
    conditions.push("i.invoice_date BETWEEN ? AND ?");
    params.push(start, end);
  }

  conditions.push(`
    EXISTS (
      SELECT 1 FROM emails e
      WHERE e.msg_id = i.source_msg_id AND e.outcome = 'error'
    )
  `);

  const sql = `
    SELECT i.*, v.canonical_name as vendor_name
    FROM invoices i
    LEFT JOIN vendors v ON i.vendor_id = v.vendor_id
    WHERE ${conditions.join(" AND ")}
    ORDER BY i.invoice_date DESC
    LIMIT 500
  `;

  return db.prepare(sql).all(...params) as InvoiceRow[];
}
