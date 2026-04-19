import { db } from "../db";
import { fyBounds } from "../fiscal";

export interface DashboardMetrics {
  invoiceCount: number;
  totalSpend: number;
  reconStatus: { state: string; count: number }[];
  categoryBreakdown: { category: string; total: number }[];
  topVendors: { name: string; total: number }[];
  pendingEmails: number;
}

export function getDashboardMetrics(fy: string): DashboardMetrics {
  const { start, end } = fyBounds(fy);

  const result = db
    .prepare(
      `
    WITH invoice_totals AS (
      SELECT COUNT(*) as count,
             COALESCE(SUM(CAST(amount_gross_gbp AS REAL)), 0) as total
      FROM invoices
      WHERE deleted_at IS NULL AND invoice_date BETWEEN ? AND ?
    ),
    recon_status AS (
      SELECT state, COUNT(*) as count
      FROM reconciliation_rows
      WHERE fiscal_year = ?
      GROUP BY state
    ),
    category_breakdown AS (
      SELECT category, SUM(CAST(amount_gross_gbp AS REAL)) as total
      FROM invoices
      WHERE deleted_at IS NULL AND invoice_date BETWEEN ? AND ?
      GROUP BY category
      ORDER BY total DESC
    ),
    top_vendors AS (
      SELECT v.canonical_name as name, SUM(CAST(i.amount_gross_gbp AS REAL)) as total
      FROM invoices i
      JOIN vendors v ON i.vendor_id = v.vendor_id
      WHERE i.deleted_at IS NULL AND i.invoice_date BETWEEN ? AND ?
      GROUP BY v.vendor_id
      ORDER BY total DESC
      LIMIT 5
    ),
    pending_emails AS (
      SELECT COUNT(*) as count
      FROM emails
      WHERE processed_at IS NULL
    )
    SELECT
      (SELECT count FROM invoice_totals) as invoice_count,
      (SELECT total FROM invoice_totals) as total_spend,
      (SELECT json_group_array(json_object('state', state, 'count', count)) FROM recon_status) as recon_json,
      (SELECT json_group_array(json_object('category', category, 'total', total)) FROM category_breakdown) as category_json,
      (SELECT json_group_array(json_object('name', name, 'total', total)) FROM top_vendors) as vendors_json,
      (SELECT count FROM pending_emails) as pending_emails
  `
    )
    .get(start, end, fy, start, end, start, end) as {
    invoice_count: number;
    total_spend: number;
    recon_json: string;
    category_json: string;
    vendors_json: string;
    pending_emails: number;
  };

  return {
    invoiceCount: result.invoice_count || 0,
    totalSpend: result.total_spend || 0,
    reconStatus: JSON.parse(result.recon_json || "[]"),
    categoryBreakdown: JSON.parse(result.category_json || "[]"),
    topVendors: JSON.parse(result.vendors_json || "[]"),
    pendingEmails: result.pending_emails || 0,
  };
}

export interface LastRun {
  operation: string;
  completedAt: string | null;
  startedAt: string | null;
  status: string;
  statsJson: string | null;
}

export function getLastRuns(): LastRun[] {
  const operations = ["ingest_email", "ingest_invoice", "reconcile"];

  const rows = db
    .prepare(
      `
      WITH ranked AS (
        SELECT operation, completed_at, started_at, status, stats_json,
               ROW_NUMBER() OVER (PARTITION BY operation ORDER BY started_at DESC) as rn
        FROM runs
        WHERE operation IN ('ingest_email', 'ingest_invoice', 'reconcile')
      )
      SELECT operation, completed_at, started_at, status, stats_json
      FROM ranked
      WHERE rn = 1
    `
    )
    .all() as { operation: string; completed_at: string | null; started_at: string | null; status: string; stats_json: string | null }[];

  const rowMap = new Map(rows.map((r) => [r.operation, r]));

  return operations.map((op) => {
    const row = rowMap.get(op);
    return {
      operation: op,
      completedAt: row?.completed_at || null,
      startedAt: row?.started_at || null,
      status: row?.status || "never",
      statsJson: row?.stats_json || null,
    };
  });
}

export interface RunningJob {
  runId: string;
  operation: string;
  startedAt: string;
  statsJson: string | null;
}

export function getRunningJobs(operation: string): RunningJob[] {
  const rows = db
    .prepare(
      `
      SELECT run_id, operation, started_at, stats_json
      FROM runs
      WHERE operation = ? AND status = 'running'
      ORDER BY started_at DESC
    `
    )
    .all(operation) as { run_id: string; operation: string; started_at: string; stats_json: string | null }[];

  return rows.map((row) => ({
    runId: row.run_id,
    operation: row.operation,
    startedAt: row.started_at,
    statsJson: row.stats_json,
  }));
}

export interface SyncCoverage {
  emailCount: number;
  earliestEmail: string | null;
  latestEmail: string | null;
}

export function getSyncCoverage(): SyncCoverage {
  const row = db
    .prepare(
      `
      SELECT
        COUNT(*) as email_count,
        MIN(received_at) as earliest_email,
        MAX(received_at) as latest_email
      FROM emails
    `
    )
    .get() as {
    email_count: number;
    earliest_email: string | null;
    latest_email: string | null;
  };

  return {
    emailCount: row.email_count || 0,
    earliestEmail: row.earliest_email,
    latestEmail: row.latest_email,
  };
}

export interface PendingAction {
  msgId: string;
  fromAddr: string;
  subject: string;
  receivedAt: string;
  outcome: string;
}

export function getPendingActions(): PendingAction[] {
  const rows = db
    .prepare(
      `
      SELECT msg_id, from_addr, subject, received_at, outcome
      FROM emails
      WHERE outcome IN ('needs_manual_download', 'error', 'no_attachment')
        AND dismissed_at IS NULL
      ORDER BY received_at DESC
      LIMIT 50
    `
    )
    .all() as {
    msg_id: string;
    from_addr: string;
    subject: string;
    received_at: string;
    outcome: string;
  }[];

  return rows.map((row) => ({
    msgId: row.msg_id,
    fromAddr: row.from_addr,
    subject: row.subject,
    receivedAt: row.received_at,
    outcome: row.outcome,
  }));
}

export function dismissEmail(
  msgId: string,
  reason: "not_invoice" | "resolved" | "duplicate"
): void {
  const email = db
    .prepare("SELECT from_addr, subject FROM emails WHERE msg_id = ?")
    .get(msgId) as { from_addr: string; subject: string } | undefined;

  if (!email) return;

  const domain = email.from_addr.split("@")[1] || "";

  db.prepare(
    `UPDATE emails SET dismissed_at = datetime('now'), dismissed_reason = ? WHERE msg_id = ?`
  ).run(reason, msgId);

  db.prepare(
    `INSERT INTO email_feedback (msg_id, feedback_type, feedback_value, from_addr, subject, sender_domain)
     VALUES (?, 'dismiss', ?, ?, ?, ?)`
  ).run(msgId, reason, email.from_addr, email.subject, domain);
}
