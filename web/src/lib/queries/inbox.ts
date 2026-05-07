import { db } from "../db";
import { fyBoundsOrAll } from "../fiscal";

export interface InboxEmailRow {
  msgId: string;
  fromAddr: string;
  subject: string;
  receivedAt: string;
  processedAt: string | null;
  outcome: string | null;
}

export interface InboxFilters {
  sender?: string;
  dateFrom?: string;
  dateTo?: string;
  view?: "unprocessed" | "all";
  fy?: string;
  outcome?: string;
}

export interface InboxCounts {
  unprocessed: number;
  all: number;
}

export function getInboxEmails(filters: InboxFilters): InboxEmailRow[] {
  const conditions: string[] = ["dismissed_at IS NULL"];
  const params: (string | number)[] = [];

  if (filters.view === "unprocessed" || !filters.view) {
    conditions.push("processed_at IS NULL");
  }

  if (filters.sender) {
    conditions.push("from_addr LIKE ?");
    params.push(`%${filters.sender}%`);
  }

  if (filters.dateFrom) {
    conditions.push("DATE(received_at) >= ?");
    params.push(filters.dateFrom);
  }

  if (filters.dateTo) {
    conditions.push("DATE(received_at) <= ?");
    params.push(filters.dateTo);
  }

  if (filters.fy) {
    const fyRange = fyBoundsOrAll(filters.fy);
    if (fyRange) {
      conditions.push("DATE(received_at) >= ? AND DATE(received_at) <= ?");
      params.push(fyRange.start, fyRange.end);
    }
  }

  if (filters.outcome) {
    if (filters.outcome === "no_attachment") {
      conditions.push("outcome = 'no_attachment'");
    } else if (filters.outcome === "neither") {
      conditions.push("outcome = 'neither'");
    } else if (filters.outcome === "invoice") {
      conditions.push("outcome = 'invoice'");
    } else if (filters.outcome === "receipt") {
      conditions.push("outcome = 'receipt'");
    } else if (filters.outcome === "statement") {
      conditions.push("outcome = 'statement'");
    } else if (filters.outcome === "pending") {
      conditions.push("outcome = 'pending'");
    } else if (filters.outcome === "error") {
      conditions.push("outcome = 'error'");
    }
  }

  const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  const rows = db
    .prepare(
      `
      SELECT
        msg_id,
        from_addr,
        subject,
        received_at,
        processed_at,
        outcome
      FROM emails
      ${whereClause}
      ORDER BY received_at DESC
      LIMIT 500
      `
    )
    .all(...params) as {
    msg_id: string;
    from_addr: string;
    subject: string;
    received_at: string;
    processed_at: string | null;
    outcome: string | null;
  }[];

  return rows.map((row) => ({
    msgId: row.msg_id,
    fromAddr: row.from_addr,
    subject: row.subject,
    receivedAt: row.received_at,
    processedAt: row.processed_at,
    outcome: row.outcome,
  }));
}

export function getInboxCounts(): InboxCounts {
  const result = db
    .prepare(
      `
      SELECT
        COUNT(*) FILTER (WHERE processed_at IS NULL AND dismissed_at IS NULL) as unprocessed,
        COUNT(*) FILTER (WHERE dismissed_at IS NULL) as all_synced
      FROM emails
      `
    )
    .get() as { unprocessed: number; all_synced: number };

  return {
    unprocessed: result.unprocessed || 0,
    all: result.all_synced || 0,
  };
}
