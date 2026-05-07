"use client";

import { useEffect, useState, useCallback } from "react";
import { useQueryStates, parseAsString } from "nuqs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { getAvailableFYs, getCurrentFY } from "@/lib/fiscal";
import { apiFetch } from "@/lib/api-fetch";
import {
  fetchTransactions,
  fetchReconciliationCounts,
  fetchAccounts,
} from "@/lib/actions/reconciliation";
import type { TransactionListRow, ReconciliationCounts } from "@/lib/queries/reconciliation";
import { UploadDialog } from "./upload-dialog";
import { BulkUploadDialog } from "./bulk-upload-dialog";
import { TransactionList } from "./transaction-list";
import { BulkResolveDialog } from "./bulk-resolve-dialog";

type View = "all" | "unmatched" | "matched" | "resolved";

const VALID_ACCOUNTS = ["amex", "wise", "tide", "monzo"];

export function ReconciliationContent() {
  const [filters, setFilters] = useQueryStates(
    {
      view: parseAsString.withDefault("all"),
      fy: parseAsString.withDefault(getCurrentFY()),
      account: parseAsString,
    },
    { shallow: true }
  );

  const [transactions, setTransactions] = useState<TransactionListRow[]>([]);
  const [counts, setCounts] = useState<ReconciliationCounts>({
    total: 0,
    unmatched: 0,
    matched: 0,
    needsManualDownload: 0,
  });
  const [accounts, setAccounts] = useState<string[]>(VALID_ACCOUNTS);
  const [loading, setLoading] = useState(true);
  const [resolving, setResolving] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selectedTxns, setSelectedTxns] = useState<Set<string>>(new Set());
  const [bulkResolveOpen, setBulkResolveOpen] = useState(false);

  const { view, fy, account } = filters;

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const stateFilter =
        view === "unmatched"
          ? "unmatched"
          : view === "matched"
          ? "auto_matched"
          : view === "resolved"
          ? "user_verified"
          : undefined;

      const [txnResult, countsResult, accountsResult] = await Promise.all([
        fetchTransactions({
          fy: fy || undefined,
          state: stateFilter,
          account: account || undefined,
          search: search || undefined,
        }),
        fetchReconciliationCounts(fy || undefined),
        fetchAccounts(),
      ]);

      if (txnResult.ok) {
        setTransactions(txnResult.data);
      }
      if (countsResult.ok) {
        setCounts(countsResult.data);
      }
      if (accountsResult.ok && accountsResult.data.length > 0) {
        setAccounts(accountsResult.data);
      }
    } catch (err) {
      console.error("Failed to load reconciliation data:", err);
    } finally {
      setLoading(false);
    }
  }, [view, fy, account, search]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleResolve = useCallback(
    async (txnId: string, state: string) => {
      setResolving(txnId);
      try {
        const response = await apiFetch("/api/reconciliation/resolve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ txnId, state }),
        });

        const result = await response.json();

        if (result.status === "success") {
          toast.success(
            state === "personal"
              ? "Marked as personal expense"
              : "Marked as no invoice needed"
          );
          await loadData();
        } else {
          toast.error(result.message || "Failed to resolve transaction");
        }
      } catch (err) {
        toast.error("Failed to resolve transaction");
        console.error(err);
      } finally {
        setResolving(null);
      }
    },
    [loadData]
  );

  const handleBulkResolve = useCallback(
    async (reason: string, note?: string) => {
      const txnIds = Array.from(selectedTxns);
      if (txnIds.length === 0) return;

      try {
        const response = await apiFetch("/api/reconciliation/bulk-resolve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ txnIds, reason, note }),
        });

        const result = await response.json();

        if (result.status === "success") {
          toast.success(`Resolved ${result.resolved} transactions`);
          setSelectedTxns(new Set());
          await loadData();
        } else {
          toast.error(result.message || "Failed to resolve transactions");
        }
      } catch (err) {
        toast.error("Failed to resolve transactions");
        console.error(err);
      }
    },
    [selectedTxns, loadData]
  );

  const toggleSelectAll = useCallback(() => {
    if (selectedTxns.size === transactions.length) {
      setSelectedTxns(new Set());
    } else {
      setSelectedTxns(new Set(transactions.map((t) => t.txnId)));
    }
  }, [transactions, selectedTxns.size]);

  const toggleSelectTxn = useCallback((txnId: string) => {
    setSelectedTxns((prev) => {
      const next = new Set(prev);
      if (next.has(txnId)) {
        next.delete(txnId);
      } else {
        next.add(txnId);
      }
      return next;
    });
  }, []);

  const clearFilters = () => {
    setFilters({
      fy: getCurrentFY(),
      account: null,
      view: "all",
    });
    setSearch("");
  };

  const hasActiveFilters = account || (fy && fy !== getCurrentFY()) || view !== "all" || search;

  return (
    <div className="space-y-6">
      {/* Stats Cards */}
      <div className="grid grid-cols-4 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Transactions
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{counts.total}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Matched
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-green-600">{counts.matched}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Unmatched
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-amber-600">{counts.unmatched}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Needs Download
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-red-600">{counts.needsManualDownload}</div>
          </CardContent>
        </Card>
      </div>

      {/* Main Content */}
      <Card>
        <CardHeader className="space-y-4">
          <div className="flex items-center justify-between">
            <CardTitle>Transactions</CardTitle>
            <div className="flex gap-2">
              <BulkUploadDialog onSuccess={loadData} />
              <UploadDialog accounts={accounts} onSuccess={loadData} />
            </div>
          </div>

          <div className="flex items-end justify-between">
            <div className="flex flex-wrap items-end gap-4">
              <div className="flex flex-col gap-1">
                <label className="text-xs font-medium text-muted-foreground">
                  Fiscal Year
                </label>
                <Select value={fy} onValueChange={(value) => setFilters({ fy: value })}>
                  <SelectTrigger className="w-36 h-9">
                    <SelectValue placeholder="Fiscal Year">
                      {fy === "all" ? "All Years" : fy}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {getAvailableFYs(true).map((fyOption) => (
                      <SelectItem key={fyOption} value={fyOption}>
                        {fyOption === "all" ? "All Years" : fyOption}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="flex flex-col gap-1">
                <label className="text-xs font-medium text-muted-foreground">Account</label>
                <Select
                  value={account || "all"}
                  onValueChange={(value) =>
                    setFilters({ account: value === "all" ? null : value })
                  }
                >
                  <SelectTrigger className="w-36 h-9">
                    <SelectValue placeholder="All Accounts" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All Accounts</SelectItem>
                    {accounts.map((acc) => (
                      <SelectItem key={acc} value={acc}>
                        {acc.charAt(0).toUpperCase() + acc.slice(1)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="flex flex-col gap-1">
                <label className="text-xs font-medium text-muted-foreground">Search</label>
                <Input
                  placeholder="Search description..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="w-48 h-9"
                />
              </div>

              {hasActiveFilters && (
                <Button variant="ghost" size="sm" className="h-9" onClick={clearFilters}>
                  Clear filters
                </Button>
              )}
            </div>

            <div className="flex gap-1 rounded-lg bg-muted p-1">
              <Button
                variant={view === "all" ? "default" : "ghost"}
                size="sm"
                className="h-7 text-xs"
                onClick={() => setFilters({ view: "all" })}
              >
                All ({counts.total})
              </Button>
              <Button
                variant={view === "unmatched" ? "default" : "ghost"}
                size="sm"
                className="h-7 text-xs"
                onClick={() => setFilters({ view: "unmatched" })}
              >
                Unmatched ({counts.unmatched})
              </Button>
              <Button
                variant={view === "matched" ? "default" : "ghost"}
                size="sm"
                className="h-7 text-xs"
                onClick={() => setFilters({ view: "matched" })}
              >
                Matched ({counts.matched})
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {selectedTxns.size > 0 && (
            <div className="mb-4 flex items-center gap-4 rounded-lg bg-muted p-3">
              <span className="text-sm font-medium">
                {selectedTxns.size} selected
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setBulkResolveOpen(true)}
              >
                Resolve Selected
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSelectedTxns(new Set())}
              >
                Clear Selection
              </Button>
            </div>
          )}
          <TransactionList
            transactions={transactions}
            loading={loading || resolving !== null}
            onResolve={handleResolve}
            selectedTxns={selectedTxns}
            onToggleSelect={toggleSelectTxn}
            onToggleSelectAll={toggleSelectAll}
          />
        </CardContent>
      </Card>

      <BulkResolveDialog
        open={bulkResolveOpen}
        onOpenChange={setBulkResolveOpen}
        count={selectedTxns.size}
        onResolve={handleBulkResolve}
      />
    </div>
  );
}
