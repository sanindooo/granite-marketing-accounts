"use client";

import { useQueryStates, parseAsString, parseAsBoolean } from "nuqs";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { InvoiceTable } from "@/components/invoice-table";
import { Button } from "@/components/ui/button";
import { getCurrentFY } from "@/lib/fiscal";
import type { InvoiceRow } from "@/lib/types";
import { fetchInvoices, fetchExceptionInvoices } from "@/lib/actions/invoices";

const MAX_SELECTION = 100;

export function InvoiceList() {
  const [invoices, setInvoices] = useState<InvoiceRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [downloading, setDownloading] = useState(false);

  const [filters] = useQueryStates({
    fy: parseAsString.withDefault(getCurrentFY()),
    vendor: parseAsString,
    category: parseAsString,
    status: parseAsString.withDefault("all"),
    search: parseAsString,
    dateFrom: parseAsString,
    dateTo: parseAsString,
    exceptions: parseAsBoolean.withDefault(false),
  });

  useEffect(() => {
    async function loadInvoices() {
      setLoading(true);
      setError(null);
      setSelectedIds(new Set());

      try {
        let result;
        if (filters.exceptions) {
          result = await fetchExceptionInvoices(filters.fy);
        } else {
          result = await fetchInvoices({
            fy: filters.fy,
            vendor: filters.vendor || undefined,
            category: filters.category || undefined,
            status:
              (filters.status as "matched" | "unmatched" | "pending" | "all") ||
              "all",
            search: filters.search || undefined,
            dateFrom: filters.dateFrom || undefined,
            dateTo: filters.dateTo || undefined,
          });
        }

        if (result.ok) {
          setInvoices(result.data);
        } else {
          setError(result.error.message);
        }
      } catch {
        setError("Failed to load invoices");
      } finally {
        setLoading(false);
      }
    }

    loadInvoices();
  }, [filters]);

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
      const response = await fetch("/api/download", {
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
      a.download = `invoices-${filters.fy}.zip`;
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
