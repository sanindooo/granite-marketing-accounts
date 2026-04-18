"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryState, parseAsString } from "nuqs";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { getCurrentFY } from "@/lib/fiscal";
import { formatCurrency, formatDateTime } from "@/lib/formatters";
import type { DashboardMetrics, LastRun, SyncCoverage, PendingAction } from "@/lib/queries/dashboard";
import { fetchDashboardMetrics, fetchLastRuns, fetchSyncCoverage, fetchPendingActions } from "@/lib/actions/dashboard";
import type { PipelineCommand, PipelineOptions } from "@/lib/actions/pipeline";
import { usePipelineStream } from "@/hooks/use-pipeline-stream";

const PIPELINE_COMMANDS: { key: PipelineCommand; label: string; description: string }[] = [
  { key: "syncEmails", label: "Sync emails", description: "Fetch new invoices from MS365" },
  { key: "processInvoices", label: "Process invoices", description: "Classify and file invoices" },
  { key: "runReconciliation", label: "Run reconciliation", description: "Match invoices to transactions" },
];

export function DashboardContent() {
  const [fy] = useQueryState("fy", parseAsString.withDefault(getCurrentFY()));
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
  const [lastRuns, setLastRuns] = useState<LastRun[]>([]);
  const [syncCoverage, setSyncCoverage] = useState<SyncCoverage | null>(null);
  const [pendingActions, setPendingActions] = useState<PendingAction[]>([]);
  const [loading, setLoading] = useState(true);
  const stream = usePipelineStream();

  const [showFilters, setShowFilters] = useState(false);
  const [pipelineFilters, setPipelineFilters] = useState<{
    dateFrom: string;
    dateTo: string;
    senderSearch: string;
    limit?: number;
    backfillFrom: string;
  }>({
    dateFrom: "",
    dateTo: "",
    senderSearch: "",
    limit: undefined,
    backfillFrom: "",
  });

  useEffect(() => {
    async function loadData() {
      setLoading(true);
      try {
        const [metricsResult, runsResult, coverageResult, actionsResult] = await Promise.all([
          fetchDashboardMetrics(fy),
          fetchLastRuns(),
          fetchSyncCoverage(),
          fetchPendingActions(),
        ]);
        if (metricsResult.ok) setMetrics(metricsResult.data);
        if (runsResult.ok) setLastRuns(runsResult.data);
        if (coverageResult.ok) setSyncCoverage(coverageResult.data);
        if (actionsResult.ok) setPendingActions(actionsResult.data);
      } catch (err) {
        console.error("Failed to load metrics:", err);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [fy]);

  const prevRunningRef = useRef(false);

  const handleRunCommand = async (command: PipelineCommand) => {
    const options: PipelineOptions = { fiscalYear: fy };
    if (pipelineFilters.senderSearch) options.sender = pipelineFilters.senderSearch;
    if (pipelineFilters.dateFrom) options.dateFrom = pipelineFilters.dateFrom;
    if (pipelineFilters.dateTo) options.dateTo = pipelineFilters.dateTo;
    if (pipelineFilters.limit) options.limit = pipelineFilters.limit;
    if (pipelineFilters.backfillFrom) options.backfillFrom = pipelineFilters.backfillFrom;

    await stream.run(command, options);
  };

  // Handle stream completion - only react when isRunning transitions from true to false
  useEffect(() => {
    const wasRunning = prevRunningRef.current;
    prevRunningRef.current = stream.isRunning;

    if (wasRunning && !stream.isRunning) {
      if (stream.result) {
        toast.success("Command completed successfully");
        Promise.all([
          fetchDashboardMetrics(fy),
          fetchLastRuns(),
          fetchSyncCoverage(),
          fetchPendingActions(),
        ]).then(([metricsResult, runsResult, coverageResult, actionsResult]) => {
          if (metricsResult.ok) setMetrics(metricsResult.data);
          if (runsResult.ok) setLastRuns(runsResult.data);
          if (coverageResult.ok) setSyncCoverage(coverageResult.data);
          if (actionsResult.ok) setPendingActions(actionsResult.data);
        });
      } else if (stream.error) {
        if (stream.error.error_code === "needs_reauth") {
          toast.error("Authentication expired", {
            description: stream.error.user_message || "Run `granite ops reauth ms365` in terminal",
            duration: 10000,
          });
        } else {
          toast.error(stream.error.message);
        }
      }
    }
  }, [stream.isRunning, stream.result, stream.error, fy]);

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
            <div className="text-2xl font-bold font-mono tabular-nums">
              {metrics.invoiceCount}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Spend
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono tabular-nums">
              {formatCurrency(metrics.totalSpend)}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Matched
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono tabular-nums text-green-600">
              {matchedCount}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Unmatched
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono tabular-nums text-amber-600">
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
                      <TableCell className="text-right font-mono tabular-nums">
                        {formatCurrency(cat.total)}
                      </TableCell>
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
                      <TableCell className="text-right font-mono tabular-nums">
                        {formatCurrency(vendor.total)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>

      {pendingActions.length > 0 && (
        <Card className="border-amber-200 bg-amber-50/50">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-amber-800">
              <span className="inline-flex h-2 w-2 rounded-full bg-amber-500" />
              Needs Attention ({pendingActions.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>From</TableHead>
                  <TableHead>Subject</TableHead>
                  <TableHead>Date</TableHead>
                  <TableHead>Issue</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pendingActions.map((action) => (
                  <TableRow key={action.msgId}>
                    <TableCell className="max-w-32 truncate text-sm">
                      {action.fromAddr}
                    </TableCell>
                    <TableCell className="max-w-64 truncate text-sm">
                      {action.subject}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {new Date(action.receivedAt).toLocaleDateString()}
                    </TableCell>
                    <TableCell>
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                        action.outcome === "needs_manual_download"
                          ? "bg-amber-100 text-amber-800"
                          : "bg-red-100 text-red-800"
                      }`}>
                        {action.outcome === "needs_manual_download"
                          ? "Manual download needed"
                          : "Processing error"}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <p className="mt-3 text-xs text-muted-foreground">
              These emails contain invoices that could not be automatically processed.
              Visit the vendor portal to download the PDF manually.
            </p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Pipeline Controls</CardTitle>
            {syncCoverage && syncCoverage.emailCount > 0 && (
              <p className="mt-1 text-sm text-muted-foreground">
                Synced: {syncCoverage.emailCount} emails
                {syncCoverage.earliestEmail && syncCoverage.latestEmail && (
                  <> from {new Date(syncCoverage.earliestEmail).toLocaleDateString()} to {new Date(syncCoverage.latestEmail).toLocaleDateString()}</>
                )}
              </p>
            )}
            {syncCoverage && syncCoverage.emailCount === 0 && (
              <p className="mt-1 text-sm text-amber-600">
                No emails synced yet. Run &quot;Sync emails&quot; to fetch invoices.
              </p>
            )}
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowFilters(!showFilters)}
          >
            {showFilters ? "Hide filters" : "Show filters"}
          </Button>
        </CardHeader>
        <CardContent>
          {showFilters && (
            <div className="mb-6 space-y-4 rounded-md border bg-muted/30 p-4">
              <div className="space-y-4">
                <div className="space-y-2">
                  <label className="text-sm font-medium">Search vendor/sender</label>
                  <div className="flex gap-2">
                    <Input
                      type="text"
                      placeholder="e.g. Uber, Anthropic, Adobe..."
                      value={pipelineFilters.senderSearch}
                      onChange={(e) =>
                        setPipelineFilters((f) => ({ ...f, senderSearch: e.target.value }))
                      }
                      className="max-w-xs"
                    />
                    {pipelineFilters.senderSearch && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          window.location.href = `/invoices?search=${encodeURIComponent(pipelineFilters.senderSearch)}`;
                        }}
                      >
                        View existing →
                      </Button>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Sync will search your inbox for emails from this sender and fetch new invoices
                  </p>
                </div>

                <div className="grid gap-4 sm:grid-cols-3">
                  <div className="space-y-2">
                    <label className="text-sm font-medium">From date</label>
                    <Input
                      type="date"
                      value={pipelineFilters.dateFrom}
                      onChange={(e) =>
                        setPipelineFilters((f) => ({ ...f, dateFrom: e.target.value }))
                      }
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm font-medium">To date</label>
                    <Input
                      type="date"
                      value={pipelineFilters.dateTo}
                      onChange={(e) =>
                        setPipelineFilters((f) => ({ ...f, dateTo: e.target.value }))
                      }
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm font-medium">Process limit</label>
                    <Input
                      type="number"
                      placeholder="All"
                      min={1}
                      max={100}
                      value={pipelineFilters.limit || ""}
                      onChange={(e) =>
                        setPipelineFilters((f) => ({
                          ...f,
                          limit: e.target.value ? parseInt(e.target.value, 10) : undefined,
                        }))
                      }
                    />
                  </div>
                </div>

                <div className="border-t pt-4">
                  <div className="space-y-2">
                    <label className="text-sm font-medium">Backfill historical emails</label>
                    <div className="flex gap-2 items-end">
                      <Input
                        type="date"
                        value={pipelineFilters.backfillFrom}
                        onChange={(e) =>
                          setPipelineFilters((f) => ({ ...f, backfillFrom: e.target.value }))
                        }
                        className="max-w-xs"
                      />
                      {pipelineFilters.backfillFrom && (
                        <span className="text-sm text-muted-foreground pb-2">
                          Will fetch all emails from {pipelineFilters.backfillFrom} and set up incremental sync
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Use this to capture historical invoices that were missed by initial sync
                    </p>
                  </div>
                </div>
              </div>

              {(pipelineFilters.senderSearch || pipelineFilters.dateFrom || pipelineFilters.dateTo || pipelineFilters.limit || pipelineFilters.backfillFrom) && (
                <div className="flex items-center justify-between border-t pt-3">
                  <p className="text-sm text-muted-foreground">
                    {pipelineFilters.backfillFrom && (
                      <span className="text-amber-600 font-medium">Backfill from {pipelineFilters.backfillFrom}</span>
                    )}
                    {pipelineFilters.backfillFrom && pipelineFilters.senderSearch && " • "}
                    {pipelineFilters.senderSearch && `Searching for "${pipelineFilters.senderSearch}"`}
                    {pipelineFilters.senderSearch && (pipelineFilters.dateFrom || pipelineFilters.dateTo) && " • "}
                    {pipelineFilters.dateFrom && `From ${pipelineFilters.dateFrom}`}
                    {pipelineFilters.dateFrom && pipelineFilters.dateTo && " to "}
                    {pipelineFilters.dateTo && !pipelineFilters.dateFrom && `Until ${pipelineFilters.dateTo}`}
                    {pipelineFilters.dateTo && pipelineFilters.dateFrom && pipelineFilters.dateTo}
                    {pipelineFilters.limit && ` • Limit: ${pipelineFilters.limit}`}
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setPipelineFilters({ dateFrom: "", dateTo: "", senderSearch: "", limit: undefined, backfillFrom: "" })
                    }
                  >
                    Clear
                  </Button>
                </div>
              )}
            </div>
          )}
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

              const hasPending = cmd.key === "processInvoices" && metrics.pendingEmails > 0;
              const isThisRunning = stream.isRunning && stream.activeCommand === cmd.key;

              return (
                <div
                  key={cmd.key}
                  className="flex items-center justify-between border-b pb-4 last:border-0 last:pb-0"
                >
                  <div className="flex-1">
                    <p className="font-medium">
                      {cmd.label}
                      {hasPending && (
                        <span className="ml-2 inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
                          {metrics.pendingEmails} pending
                        </span>
                      )}
                    </p>
                    <p className="text-sm text-muted-foreground">{cmd.description}</p>
                    {isThisRunning && stream.progress ? (
                      <div className="mt-2 space-y-1">
                        <div className="flex items-center gap-2 text-sm">
                          <div className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
                          <span className="text-blue-600">{stream.progress.detail}</span>
                        </div>
                        {stream.progress.total > 0 && (
                          <div className="flex items-center gap-2">
                            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                              <div
                                className="h-full bg-blue-500 transition-all duration-300"
                                style={{
                                  width: `${Math.min(100, (stream.progress.current / stream.progress.total) * 100)}%`,
                                }}
                              />
                            </div>
                            <span className="text-xs text-muted-foreground tabular-nums">
                              {stream.progress.current}/{stream.progress.total}
                            </span>
                          </div>
                        )}
                      </div>
                    ) : (
                      <p className="text-xs text-muted-foreground">
                        Last run: {formatDateTime(lastRun?.completedAt || null)}
                        {lastRun?.status && lastRun.status !== "never" && (
                          <span
                            className={
                              lastRun.status === "success" || lastRun.status === "ok"
                                ? "ml-2 text-green-600"
                                : "ml-2 text-red-600"
                            }
                          >
                            ({lastRun.status})
                          </span>
                        )}
                      </p>
                    )}
                  </div>
                  <Button
                    onClick={() => handleRunCommand(cmd.key)}
                    disabled={stream.isRunning}
                    variant={isThisRunning ? "secondary" : "default"}
                  >
                    {isThisRunning ? "Running..." : "Run"}
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
