"use client";

import Link from "next/link";
import { useEffect } from "react";
import { Button } from "@/components/ui/button";

export default function InvoiceError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="flex flex-col items-center justify-center gap-4 py-16">
        <h2 className="text-xl font-semibold">Failed to load invoice</h2>
        <p className="text-muted-foreground">{error.message}</p>
        <div className="flex gap-3">
          <Button onClick={reset}>Try again</Button>
          <Link href="/invoices">
            <Button variant="outline">Back to invoices</Button>
          </Link>
        </div>
      </div>
    </div>
  );
}
