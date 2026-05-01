"use server";

import { db } from "@/lib/db";

export async function markInvoicesExported(invoiceIds: string[]): Promise<void> {
  if (invoiceIds.length === 0) return;
  const placeholders = invoiceIds.map(() => "?").join(",");
  db.prepare(
    `UPDATE invoices
     SET last_exported_at = datetime('now')
     WHERE invoice_id IN (${placeholders})`
  ).run(...invoiceIds);
}
