import { Suspense } from "react";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { InboxContent } from "./inbox-content";

export default function InboxPage() {
  return (
    <NuqsAdapter>
      <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold">Inbox</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Review and triage synced emails before processing
          </p>
        </div>

        <Suspense fallback={<div className="text-muted-foreground">Loading inbox...</div>}>
          <InboxContent />
        </Suspense>
      </div>
    </NuqsAdapter>
  );
}
