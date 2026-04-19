import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function InvoiceNotFound() {
  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="flex flex-col items-center justify-center gap-4 py-16">
        <h2 className="text-xl font-semibold">Invoice not found</h2>
        <p className="text-muted-foreground">
          The invoice you're looking for doesn't exist or has been deleted.
        </p>
        <Link href="/invoices">
          <Button>Back to invoices</Button>
        </Link>
      </div>
    </div>
  );
}
