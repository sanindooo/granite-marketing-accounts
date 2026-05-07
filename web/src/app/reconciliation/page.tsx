import { Suspense } from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { ReconciliationContent } from "./reconciliation-content";

export default function ReconciliationPage() {
  return (
    <NuqsAdapter>
      <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold">Reconciliation</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Match bank transactions to invoices and receipts
          </p>
        </div>

        <Suspense fallback={<div className="text-muted-foreground">Loading...</div>}>
          <ReconciliationContent />
        </Suspense>
      </div>
    </NuqsAdapter>
  );
}
