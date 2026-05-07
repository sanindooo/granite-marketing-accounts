"use server";

import {
  getTransactions,
  getReconciliationCounts,
  getAccounts,
  type ReconciliationFilters,
  type TransactionListRow,
  type ReconciliationCounts,
} from "../queries/reconciliation";

type Result<T> = { ok: true; data: T } | { ok: false; error: { message: string } };

export async function fetchTransactions(
  filters: ReconciliationFilters
): Promise<Result<TransactionListRow[]>> {
  try {
    const transactions = getTransactions(filters);
    return { ok: true, data: transactions };
  } catch (err) {
    console.error("Failed to fetch transactions:", err);
    return { ok: false, error: { message: "Failed to fetch transactions" } };
  }
}

export async function fetchReconciliationCounts(
  fy?: string
): Promise<Result<ReconciliationCounts>> {
  try {
    const counts = getReconciliationCounts(fy);
    return { ok: true, data: counts };
  } catch (err) {
    console.error("Failed to fetch reconciliation counts:", err);
    return { ok: false, error: { message: "Failed to fetch counts" } };
  }
}

export async function fetchAccounts(): Promise<Result<string[]>> {
  try {
    const accounts = getAccounts();
    return { ok: true, data: accounts };
  } catch (err) {
    console.error("Failed to fetch accounts:", err);
    return { ok: false, error: { message: "Failed to fetch accounts" } };
  }
}
