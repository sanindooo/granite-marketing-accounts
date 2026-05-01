"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
import type { DashboardMetrics, LastRun, SyncCoverage, PendingAction, RunningJob, FxError } from "@/lib/queries/dashboard";
import { fetchDashboardMetrics, fetchLastRuns, fetchSyncCoverage, fetchPendingActions, cancelRun, fetchRunningJobs, fetchFxErrors } from "@/lib/actions/dashboard";
import type { PipelineCommand, PipelineOptions } from "@/lib/types";
import { usePipelineStream } from "@/hooks/use-pipeline-stream";
import { apiFetch } from "@/lib/api-fetch";
import { NeedsAttentionCard } from "./needs-attention-card";
import { FxErrorsCard } from "./fx-errors-card";
import { StaleRunModal } from "./stale-run-modal";

const PIPELINE_COMMANDS: { key: PipelineCommand; label: string; description: string }[] = [
  { key: "syncEmails", label: "Sync emails", description: "Fetch new invoices from MS365" },
  { key: "processInvoices", label: "Process invoices", description: "Classify and file invoices" },
  // Reconciliation hidden until real bank statement matching is implemented
];

function formatRunningStats(statsJson: string | null, operation: string): string | null {
  if (!statsJson) return null;
  try {
    const stats = JSON.parse(statsJson);
    // Empty stats means the job just started
    if (Object.keys(stats).length === 0) {
      return "Starting...";
    }
    if (operation === "ingest_email") {
      const phase = stats.phase || "sync";
      const emails = stats.emails || 0;
      const skipped = stats.skipped || 0;
      const scanned = stats.scanned || 0;
      if (phase === "backfill" || phase === "search" || phase === "scan") {
        if (scanned > 0) {
          return `Scanned ${scanned}: ${emails} new, ${skipped} already synced`;
        }
        return emails > 0 ? `Found ${emails} new emails` : "Searching inbox...";
      } else if (phase === "delta_setup") {
        return `Sync done. Establishing checkpoint for future runs...`;
      } else if (phase === "backfill_delta") {
        return `Sync done. Establishing checkpoint (takes a few minutes for large inboxes)...`;
      } else if (phase === "incremental") {
        return `Synced ${emails} new emails`;
      } else {
        return emails > 0 ? `Processing ${emails} emails` : "Connecting to Microsoft 365...";
      }
    } else if (operation === "ingest_invoice") {
      const processed = stats.processed || 0;
      const total = stats.total || 0;
      return total > 0 ? `Processing ${processed}/${total}` : `Processed ${processed}`;
    } else if (operation === "reconcile") {
      const matched = stats.matched || 0;
      return `Matched ${matched} transactions`;
    }
    return null;
  } catch {
    return null;
  }
}

function formatElapsedTime(startedAt: string | null): string {
  if (!startedAt) return "";
  const start = new Date(startedAt);
  const now = new Date();
  const diffMs = now.getTime() - start.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffSecs = Math.floor((diffMs % 60000) / 1000);

  if (diffMins > 60) {
    const hours = Math.floor(diffMins / 60);
    const mins = diffMins % 60;
    return `${hours}h ${mins}m`;
  } else if (diffMins > 0) {
    return `${diffMins}m ${diffSecs}s`;
  }
  return `${diffSecs}s`;
}

function isRunStale(startedAt: string | null, statsJson: string | null): boolean {
  if (!startedAt) return false;
  const start = new Date(startedAt);
  const now = new Date();
  const diffMins = (now.getTime() - start.getTime()) / 60000;

  // Consider stale if running > 2 minutes with no stats updates
  // or > 10 minutes regardless of stats (long-running jobs should still update periodically)
  try {
    const stats = statsJson ? JSON.parse(statsJson) : {};
    const hasProgress = Object.keys(stats).length > 0;
    return diffMins > 10 || (diffMins > 2 && !hasProgress);
  } catch {
    return diffMins > 2;
  }
}

function getStaleRunGuidance(operation: string): string {
  switch (operation) {
    case "ingest_email":
      return "The sync may have lost connection to Microsoft 365. Try cancelling and running again. If this keeps happening, you may need to re-authenticate (run 'granite ops reauth ms365' in terminal).";
    case "ingest_invoice":
      return "Processing may have stalled due to an API error. Try cancelling and running again with a smaller batch (set Process limit to 10).";
    case "reconcile":
      return "Reconciliation may be stuck. Try cancelling and running again.";
    default:
      return "This job appears to be stuck. Try cancelling and running again.";
  }
}

function formatCompletedStats(statsJson: string | null, operation: string): string | null {
  if (!statsJson) return null;
  try {
    const stats = JSON.parse(statsJson);
    if (Object.keys(stats).length === 0) return null;

    if (operation === "ingest_email") {
      const emails = stats.emails || 0;
      const skipped = stats.skipped || 0;
      if (emails > 0 && skipped > 0) {
        return `Synced ${emails} new emails (${skipped} already in database)`;
      } else if (emails > 0) {
        return `Synced ${emails} new emails`;
      } else if (skipped > 0) {
        return `No new emails (${skipped} already synced)`;
      }
      return "Completed";
    } else if (operation === "ingest_invoice") {
      const processed = stats.processed || 0;
      const filed = stats.filed || 0;
      const errors = stats.errors || 0;
      const duplicates = stats.duplicates || 0;
      const parts: string[] = [];
      if (filed > 0) parts.push(`${filed} filed`);
      if (duplicates > 0) parts.push(`${duplicates} duplicates`);
      if (errors > 0) parts.push(`${errors} errors`);
      if (parts.length > 0) {
        return `Processed ${processed}: ${parts.join(", ")}`;
      }
      return `Processed ${processed} emails`;
    } else if (operation === "reconcile") {
      const matched = stats.matched || 0;
      const unmatched = stats.unmatched || 0;
      if (matched > 0 || unmatched > 0) {
        return `${matched} matched, ${unmatched} unmatched`;
      }
      return "Completed";
    }
    return null;
  } catch {
    return null;
  }
}

export function DashboardContent() {
  const [fy] = useQueryState("fy", parseAsString.withDefault(getCurrentFY()));
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
  const [lastRuns, setLastRuns] = useState<LastRun[]>([]);
  const [syncCoverage, setSyncCoverage] = useState<SyncCoverage | null>(null);
  const [pendingActions, setPendingActions] = useState<PendingAction[]>([]);
  const [fxErrors, setFxErrors] = useState<FxError[]>([]);
  const [loading, setLoading] = useState(true);
  const stream = usePipelineStream();

  const [showFilters, setShowFilters] = useState(false);
  const [pipelineFilters, setPipelineFilters] = useState<{
    dateFrom: string;
    dateTo: string;
    senderSearch: string;
    limit?: number;
    backfillFrom: string;
    rescan: boolean;
    workers: number;
    model: "claude" | "openai";
    processFy: string;
  }>({
    dateFrom: "",
    dateTo: "",
    senderSearch: "",
    limit: undefined,
    backfillFrom: "",
    rescan: false,
    workers: 5,
    model: "openai",
    processFy: "",
  });

  // Modal state for stale run detection
  const [staleRunModal, setStaleRunModal] = useState<{
    open: boolean;
    command: PipelineCommand | null;
    runningJobs: RunningJob[];
    operationLabel: string;
  }>({
    open: false,
    command: null,
    runningJobs: [],
    operationLabel: "",
  });

  const refreshAllData = useCallback(async () => {
    const [metricsResult, runsResult, coverageResult, actionsResult, fxErrorsResult] = await Promise.all([
      fetchDashboardMetrics(fy),
      fetchLastRuns(),
      fetchSyncCoverage(),
fetchPendingActions(fy === "all" ? undefined : fy),
      fetchFxErrors(),
    ]);
    if (metricsResult.ok) setMetrics(metricsResult.data);
    if (runsResult.ok) setLastRuns(runsResult.data);
    if (coverageResult.ok) setSyncCoverage(coverageResult.data);
    if (actionsResult.ok) setPendingActions(actionsResult.data);
    if (fxErrorsResult.ok) setFxErrors(fxErrorsResult.data);
  }, [fy]);

  // Reset Needs Attention rows (all or a selection) and hand off to the
  // existing pipeline-stream hook so the bottom Pipeline Controls show
  // progress, errors surface as toasts, and isRunning stays true throughout.
  // Used by both the "Retry all" and "Retry selected" buttons on the Needs
  // Attention card.
  const runRetry = useCallback(
    async ({ msgIds }: { msgIds?: string[] }) => {
      const body = msgIds && msgIds.length > 0 ? { msgIds } : { all: true };
      const reset = await apiFetch("/api/pipeline/retry-errors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!reset.ok) {
        const err = await reset.json().catch(() => ({}));
        toast.error(err.error?.message || err.error || "Retry failed");
        return;
      }
      const payload = await reset.json();
      const count = payload?.result?.reset ?? 0;
      if (count === 0) {
        toast.info("Nothing to retry — no eligible emails were reset");
        return;
      }
      toast.success(
        msgIds
          ? `Reset ${count} of ${msgIds.length} selected emails — running pipeline now…`
          : `Reset ${count} emails for re-processing — running pipeline now…`
      );
      // Hand off to the hook — it parses SSE events, fires error toasts,
      // tracks isRunning so the bottom Pipeline Controls reflect retry state,
      // and refreshAllData fires from the existing transition handler when
      // the run completes.
      await stream.run("processInvoices");
    },
    [stream]
  );

  useEffect(() => {
    async function loadData() {
      setLoading(true);
      try {
        await refreshAllData();
      } catch (err) {
        console.error("Failed to load metrics:", err);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [refreshAllData]);

  const prevRunningRef = useRef(false);

  const getOperationForCommand = (command: PipelineCommand): "ingest_email" | "ingest_invoice" | "reconcile" => {
    return command === "syncEmails"
      ? "ingest_email"
      : command === "processInvoices"
      ? "ingest_invoice"
      : "reconcile";
  };

  const getLabelForCommand = (command: PipelineCommand): string => {
    return command === "syncEmails"
      ? "Sync emails"
      : command === "processInvoices"
      ? "Process invoices"
      : "Reconciliation";
  };

  const startCommand = async (command: PipelineCommand) => {
    const options: PipelineOptions = { fiscalYear: fy };
    if (pipelineFilters.senderSearch) options.sender = pipelineFilters.senderSearch;
    if (pipelineFilters.dateFrom) options.dateFrom = pipelineFilters.dateFrom;
    if (pipelineFilters.dateTo) options.dateTo = pipelineFilters.dateTo;
    if (pipelineFilters.limit) options.limit = pipelineFilters.limit;
    if (pipelineFilters.backfillFrom) options.backfillFrom = pipelineFilters.backfillFrom;
    if (pipelineFilters.rescan) options.rescan = pipelineFilters.rescan;
    // Only pass workers, model, and processFy for processInvoices command
    if (command === "processInvoices") {
      options.workers = pipelineFilters.workers;
      options.model = pipelineFilters.model;
      if (pipelineFilters.processFy) {
        options.fiscalYear = pipelineFilters.processFy;
      }
    }

    await stream.run(command, options);
  };

  const handleRunCommand = async (command: PipelineCommand) => {
    const operation = getOperationForCommand(command);

    // Check for existing running jobs
    const result = await fetchRunningJobs(operation);
    if (result.ok && result.data.length > 0) {
      // Show modal to ask user what to do
      setStaleRunModal({
        open: true,
        command,
        runningJobs: result.data,
        operationLabel: getLabelForCommand(command),
      });
      return;
    }

    // No running jobs, start immediately
    await startCommand(command);
  };

  const handleCancelAndStart = async () => {
    if (!staleRunModal.command) return;

    const operation = getOperationForCommand(staleRunModal.command);
    const cancelResult = await cancelRun(operation);

    if (cancelResult.ok) {
      toast.success(`Cancelled ${cancelResult.data.cancelled} stale job(s)`);
    }

    setStaleRunModal({ open: false, command: null, runningJobs: [], operationLabel: "" });
    await refreshAllData();
    await startCommand(staleRunModal.command);
  };

  const handleKeepWaiting = () => {
    setStaleRunModal({ open: false, command: null, runningJobs: [], operationLabel: "" });
  };

  // Handle stream completion - only react when isRunning transitions from true to false
  useEffect(() => {
    const wasRunning = prevRunningRef.current;
    prevRunningRef.current = stream.isRunning;

    if (wasRunning && !stream.isRunning) {
      if (stream.result) {
        toast.success("Command completed successfully");
        // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: refreshing data after external process completes
        refreshAllData();
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
  }, [stream.isRunning, stream.result, stream.error, refreshAllData]);

  // Poll only when a DB-tracked run is active without a connected stream
  // (e.g. another browser tab kicked it off, or the user navigated here
  // mid-run). When `stream.isRunning` is true the SSE connection delivers
  // per-event updates and the completion handler above does the
  // refreshAllData call; another 5s poll on top of that hammers the
  // sync-only better-sqlite3 connection for no UI win.
  const hasRunningInDb = lastRuns.some((r) => r.status === "running");
  useEffect(() => {
    if (stream.isRunning) return;
    if (!hasRunningInDb) return;

    const interval = setInterval(() => {
      refreshAllData();
    }, 5000); // Poll every 5 seconds for responsive feedback

    return () => clearInterval(interval);
  }, [stream.isRunning, hasRunningInDb, refreshAllData]);

  const handleCancelRun = async (operation: "ingest_email" | "ingest_invoice" | "reconcile") => {
    const result = await cancelRun(operation);
    if (result.ok) {
      toast.success(`Cancelled ${result.data.cancelled} running job(s)`);
      await refreshAllData();
    } else {
      toast.error("Failed to cancel run");
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
        <NeedsAttentionCard
          pendingActions={pendingActions}
          onDismiss={async (msgId, reason, blockDomain) => {
            await apiFetch("/api/emails/dismiss", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ msgId, reason, blockDomain }),
            });
            const result = await fetchPendingActions();
            if (result.ok) setPendingActions(result.data);
            const message = blockDomain
              ? "Marked as not an invoice and domain blocked"
              : reason === "not_invoice"
              ? "Marked as not an invoice"
              : "Marked as resolved";
            toast.success(message);
          }}
          onBulkDismiss={async (msgIds, reason) => {
            const response = await apiFetch("/api/emails/bulk-dismiss", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ msgIds, reason }),
            });
            if (response.ok) {
              const result = await fetchPendingActions();
              if (result.ok) setPendingActions(result.data);
              toast.success(`Marked ${msgIds.length} emails as not invoices`);
            } else {
              toast.error("Failed to dismiss emails");
            }
          }}
          onUploadPdf={async (msgId, file) => {
            const formData = new FormData();
            formData.append("msgId", msgId);
            formData.append("pdf", file);
            const response = await apiFetch("/api/invoices/upload", {
              method: "POST",
              body: formData,
            });
            if (response.ok) {
              toast.success("PDF uploaded and processed");
              await refreshAllData();
            } else {
              const err = await response.json();
              toast.error(err.error || "Upload failed");
            }
          }}
          onRetryAll={async () => {
            await runRetry({});
          }}
          onRetrySelected={async (msgIds) => {
            await runRetry({ msgIds });
          }}
          onReauthGoogle={async () => {
            toast.info(
              "Browser will open for Google sign-in. Approve access (including 2FA), then return here."
            );
            const response = await apiFetch("/api/auth/google/reauth", {
              method: "POST",
            });
            if (response.ok) {
              toast.success("Google access restored");
              await refreshAllData();
            } else {
              const err = await response.json();
              toast.error(
                err.error?.user_message ||
                  err.error?.message ||
                  "Re-authentication failed"
              );
            }
          }}
        />
      )}

      <FxErrorsCard fxErrors={fxErrors} />

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

                {!pipelineFilters.backfillFrom ? (
                  <>
                    <div className="space-y-2">
                      <p className="text-xs text-muted-foreground">
                        Set a date range to search your inbox for emails in that period. Without filters, sync only fetches emails that arrived since the last sync.
                      </p>
                    </div>

                    <div className="grid gap-4 sm:grid-cols-3">
                      <div className="space-y-2">
                        <label className="text-sm font-medium">From date</label>
                        <Input
                          type="date"
                          value={pipelineFilters.dateFrom}
                          onChange={(e) =>
                            setPipelineFilters((f) => ({
                              ...f,
                              dateFrom: e.target.value,
                              backfillFrom: "",
                            }))
                          }
                        />
                      </div>
                      <div className="space-y-2">
                        <label className="text-sm font-medium">To date</label>
                        <Input
                          type="date"
                          value={pipelineFilters.dateTo}
                          onChange={(e) =>
                            setPipelineFilters((f) => ({
                              ...f,
                              dateTo: e.target.value,
                              backfillFrom: "",
                            }))
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
                  </>
                ) : (
                  <div className="rounded-md border border-amber-200 bg-amber-50 p-3">
                    <p className="text-sm text-amber-800">
                      Date range filters are hidden because backfill is active. Backfill fetches all emails from the specified date and sets up delta sync for future incremental fetches. Clear the backfill date below to use date range instead.
                    </p>
                  </div>
                )}

                <div className="border-t pt-4">
                  {pipelineFilters.backfillFrom || !(pipelineFilters.dateFrom || pipelineFilters.dateTo) ? (
                    <div className="space-y-2">
                      <label className="text-sm font-medium">Backfill historical emails</label>
                      <div className="flex gap-2 items-end">
                        <Input
                          type="date"
                          value={pipelineFilters.backfillFrom}
                          onChange={(e) =>
                            setPipelineFilters((f) => ({
                              ...f,
                              backfillFrom: e.target.value,
                              dateFrom: "",
                              dateTo: "",
                            }))
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
                  ) : (
                    <div className="rounded-md border border-blue-200 bg-blue-50 p-3">
                      <p className="text-sm text-blue-800">
                        Backfill is hidden because date range is active. Date range performs a one-off search without setting up delta sync. Clear the date range above to use backfill instead.
                      </p>
                    </div>
                  )}
                </div>

                <div className="border-t pt-4">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={pipelineFilters.rescan}
                      onChange={(e) =>
                        setPipelineFilters((f) => ({ ...f, rescan: e.target.checked }))
                      }
                      className="h-4 w-4 rounded border-gray-300"
                    />
                    <span className="text-sm font-medium">Re-scan already synced emails</span>
                  </label>
                  <p className="text-xs text-muted-foreground mt-1 ml-6">
                    Re-fetch emails even if already in the database. Clears their processing status so they get re-classified and re-extracted.
                  </p>
                </div>

                <div className="border-t pt-4">
                  <h4 className="text-sm font-medium mb-3">Processing Settings</h4>
                  <div className="grid gap-4 sm:grid-cols-3">
                    <div className="space-y-2">
                      <label className="text-sm font-medium">Fiscal year filter</label>
                      <select
                        value={pipelineFilters.processFy}
                        onChange={(e) =>
                          setPipelineFilters((f) => ({ ...f, processFy: e.target.value }))
                        }
                        className="w-full h-9 px-3 rounded-md border border-input bg-background text-sm"
                      >
                        <option value="">All pending emails</option>
                        <option value="FY-2025-26">FY-2025-26 (Mar 2025 - Feb 2026)</option>
                        <option value="FY-2024-25">FY-2024-25 (Mar 2024 - Feb 2025)</option>
                        <option value="FY-2023-24">FY-2023-24 (Mar 2023 - Feb 2024)</option>
                      </select>
                      <p className="text-xs text-muted-foreground">
                        Only process emails received in this fiscal year. Great for finding missing invoices.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <label className="text-sm font-medium">Concurrent workers</label>
                      <div className="flex items-center gap-3">
                        <input
                          type="range"
                          min={1}
                          max={20}
                          value={pipelineFilters.workers}
                          onChange={(e) =>
                            setPipelineFilters((f) => ({ ...f, workers: parseInt(e.target.value, 10) }))
                          }
                          className="flex-1 h-2 bg-muted rounded-lg appearance-none cursor-pointer"
                        />
                        <span className="text-sm font-mono tabular-nums w-6 text-right">
                          {pipelineFilters.workers}
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Process multiple emails in parallel. Higher = faster but uses more API quota.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <label className="text-sm font-medium">LLM provider</label>
                      <select
                        value={pipelineFilters.model}
                        onChange={(e) =>
                          setPipelineFilters((f) => ({ ...f, model: e.target.value as "claude" | "openai" }))
                        }
                        className="w-full h-9 px-3 rounded-md border border-input bg-background text-sm"
                      >
                        <option value="claude">Claude (Haiku 4.5)</option>
                        <option value="openai">OpenAI (GPT-4o-mini)</option>
                      </select>
                      <p className="text-xs text-muted-foreground">
                        Claude is more accurate. OpenAI is ~6x cheaper for bulk processing.
                      </p>
                    </div>
                  </div>
                </div>
              </div>

              {(pipelineFilters.senderSearch || pipelineFilters.dateFrom || pipelineFilters.dateTo || pipelineFilters.limit || pipelineFilters.backfillFrom || pipelineFilters.rescan || pipelineFilters.processFy) && (
                <div className="flex items-center justify-between border-t pt-3">
                  <p className="text-sm text-muted-foreground">
                    {pipelineFilters.backfillFrom && (
                      <span className="text-amber-600 font-medium">Backfill from {pipelineFilters.backfillFrom}</span>
                    )}
                    {pipelineFilters.backfillFrom && pipelineFilters.senderSearch && " • "}
                    {pipelineFilters.senderSearch && `Searching for "${pipelineFilters.senderSearch}"`}
                    {!pipelineFilters.backfillFrom && pipelineFilters.senderSearch && (pipelineFilters.dateFrom || pipelineFilters.dateTo) && " • "}
                    {!pipelineFilters.backfillFrom && pipelineFilters.dateFrom && `From ${pipelineFilters.dateFrom}`}
                    {!pipelineFilters.backfillFrom && pipelineFilters.dateFrom && pipelineFilters.dateTo && " to "}
                    {!pipelineFilters.backfillFrom && pipelineFilters.dateTo && !pipelineFilters.dateFrom && `Until ${pipelineFilters.dateTo}`}
                    {!pipelineFilters.backfillFrom && pipelineFilters.dateTo && pipelineFilters.dateFrom && pipelineFilters.dateTo}
                    {pipelineFilters.limit && ` • Limit: ${pipelineFilters.limit}`}
                    {pipelineFilters.rescan && <span className="text-purple-600 font-medium"> • Re-scan mode</span>}
                    {pipelineFilters.processFy && <span className="text-blue-600 font-medium"> • Process: {pipelineFilters.processFy}</span>}
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setPipelineFilters({ dateFrom: "", dateTo: "", senderSearch: "", limit: undefined, backfillFrom: "", rescan: false, workers: 5, model: "openai", processFy: "" })
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
              const dbOperation = cmd.key === "syncEmails"
                ? "ingest_email"
                : cmd.key === "processInvoices"
                ? "ingest_invoice"
                : "reconcile";
              const isDbRunning = lastRun?.status === "running" && !isThisRunning;
              const isStale = isDbRunning && isRunStale(lastRun?.startedAt || null, lastRun?.statsJson || null);
              const elapsedTime = isDbRunning ? formatElapsedTime(lastRun?.startedAt || null) : "";

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
                    ) : isDbRunning ? (
                      <div className="mt-2 space-y-2">
                        <div className="flex items-center gap-2 text-sm">
                          <div className={`h-2 w-2 rounded-full ${isStale ? "bg-red-500" : "bg-amber-500 animate-pulse"}`} />
                          <span className={isStale ? "text-red-600" : "text-amber-600"}>
                            {formatRunningStats(lastRun.statsJson, dbOperation) || "Running..."}
                          </span>
                          {elapsedTime && (
                            <span className="text-xs text-muted-foreground">({elapsedTime})</span>
                          )}
                        </div>
                        {isStale && (
                          <div className="rounded-md border border-red-200 bg-red-50 p-3">
                            <div className="flex items-start justify-between gap-4">
                              <div>
                                <p className="text-sm font-medium text-red-800">
                                  This job appears to be stuck
                                </p>
                                <p className="mt-1 text-xs text-red-700">
                                  {getStaleRunGuidance(dbOperation)}
                                </p>
                              </div>
                              <Button
                                variant="outline"
                                size="sm"
                                className="shrink-0 text-xs border-red-300 text-red-700 hover:bg-red-100"
                                onClick={async () => {
                                  await handleCancelRun(dbOperation as "ingest_email" | "ingest_invoice" | "reconcile");
                                  await startCommand(cmd.key);
                                }}
                                disabled={stream.isRunning}
                              >
                                Cancel & Retry
                              </Button>
                            </div>
                          </div>
                        )}
                        {!isStale && (
                          <p className="text-xs text-muted-foreground">
                            Started: {formatDateTime(lastRun.startedAt)}
                          </p>
                        )}
                      </div>
                    ) : (
                      <div className="mt-1">
                        <p className="text-xs text-muted-foreground">
                          Last run: {formatDateTime(lastRun?.completedAt || null)}
                          {lastRun?.status && lastRun.status !== "never" && (
                            <span
                              className={
                                lastRun.status === "success" || lastRun.status === "ok"
                                  ? "ml-2 text-green-600"
                                  : lastRun.status === "cancelled"
                                  ? "ml-2 text-amber-600"
                                  : "ml-2 text-red-600"
                              }
                            >
                              ({lastRun.status})
                            </span>
                          )}
                        </p>
                        {lastRun?.status === "ok" && lastRun.statsJson && (
                          <p className="text-xs text-green-600 mt-0.5">
                            {formatCompletedStats(lastRun.statsJson, dbOperation)}
                          </p>
                        )}
                        {lastRun?.status === "error" && (
                          <div className="flex items-center gap-2 mt-0.5">
                            <p className="text-xs text-red-600">
                              Something went wrong.
                            </p>
                            <Button
                              variant="link"
                              size="sm"
                              className="h-auto p-0 text-xs text-red-600 hover:text-red-700"
                              onClick={() => startCommand(cmd.key)}
                              disabled={stream.isRunning}
                            >
                              Retry →
                            </Button>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="flex gap-2">
                    {(isDbRunning || isThisRunning) && (
                      <Button
                        onClick={() => {
                          if (isThisRunning) {
                            stream.cancel();
                          }
                          handleCancelRun(dbOperation as "ingest_email" | "ingest_invoice" | "reconcile");
                        }}
                        variant="outline"
                        size="sm"
                        className={isStale ? "text-red-600 hover:text-red-700 hover:bg-red-50" : ""}
                      >
                        Cancel
                      </Button>
                    )}
                    <Button
                      onClick={() => handleRunCommand(cmd.key)}
                      disabled={stream.isRunning || isDbRunning}
                      variant={isThisRunning ? "secondary" : "default"}
                    >
                      {isThisRunning || isDbRunning ? "Running..." : "Run"}
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <StaleRunModal
        open={staleRunModal.open}
        onOpenChange={(open) => {
          if (!open) setStaleRunModal({ open: false, command: null, runningJobs: [], operationLabel: "" });
        }}
        staleRuns={staleRunModal.runningJobs.map((job) => ({
          runId: job.runId,
          operation: job.operation,
          startedAt: job.startedAt,
          runningFor: "",
          statsJson: job.statsJson || undefined,
        }))}
        operationLabel={staleRunModal.operationLabel}
        onCancelAndStart={handleCancelAndStart}
        onKeepWaiting={handleKeepWaiting}
      />
    </div>
  );
}
