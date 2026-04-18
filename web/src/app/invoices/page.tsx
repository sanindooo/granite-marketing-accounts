import { Suspense } from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { InvoiceFilters } from "@/components/invoice-filters";
import { InvoiceList } from "./invoice-list";
import { getVendors, getCategories } from "@/lib/queries/invoices";

export default function InvoicesPage() {
  const vendors = getVendors();
  const categories = getCategories();

  return (
    <NuqsAdapter>
      <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold">Invoices</h1>
        </div>

        <div className="space-y-6">
          <InvoiceFilters vendors={vendors} categories={categories} />

          <Suspense fallback={<div className="text-muted-foreground">Loading invoices...</div>}>
            <InvoiceList />
          </Suspense>
        </div>
      </div>
    </NuqsAdapter>
  );
}
