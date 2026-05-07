import { db } from "../db";
import { fyBoundsOrAll } from "../fiscal";

export interface TransactionListRow {
  txnId: string;
  account: string;
  bookingDate: string;
  descriptionCanonical: string;
  currency: string;
  amount: string;
  amountGbp: string;
  state: string | null;
  invoiceId: string | null;
  needsManualDownload: boolean;
}

export interface ReconciliationFilters {
  fy?: string;
  state?: string;
  account?: string;
  needsManualDownload?: boolean;
  search?: string;
}

export interface ReconciliationCounts {
  total: number;
  unmatched: number;
  matched: number;
  needsManualDownload: number;
}

const VALID_STATES = [
  "unmatched",
  "suggested",
  "auto_matched",
  "user_verified",
  "user_personal",
  "user_ignore",
] as const;

export function getTransactions(
  filters: ReconciliationFilters
): TransactionListRow[] {
  const conditions: string[] = ["t.deleted_at IS NULL", "t.status = 'settled'"];
  const params: (string | number)[] = [];

  if (filters.fy) {
    const fyRange = fyBoundsOrAll(filters.fy);
    if (fyRange) {
      conditions.push("DATE(t.booking_date) >= ? AND DATE(t.booking_date) <= ?");
      params.push(fyRange.start, fyRange.end);
    }
  }

  if (filters.state) {
    if (filters.state === "unmatched") {
      conditions.push(
        "(r.state IS NULL OR r.state = 'unmatched' OR r.state = 'suggested')"
      );
    } else if (VALID_STATES.includes(filters.state as typeof VALID_STATES[number])) {
      conditions.push("r.state = ?");
      params.push(filters.state);
    }
  }

  if (filters.account) {
    conditions.push("t.account = ?");
    params.push(filters.account);
  }

  if (filters.needsManualDownload) {
    conditions.push("t.needs_manual_download = 1");
  }

  if (filters.search) {
    conditions.push("t.description_canonical LIKE ?");
    params.push(`%${filters.search.toUpperCase()}%`);
  }

  const whereClause =
    conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  const rows = db
    .prepare(
      `
      SELECT
        t.txn_id,
        t.account,
        t.booking_date,
        t.description_canonical,
        t.currency,
        t.amount,
        t.amount_gbp,
        r.state,
        r.invoice_id,
        t.needs_manual_download
      FROM transactions t
      LEFT JOIN reconciliation_rows r ON r.txn_id = t.txn_id
      ${whereClause}
      ORDER BY t.booking_date DESC
      LIMIT 500
      `
    )
    .all(...params) as {
    txn_id: string;
    account: string;
    booking_date: string;
    description_canonical: string;
    currency: string;
    amount: string;
    amount_gbp: string;
    state: string | null;
    invoice_id: string | null;
    needs_manual_download: number;
  }[];

  return rows.map((row) => ({
    txnId: row.txn_id,
    account: row.account,
    bookingDate: row.booking_date,
    descriptionCanonical: row.description_canonical,
    currency: row.currency,
    amount: row.amount,
    amountGbp: row.amount_gbp,
    state: row.state,
    invoiceId: row.invoice_id,
    needsManualDownload: row.needs_manual_download === 1,
  }));
}

export function getReconciliationCounts(fy?: string): ReconciliationCounts {
  const conditions: string[] = ["t.deleted_at IS NULL", "t.status = 'settled'"];
  const params: (string | number)[] = [];

  if (fy) {
    const fyRange = fyBoundsOrAll(fy);
    if (fyRange) {
      conditions.push("DATE(t.booking_date) >= ? AND DATE(t.booking_date) <= ?");
      params.push(fyRange.start, fyRange.end);
    }
  }

  const whereClause =
    conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  const result = db
    .prepare(
      `
      SELECT
        COUNT(*) as total,
        COUNT(*) FILTER (
          WHERE r.state IS NULL OR r.state IN ('unmatched', 'suggested')
        ) as unmatched,
        COUNT(*) FILTER (
          WHERE r.state IN ('auto_matched', 'user_verified')
        ) as matched,
        COUNT(*) FILTER (
          WHERE t.needs_manual_download = 1
        ) as needs_manual_download
      FROM transactions t
      LEFT JOIN reconciliation_rows r ON r.txn_id = t.txn_id
      ${whereClause}
      `
    )
    .get(...params) as {
    total: number;
    unmatched: number;
    matched: number;
    needs_manual_download: number;
  };

  return {
    total: result.total || 0,
    unmatched: result.unmatched || 0,
    matched: result.matched || 0,
    needsManualDownload: result.needs_manual_download || 0,
  };
}

export function getAccounts(): string[] {
  const rows = db
    .prepare(
      `
      SELECT DISTINCT account
      FROM transactions
      WHERE deleted_at IS NULL
      ORDER BY account
      `
    )
    .all() as { account: string }[];

  return rows.map((row) => row.account);
}
