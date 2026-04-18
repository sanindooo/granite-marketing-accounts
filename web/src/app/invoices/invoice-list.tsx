"use client";

import { useQueryStates, parseAsString, parseAsBoolean } from "nuqs";
import { useEffect, useState } from "react";
import { InvoiceTable } from "@/components/invoice-table";
import { getCurrentFY } from "@/lib/fiscal";
import type { InvoiceRow } from "@/lib/types";
import { fetchInvoices, fetchExceptionInvoices } from "@/lib/actions/invoices";

export function InvoiceList() {
  const [invoices, setInvoices] = useState<InvoiceRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

      try {
        let result;
        if (filters.exceptions) {
          result = await fetchExceptionInvoices(filters.fy);
        } else {
          result = await fetchInvoices({
            fy: filters.fy,
            vendor: filters.vendor || undefined,
            category: filters.category || undefined,
            status: (filters.status as "matched" | "unmatched" | "pending" | "all") || "all",
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

  if (loading) {
    return <div className="text-muted-foreground">Loading invoices...</div>;
  }

  if (error) {
    return <div className="text-destructive">{error}</div>;
  }

  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">
        {invoices.length} invoice{invoices.length !== 1 ? "s" : ""} found
      </p>
      <InvoiceTable data={invoices} />
    </div>
  );
}
