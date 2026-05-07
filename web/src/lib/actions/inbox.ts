"use server";

import { getInboxEmails, getInboxCounts, type InboxFilters, type InboxEmailRow, type InboxCounts } from "../queries/inbox";
import { bulkDismissEmails } from "../queries/dashboard";

type Result<T> = { ok: true; data: T } | { ok: false; error: { message: string } };

export async function fetchInboxEmails(
  filters: InboxFilters
): Promise<Result<InboxEmailRow[]>> {
  try {
    const emails = getInboxEmails(filters);
    return { ok: true, data: emails };
  } catch (err) {
    console.error("Failed to fetch inbox emails:", err);
    return { ok: false, error: { message: "Failed to fetch emails" } };
  }
}

export async function fetchInboxCounts(): Promise<Result<InboxCounts>> {
  try {
    const counts = getInboxCounts();
    return { ok: true, data: counts };
  } catch (err) {
    console.error("Failed to fetch inbox counts:", err);
    return { ok: false, error: { message: "Failed to fetch counts" } };
  }
}

export async function rejectEmails(
  msgIds: string[]
): Promise<Result<{ count: number }>> {
  try {
    const count = bulkDismissEmails(msgIds, "rejected");
    return { ok: true, data: { count } };
  } catch (err) {
    console.error("Failed to reject emails:", err);
    return { ok: false, error: { message: "Failed to reject emails" } };
  }
}
