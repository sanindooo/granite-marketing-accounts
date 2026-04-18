"use client";

import { useEffect, useState } from "react";
import { useQueryState, parseAsString } from "nuqs";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { getCurrentFY } from "@/lib/fiscal";
import type { DashboardMetrics, LastRun } from "@/lib/queries/dashboard";
import { fetchDashboardMetrics, fetchLastRuns } from "@/lib/actions/dashboard";
import { runPipelineCommand, type PipelineCommand } from "@/lib/actions/pipeline";

function formatCurrency(amount: number): string {
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: "GBP",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(amount);
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "Never";
  return new Date(dateStr).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const PIPELINE_COMMANDS: { key: PipelineCommand; label: string; description: string }[] = [
  { key: "syncEmails", label: "Sync emails", description: "Fetch new invoices from MS365" },
  { key: "processInvoices", label: "Process invoices", description: "Classify and file invoices" },
  { key: "runReconciliation", label: "Run reconciliation", description: "Match invoices to transactions" },
];

export function DashboardContent() {
  const [fy] = useQueryState("fy", parseAsString.withDefault(getCurrentFY()));
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
  const [lastRuns, setLastRuns] = useState<LastRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [runningCommand, setRunningCommand] = useState<PipelineCommand | null>(null);

  useEffect(() => {
    async function loadData() {
      setLoading(true);
      try {
        const [metricsResult, runsResult] = await Promise.all([
          fetchDashboardMetrics(fy),
          fetchLastRuns(),
        ]);
        if (metricsResult.ok) setMetrics(metricsResult.data);
        if (runsResult.ok) setLastRuns(runsResult.data);
      } catch (err) {
        console.error("Failed to load metrics:", err);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [fy]);

  const handleRunCommand = async (command: PipelineCommand) => {
    setRunningCommand(command);
    try {
      const result = await runPipelineCommand(command, { fiscalYear: fy });
      if (result.ok) {
        toast.success(`${command} completed successfully`);
        const [metricsResult, runsResult] = await Promise.all([
          fetchDashboardMetrics(fy),
          fetchLastRuns(),
        ]);
        if (metricsResult.ok) setMetrics(metricsResult.data);
        if (runsResult.ok) setLastRuns(runsResult.data);
      } else {
        if (result.error.code === "NEEDS_REAUTH") {
          toast.error("Authentication expired", {
            description: result.error.userMessage || "Run `granite ops reauth ms365` in terminal",
            duration: 10000,
          });
        } else {
          toast.error(result.error.message);
        }
      }
    } catch {
      toast.error("Command failed");
    } finally {
      setRunningCommand(null);
    }
  };

  if (loading) {
    return <div className="text-muted-foreground">Loading metrics...</div>;
  }

  if (!metrics) {
    return <div className="text-muted-foreground">Failed to load metrics</div>;
  }

  const matchedCount = metrics.reconStatus.find((r) => r.state === "matched")?.count || 0;
  const unmatchedCount = metrics.reconStatus.find((r) => r.state === "unmatched")?.count || 0;
  const pendingCount = metrics.reconStatus.find((r) => r.state === "pending")?.count || 0;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Invoices
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{metrics.invoiceCount}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Spend
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{formatCurrency(metrics.totalSpend)}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Matched
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-green-600">{matchedCount}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Unmatched
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-amber-600">
              {unmatchedCount + pendingCount}
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Category Breakdown</CardTitle>
          </CardHeader>
          <CardContent>
            {metrics.categoryBreakdown.length === 0 ? (
              <p className="text-muted-foreground">No data for this fiscal year</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Category</TableHead>
                    <TableHead className="text-right">Amount</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {metrics.categoryBreakdown.map((cat) => (
                    <TableRow key={cat.category}>
                      <TableCell className="capitalize">{cat.category}</TableCell>
                      <TableCell className="text-right">{formatCurrency(cat.total)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Top Vendors</CardTitle>
          </CardHeader>
          <CardContent>
            {metrics.topVendors.length === 0 ? (
              <p className="text-muted-foreground">No data for this fiscal year</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Vendor</TableHead>
                    <TableHead className="text-right">Spend</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {metrics.topVendors.map((vendor) => (
                    <TableRow key={vendor.name}>
                      <TableCell>{vendor.name}</TableCell>
                      <TableCell className="text-right">{formatCurrency(vendor.total)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Pipeline Controls</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {PIPELINE_COMMANDS.map((cmd) => {
              const lastRun = lastRuns.find(
                (r) =>
                  r.operation ===
                  (cmd.key === "syncEmails"
                    ? "ingest_email"
                    : cmd.key === "processInvoices"
                    ? "ingest_invoice"
                    : "reconcile")
              );

              return (
                <div
                  key={cmd.key}
                  className="flex items-center justify-between border-b pb-4 last:border-0 last:pb-0"
                >
                  <div>
                    <p className="font-medium">{cmd.label}</p>
                    <p className="text-sm text-muted-foreground">{cmd.description}</p>
                    <p className="text-xs text-muted-foreground">
                      Last run: {formatDate(lastRun?.completedAt || null)}
                      {lastRun?.status && lastRun.status !== "never" && (
                        <span
                          className={
                            lastRun.status === "success"
                              ? "ml-2 text-green-600"
                              : "ml-2 text-red-600"
                          }
                        >
                          ({lastRun.status})
                        </span>
                      )}
                    </p>
                  </div>
                  <Button
                    onClick={() => handleRunCommand(cmd.key)}
                    disabled={runningCommand !== null}
                    variant={runningCommand === cmd.key ? "secondary" : "default"}
                  >
                    {runningCommand === cmd.key ? "Running..." : "Run"}
                  </Button>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
