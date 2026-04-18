"use client";

import { Fragment, useState, useRef } from "react";
import DOMPurify from "dompurify";
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
import type { PendingAction } from "@/lib/queries/dashboard";

interface EmailBody {
  body_html: string;
  body_text: string;
}

interface NeedsAttentionCardProps {
  pendingActions: PendingAction[];
  onDismiss: (msgId: string, reason: "not_invoice" | "resolved") => Promise<void>;
  onUploadPdf: (msgId: string, file: File) => Promise<void>;
}

export function NeedsAttentionCard({ pendingActions, onDismiss, onUploadPdf }: NeedsAttentionCardProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [emailBody, setEmailBody] = useState<EmailBody | null>(null);
  const [loadingBody, setLoadingBody] = useState(false);
  const [uploadingId, setUploadingId] = useState<string | null>(null);
  const fileInputRefs = useRef<Map<string, HTMLInputElement>>(new Map());

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

  return (
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
                    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                      action.outcome === "needs_manual_download"
                        ? "bg-amber-100 text-amber-800"
                        : action.outcome === "no_attachment"
                        ? "bg-blue-100 text-blue-800"
                        : "bg-red-100 text-red-800"
                    }`}>
                      {action.outcome === "needs_manual_download"
                        ? "Manual download needed"
                        : action.outcome === "no_attachment"
                        ? "No PDF attached"
                        : "Processing error"}
                    </span>
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1">
                      {(action.outcome === "needs_manual_download" || action.outcome === "no_attachment") && (
                        <>
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
                        </>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs text-muted-foreground hover:text-foreground"
                        onClick={() => onDismiss(action.msgId, "not_invoice")}
                      >
                        Not Invoice
                      </Button>
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
                    <TableCell colSpan={6} className="bg-white p-4">
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
          Click + to view email content. Mark as &quot;Not Invoice&quot; to train the system, or &quot;Resolved&quot; if handled manually.
        </p>
      </CardContent>
    </Card>
  );
}
