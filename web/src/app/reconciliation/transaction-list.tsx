"use client";

import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { TransactionListRow } from "@/lib/queries/reconciliation";

interface TransactionListProps {
  transactions: TransactionListRow[];
  loading: boolean;
  onResolve: (txnId: string, state: string) => void;
}

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleDateString();
}

function formatAmount(amount: string, currency: string) {
  const value = parseFloat(amount);
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: currency === "GBP" ? "GBP" : currency === "USD" ? "USD" : currency === "EUR" ? "EUR" : "GBP",
  }).format(Math.abs(value));
}

function StateLabel({ state, needsManualDownload }: { state: string | null; needsManualDownload: boolean }) {
  if (needsManualDownload) {
    return (
      <span className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">
        Needs Download
      </span>
    );
  }

  const labels: Record<string, { label: string; className: string }> = {
    auto_matched: { label: "Auto Matched", className: "bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-200" },
    user_verified: { label: "Verified", className: "bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-200" },
    suggested: { label: "Suggested", className: "bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-200" },
    unmatched: { label: "Unmatched", className: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" },
    user_personal: { label: "Personal", className: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200" },
    user_ignore: { label: "Ignored", className: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200" },
  };

  const info = state
    ? labels[state] || { label: state, className: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200" }
    : { label: "Unmatched", className: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" };

  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${info.className}`}>
      {info.label}
    </span>
  );
}

export function TransactionList({ transactions, loading, onResolve }: TransactionListProps) {
  if (loading) {
    return <div className="text-muted-foreground py-8 text-center">Loading transactions...</div>;
  }

  if (transactions.length === 0) {
    return (
      <div className="py-8 text-center text-muted-foreground">
        No transactions found. Upload a bank statement to get started.
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Date</TableHead>
          <TableHead>Account</TableHead>
          <TableHead>Description</TableHead>
          <TableHead className="text-right">Amount</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="w-10"></TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {transactions.map((txn) => (
          <TableRow key={txn.txnId}>
            <TableCell className="text-sm text-muted-foreground">
              {formatDate(txn.bookingDate)}
            </TableCell>
            <TableCell className="text-sm capitalize">{txn.account}</TableCell>
            <TableCell className="max-w-60 truncate text-sm">
              {txn.descriptionCanonical}
            </TableCell>
            <TableCell className="text-right text-sm font-medium tabular-nums">
              {formatAmount(txn.amountGbp, "GBP")}
            </TableCell>
            <TableCell>
              <StateLabel state={txn.state} needsManualDownload={txn.needsManualDownload} />
            </TableCell>
            <TableCell>
              <TransactionActions
                txn={txn}
                onResolve={onResolve}
              />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function TransactionActions({
  txn,
  onResolve,
}: {
  txn: TransactionListRow;
  onResolve: (txnId: string, state: string) => void;
}) {
  const state = txn.state;
  const canMarkPersonal = !state || state === "unmatched" || state === "suggested" || txn.needsManualDownload;
  const canMarkIgnore = !state || state === "unmatched" || state === "suggested" || txn.needsManualDownload;

  if (!canMarkPersonal && !canMarkIgnore) {
    return null;
  }

  return (
    <div className="flex gap-1">
      {canMarkPersonal && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 text-xs"
          onClick={() => onResolve(txn.txnId, "personal")}
        >
          Personal
        </Button>
      )}
      {canMarkIgnore && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 text-xs"
          onClick={() => onResolve(txn.txnId, "ignore")}
        >
          Ignore
        </Button>
      )}
    </div>
  );
}
