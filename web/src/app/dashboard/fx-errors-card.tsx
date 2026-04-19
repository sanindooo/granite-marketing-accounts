"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { toast } from "sonner";
import { RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { FxError } from "@/lib/queries/dashboard";
import { usePipelineStream } from "@/hooks/use-pipeline-stream";

interface FxErrorsCardProps {
  fxErrors: FxError[];
}

export function FxErrorsCard({ fxErrors }: FxErrorsCardProps) {
  const router = useRouter();
  const { isRunning, activeCommand, result, error, run } = usePipelineStream();
  const isBackfillRunning = isRunning && activeCommand === "backfillFx";
  const handledResultRef = useRef<Record<string, unknown> | null>(null);
  const handledErrorRef = useRef<{ message: string } | null>(null);

  const handleRetry = async () => {
    handledResultRef.current = null;
    handledErrorRef.current = null;
    await run("backfillFx", { force: true });
  };

  useEffect(() => {
    if (result && !isRunning && result !== handledResultRef.current) {
      handledResultRef.current = result;
      const processed = (result.processed as number) ?? 0;
      const errors = (result.errors as number) ?? 0;
      if (processed > 0 && errors === 0) {
        toast.success(`Successfully converted ${processed} invoice${processed !== 1 ? "s" : ""} to GBP`);
      } else if (processed > 0 && errors > 0) {
        toast.warning(`Converted ${processed} invoice${processed !== 1 ? "s" : ""}, ${errors} still have errors`);
      } else if (errors > 0) {
        toast.error(`Failed to convert ${errors} invoice${errors !== 1 ? "s" : ""}`);
      } else {
        toast.info("No invoices needed conversion");
      }
      router.refresh();
    }
  }, [result, isRunning, router]);

  useEffect(() => {
    if (error && !isRunning && error !== handledErrorRef.current) {
      handledErrorRef.current = error;
      toast.error(error.user_message ?? error.message ?? "FX conversion failed");
    }
  }, [error, isRunning]);

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
              {fxErrors.slice(0, 5).map((fxErr) => (
                <tr key={fxErr.invoiceId} className="border-t border-orange-100">
                  <td className="max-w-24 truncate px-2 py-1">{fxErr.vendorName}</td>
                  <td className="px-2 py-1 whitespace-nowrap">
                    {fxErr.amountGross} {fxErr.currency}
                  </td>
                  <td className="px-2 py-1 whitespace-nowrap">{fxErr.invoiceDate}</td>
                  <td className="max-w-32 truncate px-2 py-1 text-red-600">{fxErr.fxError}</td>
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
          <Button
            variant="default"
            size="sm"
            onClick={handleRetry}
            disabled={isBackfillRunning}
            className="bg-orange-600 hover:bg-orange-700"
          >
            {isBackfillRunning ? (
              <>
                <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                Converting...
              </>
            ) : (
              <>
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                Retry Conversion
              </>
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
