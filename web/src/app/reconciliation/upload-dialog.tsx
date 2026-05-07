"use client";

import { useState, useRef, useCallback } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { apiFetch } from "@/lib/api-fetch";

interface UploadDialogProps {
  accounts: string[];
  onSuccess: () => void;
}

interface ProgressEvent {
  event: "progress";
  stage: string;
  current: number;
  total: number;
  detail: string;
}

interface UploadResult {
  status: string;
  transactions_added?: number;
  duplicates_skipped?: number;
  matched?: number;
}

export function UploadDialog({ accounts, onSuccess }: UploadDialogProps) {
  const [open, setOpen] = useState(false);
  const [account, setAccount] = useState<string>("");
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState<ProgressEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      const ext = selectedFile.name.toLowerCase();
      if (!ext.endsWith(".pdf") && !ext.endsWith(".csv")) {
        setError("Please upload a PDF or CSV bank statement.");
        setFile(null);
        return;
      }
      setFile(selectedFile);
      setError(null);
    }
  };

  const handleUpload = useCallback(async () => {
    if (!file || !account) return;

    setUploading(true);
    setProgress(null);
    setError(null);

    const formData = new FormData();
    formData.append("file", file);
    formData.append("account", account);

    try {
      const response = await apiFetch("/api/reconciliation/upload", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.error || "Upload failed");
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("No response body");
      }

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const data = line.slice(6);
          try {
            const event = JSON.parse(data);

            if (event.event === "progress") {
              setProgress(event as ProgressEvent);
            } else if (event.event === "complete") {
              const result = event.result as UploadResult;
              const added = result.transactions_added || 0;
              const skipped = result.duplicates_skipped || 0;
              const matched = result.matched || 0;
              toast.success(
                `Added ${added} transactions (${matched} matched, ${skipped} duplicates skipped)`
              );
              setOpen(false);
              onSuccess();
            } else if (event.event === "error") {
              setError(event.message || "Upload failed");
            }
          } catch {
            // Ignore parse errors
          }
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      setProgress(null);
    }
  }, [file, account, onSuccess]);

  const handleClose = () => {
    if (uploading) return;
    setOpen(false);
    setFile(null);
    setAccount("");
    setError(null);
    setProgress(null);
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogTrigger asChild>
        <Button onClick={() => setOpen(true)}>Upload Statement</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Upload Bank Statement</DialogTitle>
          <DialogDescription>
            Upload a PDF or CSV statement to import transactions
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <label className="text-sm font-medium">Account</label>
            <Select value={account} onValueChange={(value) => setAccount(value ?? "")} disabled={uploading}>
              <SelectTrigger>
                <SelectValue placeholder="Select account" />
              </SelectTrigger>
              <SelectContent>
                {accounts.map((acc) => (
                  <SelectItem key={acc} value={acc}>
                    {acc.charAt(0).toUpperCase() + acc.slice(1)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">Statement File</label>
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf,.csv"
                onChange={handleFileChange}
                className="hidden"
                disabled={uploading}
              />
              <Button
                variant="outline"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              >
                Choose File
              </Button>
              <span className="text-sm text-muted-foreground truncate flex-1">
                {file?.name || "No file selected"}
              </span>
            </div>
          </div>

          {uploading && progress && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-sm">
                <div className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
                <span className="text-blue-600">{progress.detail}</span>
              </div>
              {progress.total > 0 && (
                <div className="flex items-center gap-2">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full bg-blue-500 transition-all duration-300"
                      style={{
                        width: `${Math.min(100, (progress.current / progress.total) * 100)}%`,
                      }}
                    />
                  </div>
                  <span className="text-xs text-muted-foreground tabular-nums">
                    {progress.current}/{progress.total}
                  </span>
                </div>
              )}
            </div>
          )}

          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-950/50 p-3 text-sm text-red-600 dark:text-red-400">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={handleClose} disabled={uploading}>
            Cancel
          </Button>
          <Button onClick={handleUpload} disabled={!file || !account || uploading}>
            {uploading ? "Uploading..." : "Upload"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
