"use client";

import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { FxError } from "@/lib/queries/dashboard";

interface FxErrorsCardProps {
  fxErrors: FxError[];
}

export function FxErrorsCard({ fxErrors }: FxErrorsCardProps) {
  if (fxErrors.length === 0) return null;

  const uniqueCurrencies = [...new Set(fxErrors.map((e) => e.currency))];

  return (
    <Card className="border-orange-200 bg-orange-50/50">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-orange-800">
          <span className="inline-flex h-2 w-2 rounded-full bg-orange-500" />
          Missing GBP Conversion ({fxErrors.length})
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-sm text-orange-700">
          {fxErrors.length} invoice{fxErrors.length !== 1 ? "s" : ""} could not be converted to GBP.
          {uniqueCurrencies.length > 0 && (
            <span className="ml-1">
              Currencies: {uniqueCurrencies.join(", ")}
            </span>
          )}
        </p>
        <div className="mb-3 max-h-32 overflow-auto rounded border border-orange-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-orange-50 text-left text-xs">
              <tr>
                <th className="px-2 py-1">Vendor</th>
                <th className="px-2 py-1">Amount</th>
                <th className="px-2 py-1">Date</th>
                <th className="px-2 py-1">Error</th>
              </tr>
            </thead>
            <tbody>
              {fxErrors.slice(0, 5).map((error) => (
                <tr key={error.invoiceId} className="border-t border-orange-100">
                  <td className="max-w-24 truncate px-2 py-1">{error.vendorName}</td>
                  <td className="px-2 py-1 whitespace-nowrap">
                    {error.amountGross} {error.currency}
                  </td>
                  <td className="px-2 py-1 whitespace-nowrap">{error.invoiceDate}</td>
                  <td className="max-w-32 truncate px-2 py-1 text-red-600">{error.fxError}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {fxErrors.length > 5 && (
            <p className="border-t border-orange-100 bg-orange-50 px-2 py-1 text-center text-xs text-orange-600">
              + {fxErrors.length - 5} more
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Link href="/invoices?fx_error=true">
            <Button variant="outline" size="sm" className="text-orange-700 border-orange-300 hover:bg-orange-100">
              View All
            </Button>
          </Link>
          <p className="text-xs text-muted-foreground">
            Run <code className="rounded bg-gray-100 px-1">granite db backfill-fx --force</code> to retry
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
