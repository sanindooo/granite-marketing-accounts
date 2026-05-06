"use client";

import { useEffect, useState } from "react";
import { useQueryStates, parseAsString } from "nuqs";
import { useDebouncedCallback } from "use-debounce";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { toast } from "sonner";
import { fetchInboxEmails, fetchInboxCounts, rejectEmails } from "@/lib/actions/inbox";
import { usePipelineStream } from "@/hooks/use-pipeline-stream";
import type { InboxEmailRow, InboxCounts } from "@/lib/queries/inbox";

type View = "unprocessed" | "all";

export function InboxContent() {
  const [filters, setFilters] = useQueryStates(
    {
      view: parseAsString.withDefault("unprocessed"),
      sender: parseAsString,
      dateFrom: parseAsString,
      dateTo: parseAsString,
    },
    { shallow: true }
  );

  const [localSender, setLocalSender] = useState(filters.sender || "");
  const [emails, setEmails] = useState<InboxEmailRow[]>([]);
  const [counts, setCounts] = useState<InboxCounts>({ unprocessed: 0, all: 0 });
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [rejecting, setRejecting] = useState(false);
  const stream = usePipelineStream();

  useEffect(() => {
    setLocalSender(filters.sender || "");
  }, [filters.sender]);

  const debouncedSetSender = useDebouncedCallback((value: string) => {
    setFilters({ sender: value || null });
  }, 300);

  const { view, sender, dateFrom, dateTo } = filters;

  const toggleSelection = (msgId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(msgId)) {
        next.delete(msgId);
      } else {
        next.add(msgId);
      }
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === emails.length && emails.length > 0) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(emails.map((e) => e.msgId)));
    }
  };

  useEffect(() => {
    let cancelled = false;

    async function loadData() {
      setLoading(true);
      setSelectedIds(new Set());
      try {
        const [emailsResult, countsResult] = await Promise.all([
          fetchInboxEmails({
            view: view as View,
            sender: sender || undefined,
            dateFrom: dateFrom || undefined,
            dateTo: dateTo || undefined,
          }),
          fetchInboxCounts(),
        ]);

        if (cancelled) return;

        if (emailsResult.ok) {
          setEmails(emailsResult.data);
        }
        if (countsResult.ok) {
          setCounts(countsResult.data);
        }
      } catch (err) {
        console.error("Failed to load inbox:", err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    loadData();

    return () => {
      cancelled = true;
    };
  }, [view, sender, dateFrom, dateTo]);

  const hasActiveFilters = sender || dateFrom || dateTo;
  const hasSelection = selectedIds.size > 0;

  const clearFilters = () => {
    setLocalSender("");
    setFilters({
      sender: null,
      dateFrom: null,
      dateTo: null,
    });
  };

  const refreshData = async () => {
    const [emailsResult, countsResult] = await Promise.all([
      fetchInboxEmails({
        view: view as View,
        sender: sender || undefined,
        dateFrom: dateFrom || undefined,
        dateTo: dateTo || undefined,
      }),
      fetchInboxCounts(),
    ]);
    if (emailsResult.ok) setEmails(emailsResult.data);
    if (countsResult.ok) setCounts(countsResult.data);
  };

  const handleReject = async () => {
    if (selectedIds.size === 0) return;
    setRejecting(true);
    try {
      const result = await rejectEmails(Array.from(selectedIds));
      if (result.ok) {
        toast.success(`Rejected ${result.data.count} emails`);
        setSelectedIds(new Set());
        await refreshData();
      } else {
        toast.error(result.error.message);
      }
    } finally {
      setRejecting(false);
    }
  };

  const handleProcessSelected = async () => {
    if (selectedIds.size === 0) return;
    toast.info(`Processing ${selectedIds.size} selected emails...`);
    setSelectedIds(new Set());
    await stream.run("processInvoices", { msgIds: Array.from(selectedIds) });
  };

  // Handle stream completion
  useEffect(() => {
    if (!stream.isRunning && stream.result) {
      toast.success("Processing complete");
      refreshData();
    } else if (!stream.isRunning && stream.error) {
      if (stream.error.error_code === "needs_reauth") {
        toast.error("Authentication expired", {
          description: stream.error.user_message || "Run `granite ops reauth ms365` in terminal",
          duration: 10000,
        });
      } else {
        toast.error(stream.error.message);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.isRunning, stream.result, stream.error]);

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString();
  };

  const formatOutcome = (outcome: string | null) => {
    if (!outcome) return null;

    const labels: Record<string, { label: string; className: string }> = {
      invoice: { label: "Invoice", className: "bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-200" },
      receipt: { label: "Receipt", className: "bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-200" },
      statement: { label: "Statement", className: "bg-purple-100 text-purple-800 dark:bg-purple-900/50 dark:text-purple-200" },
      neither: { label: "Not Invoice", className: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200" },
      error: { label: "Error", className: "bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200" },
      pending: { label: "Pending", className: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" },
      duplicate_resend: { label: "Duplicate", className: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200" },
    };

    const info = labels[outcome] || { label: outcome, className: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200" };

    return (
      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${info.className}`}>
        {info.label}
      </span>
    );
  };

  return (
    <Card>
      <CardHeader className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <CardTitle>Synced Emails</CardTitle>
            {hasSelection && (
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">
                  {selectedIds.size} selected
                </span>
                <Button
                  variant="default"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={handleProcessSelected}
                  disabled={stream.isRunning || rejecting}
                >
                  {stream.isRunning ? "Processing..." : "Process selected"}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs text-red-600 border-red-200 hover:bg-red-50 dark:text-red-400 dark:border-red-800 dark:hover:bg-red-950/50"
                  onClick={handleReject}
                  disabled={rejecting || stream.isRunning}
                >
                  {rejecting ? "Rejecting..." : "Reject"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => setSelectedIds(new Set())}
                >
                  Clear
                </Button>
              </div>
            )}
          </div>
          <div className="flex gap-1 rounded-lg bg-muted p-1">
            <Button
              variant={view === "unprocessed" ? "default" : "ghost"}
              size="sm"
              className="h-7 text-xs"
              onClick={() => setFilters({ view: "unprocessed" })}
            >
              Unprocessed ({counts.unprocessed})
            </Button>
            <Button
              variant={view === "all" ? "default" : "ghost"}
              size="sm"
              className="h-7 text-xs"
              onClick={() => setFilters({ view: "all" })}
            >
              All Synced ({counts.all})
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Input
            type="search"
            value={localSender}
            onChange={(e) => {
              setLocalSender(e.target.value);
              debouncedSetSender(e.target.value);
            }}
            placeholder="Search sender..."
            className="max-w-xs"
          />
          <Input
            type="date"
            value={dateFrom || ""}
            onChange={(e) => setFilters({ dateFrom: e.target.value || null })}
            className="w-36"
          />
          <span className="text-sm text-muted-foreground">to</span>
          <Input
            type="date"
            value={dateTo || ""}
            onChange={(e) => setFilters({ dateTo: e.target.value || null })}
            className="w-36"
          />
          {hasActiveFilters && (
            <Button variant="ghost" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {stream.isRunning && stream.progress && (
          <div className="mb-4 space-y-2">
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
        )}
        {loading ? (
          <div className="text-muted-foreground">Loading emails...</div>
        ) : emails.length === 0 ? (
          <div className="py-8 text-center text-muted-foreground">
            {hasActiveFilters
              ? "No emails match your filters."
              : view === "unprocessed"
              ? "No unprocessed emails. All caught up!"
              : "No synced emails yet. Run sync from the Dashboard."}
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <Checkbox
                    checked={selectedIds.size === emails.length && emails.length > 0}
                    onCheckedChange={toggleSelectAll}
                    aria-label="Select all"
                  />
                </TableHead>
                <TableHead>From</TableHead>
                <TableHead>Subject</TableHead>
                <TableHead>Date</TableHead>
                {view === "all" && <TableHead>Status</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {emails.map((email) => (
                <TableRow key={email.msgId}>
                  <TableCell className="w-10">
                    <Checkbox
                      checked={selectedIds.has(email.msgId)}
                      onCheckedChange={() => toggleSelection(email.msgId)}
                      aria-label={`Select ${email.subject}`}
                    />
                  </TableCell>
                  <TableCell className="max-w-40 truncate text-sm">
                    {email.fromAddr}
                  </TableCell>
                  <TableCell className="max-w-80 truncate text-sm">
                    {email.subject}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDate(email.receivedAt)}
                  </TableCell>
                  {view === "all" && (
                    <TableCell>
                      {email.processedAt ? formatOutcome(email.outcome) : (
                        <span className="text-xs text-muted-foreground">Not processed</span>
                      )}
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
