"use client";

import { useQueryStates, parseAsString, parseAsBoolean } from "nuqs";
import { useEffect, useState } from "react";
import { useDebouncedSearch } from "@/hooks/use-debounced-search";
import { toast } from "sonner";
import { InvoiceTable } from "@/components/invoice-table";
import { Button } from "@/components/ui/button";
import { getCurrentFY } from "@/lib/fiscal";
import type { InvoiceListRow } from "@/lib/types";
import { fetchInvoices, fetchExceptionInvoices } from "@/lib/actions/invoices";
import { apiFetch } from "@/lib/api-fetch";

const MAX_SELECTION = 100;

export function InvoiceList() {
  const [invoices, setInvoices] = useState<InvoiceListRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [downloading, setDownloading] = useState(false);

  const [filters] = useQueryStates(
    {
      fy: parseAsString.withDefault(getCurrentFY()),
      vendor: parseAsString,
      category: parseAsString,
      status: parseAsString.withDefault("all"),
      exported: parseAsString,
      search: parseAsString,
      dateFrom: parseAsString,
      dateTo: parseAsString,
      exceptions: parseAsBoolean.withDefault(false),
    },
    { shallow: true }
  );

  // Destructure individual primitive fields so the effect re-runs only when a
  // field actually changes — depending on `filters` (a fresh object identity
  // each render) caused redundant fetches.
  const {
    fy,
    vendor,
    category,
    status,
    exported,
    search,
    dateFrom,
    dateTo,
    exceptions,
  } = filters;

  // 300 ms debounce on the search input. Without it, typing "webflow" issues
  // 7 cumulative server actions; with the unindexed leading-wildcard LIKE
  // (todo 025) that meant 70K row scans per word. Pair the debounce with the
  // prefix-LIKE fix in queries/invoices.ts and the new NOCASE index.
  const debouncedSearch = useDebouncedSearch(search);

  useEffect(() => {
    // AbortController on a Server Action does NOT cancel server-side execution
    // (see vercel/next.js#81418, discussion #54516). The Server Action runs to
    // completion; the cancelled flag below just drops stale results client-side
    // so the user sees the result of their latest filter change, not an older
    // in-flight one.
    let cancelled = false;

    (async () => {
      setLoading(true);
      setError(null);
      setSelectedIds(new Set());
      try {
        const result = exceptions
          ? await fetchExceptionInvoices(fy)
          : await fetchInvoices({
              fy,
              vendor: vendor || undefined,
              category: category || undefined,
              status:
                (status as "matched" | "unmatched" | "pending" | "all") ||
                "all",
              exported: (exported as "yes" | "no" | undefined) || undefined,
              search: debouncedSearch || undefined,
              dateFrom: dateFrom || undefined,
              dateTo: dateTo || undefined,
            });

        if (cancelled) return;
        if (result.ok) {
          setInvoices(result.data);
        } else {
          setError(result.error.message);
        }
      } catch {
        if (!cancelled) setError("Failed to load invoices");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [fy, vendor, category, status, exported, debouncedSearch, dateFrom, dateTo, exceptions]);

  const handleSelectAll = () => {
    const idsToSelect = invoices.slice(0, MAX_SELECTION).map((i) => i.invoice_id);
    setSelectedIds(new Set(idsToSelect));
    if (invoices.length > MAX_SELECTION) {
      toast.warning(`Selection limited to ${MAX_SELECTION} invoices`);
    }
  };

  const handleClearSelection = () => {
    setSelectedIds(new Set());
  };

  const handleDownloadSelected = async () => {
    if (selectedIds.size === 0) {
      toast.error("No invoices selected");
      return;
    }

    setDownloading(true);
    try {
      const response = await apiFetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invoiceIds: Array.from(selectedIds) }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || "Download failed");
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `invoices-${fy}.zip`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);

      toast.success(`Downloaded ${selectedIds.size} invoices`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  };

  if (loading) {
    return <div className="text-muted-foreground">Loading invoices...</div>;
  }

  if (error) {
    return <div className="text-destructive">{error}</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {invoices.length} invoice{invoices.length !== 1 ? "s" : ""} found
          {selectedIds.size > 0 && ` · ${selectedIds.size} selected`}
        </p>
        <div className="flex gap-2">
          {selectedIds.size > 0 ? (
            <>
              <Button variant="outline" size="sm" onClick={handleClearSelection}>
                Clear selection
              </Button>
              <Button
                size="sm"
                onClick={handleDownloadSelected}
                disabled={downloading}
              >
                {downloading ? "Downloading..." : `Download ${selectedIds.size} PDFs`}
              </Button>
            </>
          ) : (
            invoices.length > 0 && (
              <Button variant="outline" size="sm" onClick={handleSelectAll}>
                Select all{" "}
                {invoices.length > MAX_SELECTION
                  ? `(first ${MAX_SELECTION})`
                  : `(${invoices.length})`}
              </Button>
            )
          )}
        </div>
      </div>

      <InvoiceTable
        data={invoices}
        selectable
        selectedIds={selectedIds}
        onSelectionChange={setSelectedIds}
      />
    </div>
  );
}
