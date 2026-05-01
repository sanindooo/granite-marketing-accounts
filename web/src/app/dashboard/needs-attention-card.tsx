"use client";

import { Fragment, useState, useRef } from "react";
import DOMPurify from "dompurify";
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
import type { PendingAction } from "@/lib/queries/dashboard";

interface EmailBody {
  body_html: string;
  body_text: string;
}

interface NeedsAttentionCardProps {
  pendingActions: PendingAction[];
  onDismiss: (msgId: string, reason: "not_invoice" | "resolved", blockDomain?: boolean) => Promise<void>;
  onBulkDismiss: (msgIds: string[], reason: "not_invoice" | "resolved") => Promise<void>;
  onUploadPdf: (msgId: string, file: File) => Promise<void>;
  onRetryAll?: () => Promise<void>;
  onRetrySelected?: (msgIds: string[]) => Promise<void>;
  onReauthGoogle?: () => Promise<void>;
}

// Map machine error_codes to short, user-readable labels. Unknown codes
// fall through as the raw value so a new error_code at least surfaces
// in the UI rather than being hidden as "Processing error".
const ERROR_CODE_LABELS: Record<string, string> = {
  needs_reauth: "Re-authentication required",
  rate_limited: "Rate limited (will retry)",
  schema_violation: "Bad response from upstream",
  data_quality: "Couldn't read invoice data",
  config_error: "Configuration problem",
  budget_exceeded: "Budget cap hit",
  ssrf_rejected: "URL blocked (security)",
  path_violation: "File path blocked (security)",
  unhandled_exception: "Unexpected crash",
};

function describeErrorCode(code: string | null): string {
  if (!code) return "Processing error";
  return ERROR_CODE_LABELS[code] ?? `Processing error (${code})`;
}

export function NeedsAttentionCard({
  pendingActions,
  onDismiss,
  onBulkDismiss,
  onUploadPdf,
  onRetryAll,
  onRetrySelected,
  onReauthGoogle,
}: NeedsAttentionCardProps) {
  const [isCollapsed, setIsCollapsed] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [emailBody, setEmailBody] = useState<EmailBody | null>(null);
  const [loadingBody, setLoadingBody] = useState(false);
  const [uploadingId, setUploadingId] = useState<string | null>(null);
  const fileInputRefs = useRef<Map<string, HTMLInputElement>>(new Map());

  // Bulk selection state
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkDismissing, setBulkDismissing] = useState(false);

  // Inline dismiss confirmation state (shows "Block domain? Yes/No" after clicking Not Invoice)
  const [confirmingDismiss, setConfirmingDismiss] = useState<string | null>(null);

  const [retrying, setRetrying] = useState(false);
  const [retryingSelected, setRetryingSelected] = useState(false);
  const [reauthing, setReauthing] = useState(false);

  const needsReauth = pendingActions.some(
    (a) => a.errorCode === "needs_reauth"
  );

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
    if (selectedIds.size === pendingActions.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(pendingActions.map((a) => a.msgId)));
    }
  };

  const handleBulkDismiss = async (reason: "not_invoice" | "resolved") => {
    if (selectedIds.size === 0) return;
    setBulkDismissing(true);
    try {
      await onBulkDismiss(Array.from(selectedIds), reason);
      setSelectedIds(new Set());
    } finally {
      setBulkDismissing(false);
    }
  };

  const handleNotInvoiceClick = (msgId: string) => {
    setConfirmingDismiss(msgId);
  };

  const handleConfirmNotInvoice = async (msgId: string, blockDomain: boolean) => {
    await onDismiss(msgId, "not_invoice", blockDomain);
    setConfirmingDismiss(null);
  };

  const handleFileSelect = async (msgId: string, file: File | undefined) => {
    if (!file || !file.name.toLowerCase().endsWith(".pdf")) return;
    setUploadingId(msgId);
    try {
      await onUploadPdf(msgId, file);
    } finally {
      setUploadingId(null);
      const input = fileInputRefs.current.get(msgId);
      if (input) input.value = "";
    }
  };

  const handleToggleExpand = async (msgId: string) => {
    if (expandedId === msgId) {
      setExpandedId(null);
      setEmailBody(null);
      return;
    }

    setExpandedId(msgId);
    setLoadingBody(true);
    setEmailBody(null);

    try {
      const response = await fetch("/api/emails/body", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ msgId }),
      });

      if (response.ok) {
        const data = await response.json();
        setEmailBody(data);
      }
    } catch (error) {
      console.error("Failed to fetch email body:", error);
    } finally {
      setLoadingBody(false);
    }
  };

  const hasSelection = selectedIds.size > 0;

  return (
    <Card className="border-amber-200 bg-amber-50/50">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setIsCollapsed(!isCollapsed)}
      >
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-amber-800">
            <span className="inline-flex h-2 w-2 rounded-full bg-amber-500" />
            Needs Attention ({pendingActions.length})
            <span className="ml-1 text-sm font-normal text-amber-600">
              {isCollapsed ? "▸" : "▾"}
            </span>
          </CardTitle>
          {!isCollapsed && hasSelection && (
            <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
              <span className="text-sm text-muted-foreground">
                {selectedIds.size} selected
              </span>
              {onRetrySelected && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  disabled={retryingSelected || bulkDismissing}
                  onClick={async () => {
                    setRetryingSelected(true);
                    try {
                      await onRetrySelected(Array.from(selectedIds));
                      setSelectedIds(new Set());
                    } finally {
                      setRetryingSelected(false);
                    }
                  }}
                >
                  {retryingSelected ? "Retrying…" : "Retry selected"}
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                disabled={bulkDismissing}
                onClick={() => handleBulkDismiss("not_invoice")}
              >
                {bulkDismissing ? "..." : "Not Invoice"}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs text-green-600 border-green-200 hover:bg-green-50"
                disabled={bulkDismissing}
                onClick={() => handleBulkDismiss("resolved")}
              >
                {bulkDismissing ? "..." : "Resolved"}
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
      </CardHeader>
      {!isCollapsed && <CardContent>
        {(needsReauth || onRetryAll) && (
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {needsReauth && onReauthGoogle && (
              <div
                className="flex flex-1 items-center justify-between gap-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
                role="alert"
              >
                <span>
                  Google access has expired — re-authenticate to unblock
                  invoice filing.
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 border-red-300 bg-white text-xs text-red-700 hover:bg-red-100"
                  disabled={reauthing}
                  onClick={async (e) => {
                    e.stopPropagation();
                    setReauthing(true);
                    try {
                      await onReauthGoogle();
                    } finally {
                      setReauthing(false);
                    }
                  }}
                >
                  {reauthing ? "Opening browser…" : "Re-authenticate Google"}
                </Button>
              </div>
            )}
            {onRetryAll && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                disabled={retrying}
                onClick={async (e) => {
                  e.stopPropagation();
                  setRetrying(true);
                  try {
                    await onRetryAll();
                  } finally {
                    setRetrying(false);
                  }
                }}
              >
                {retrying ? "Retrying…" : "Retry all"}
              </Button>
            )}
          </div>
        )}
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8">
                <Checkbox
                  checked={selectedIds.size === pendingActions.length && pendingActions.length > 0}
                  onCheckedChange={toggleSelectAll}
                  aria-label="Select all"
                />
              </TableHead>
              <TableHead className="w-8"></TableHead>
              <TableHead>From</TableHead>
              <TableHead>Subject</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Issue</TableHead>
              <TableHead>Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pendingActions.map((action) => (
              <Fragment key={action.msgId}>
                <TableRow className="cursor-pointer hover:bg-amber-100/50">
                  <TableCell className="w-8">
                    <Checkbox
                      checked={selectedIds.has(action.msgId)}
                      onCheckedChange={() => toggleSelection(action.msgId)}
                      aria-label={`Select ${action.subject}`}
                    />
                  </TableCell>
                  <TableCell className="w-8">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 w-6 p-0"
                      onClick={() => handleToggleExpand(action.msgId)}
                    >
                      {expandedId === action.msgId ? "−" : "+"}
                    </Button>
                  </TableCell>
                  <TableCell className="max-w-32 truncate text-sm">
                    {action.fromAddr}
                  </TableCell>
                  <TableCell
                    className="max-w-64 truncate text-sm"
                    onClick={() => handleToggleExpand(action.msgId)}
                  >
                    {action.subject}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {new Date(action.receivedAt).toLocaleDateString()}
                  </TableCell>
                  <TableCell>
                    <span
                      title={action.errorCode || undefined}
                      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                        action.outcome === "needs_manual_download"
                          ? "bg-amber-100 text-amber-800"
                          : action.outcome === "no_attachment"
                          ? "bg-blue-100 text-blue-800"
                          : "bg-red-100 text-red-800"
                      }`}
                    >
                      {action.outcome === "needs_manual_download"
                        ? "Manual download needed"
                        : action.outcome === "no_attachment"
                        ? "No PDF attached"
                        : describeErrorCode(action.errorCode)}
                    </span>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      {action.manualDownloadUrl && (
                        <a
                          href={action.manualDownloadUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="rounded border border-blue-200 bg-blue-50 px-2 py-1 text-xs text-blue-700 hover:bg-blue-100"
                          title={`Open invoice URL: ${action.manualDownloadUrl}`}
                        >
                          Open URL
                        </a>
                      )}
                      {/* Upload PDF is offered for any failure mode where
                          the user might have manually obtained the file —
                          including `error` (e.g. unhandled HTTP failure
                          mid-fetch) so we don't leave the user stranded. */}
                      <Input
                        type="file"
                        accept=".pdf"
                        className="hidden"
                        ref={(el) => {
                          if (el) fileInputRefs.current.set(action.msgId, el);
                        }}
                        onChange={(e) => handleFileSelect(action.msgId, e.target.files?.[0])}
                      />
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        disabled={uploadingId === action.msgId}
                        onClick={() => fileInputRefs.current.get(action.msgId)?.click()}
                      >
                        {uploadingId === action.msgId ? "Uploading..." : "Upload PDF"}
                      </Button>
                      {confirmingDismiss === action.msgId ? (
                        <div className="flex items-center gap-0.5 rounded border bg-white px-1.5 py-0.5 shadow-sm">
                          <span className="text-xs text-muted-foreground">Block?</span>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-5 px-1.5 text-xs"
                            onClick={() => handleConfirmNotInvoice(action.msgId, true)}
                          >
                            Yes
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-5 px-1.5 text-xs"
                            onClick={() => handleConfirmNotInvoice(action.msgId, false)}
                          >
                            No
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-5 w-5 p-0 text-xs text-muted-foreground"
                            onClick={() => setConfirmingDismiss(null)}
                          >
                            ✕
                          </Button>
                        </div>
                      ) : (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs text-muted-foreground hover:text-foreground"
                          onClick={() => handleNotInvoiceClick(action.msgId)}
                        >
                          Not Invoice
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs text-green-600 hover:text-green-700 hover:bg-green-50"
                        onClick={() => onDismiss(action.msgId, "resolved")}
                      >
                        Resolved
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
                {expandedId === action.msgId && (
                  <TableRow>
                    <TableCell colSpan={7} className="bg-white p-4">
                      {action.errorMessage && (
                        <div className="mb-3 rounded border border-red-200 bg-red-50 p-3 text-sm">
                          <div className="mb-1 font-medium text-red-800">
                            Error detail
                            {action.errorCode && (
                              <span className="ml-2 font-mono text-xs text-red-600">
                                ({action.errorCode})
                              </span>
                            )}
                          </div>
                          <pre className="whitespace-pre-wrap break-words font-mono text-xs text-red-900">
                            {action.errorMessage}
                          </pre>
                        </div>
                      )}
                      {loadingBody ? (
                        <div className="text-sm text-muted-foreground">Loading email content...</div>
                      ) : emailBody ? (
                        <div className="max-h-96 overflow-auto rounded border bg-gray-50 p-4">
                          {emailBody.body_html ? (
                            <div
                              className="prose prose-sm max-w-none"
                              dangerouslySetInnerHTML={{
                                __html: DOMPurify.sanitize(emailBody.body_html, {
                                  FORBID_TAGS: ["script", "style", "iframe", "object", "embed"],
                                  FORBID_ATTR: ["onerror", "onload", "onclick", "onmouseover"],
                                }),
                              }}
                            />
                          ) : (
                            <pre className="whitespace-pre-wrap text-sm">{emailBody.body_text}</pre>
                          )}
                        </div>
                      ) : (
                        <div className="text-sm text-red-500">Failed to load email content</div>
                      )}
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
        <p className="mt-3 text-xs text-muted-foreground">
          Use checkboxes to select multiple emails for bulk actions. Click + to view email content.
        </p>
      </CardContent>}
    </Card>
  );
}
