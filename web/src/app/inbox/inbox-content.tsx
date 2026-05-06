"use client";

import { useEffect, useState } from "react";
import { useQueryStates, parseAsString } from "nuqs";
import { useDebouncedCallback } from "use-debounce";
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
import { fetchInboxEmails, fetchInboxCounts } from "@/lib/actions/inbox";
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

  useEffect(() => {
    setLocalSender(filters.sender || "");
  }, [filters.sender]);

  const debouncedSetSender = useDebouncedCallback((value: string) => {
    setFilters({ sender: value || null });
  }, 300);

  const { view, sender, dateFrom, dateTo } = filters;

  useEffect(() => {
    let cancelled = false;

    async function loadData() {
      setLoading(true);
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

  const clearFilters = () => {
    setLocalSender("");
    setFilters({
      sender: null,
      dateFrom: null,
      dateTo: null,
    });
  };

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
          <CardTitle>Synced Emails</CardTitle>
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
                <TableHead>From</TableHead>
                <TableHead>Subject</TableHead>
                <TableHead>Date</TableHead>
                {view === "all" && <TableHead>Status</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {emails.map((email) => (
                <TableRow key={email.msgId}>
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
