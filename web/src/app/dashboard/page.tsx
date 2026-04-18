import { Suspense } from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { DashboardContent } from "./dashboard-content";
import { FYSelector } from "./fy-selector";

export default function DashboardPage() {
  return (
    <NuqsAdapter>
      <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <div className="mb-6 flex items-center justify-between">
          <h1 className="text-2xl font-semibold">Dashboard</h1>
          <Suspense fallback={<div className="h-9 w-36 animate-pulse rounded-md bg-muted" />}>
            <FYSelector />
          </Suspense>
        </div>

        <Suspense fallback={<div className="text-muted-foreground">Loading...</div>}>
          <DashboardContent />
        </Suspense>
      </div>
    </NuqsAdapter>
  );
}
