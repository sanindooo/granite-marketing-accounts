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
import { toast } from "sonner";
import { apiFetch } from "@/lib/api-fetch";

interface BulkUploadDialogProps {
  onSuccess: () => void;
}

interface FileResult {
  file: string;
  success: boolean;
  invoice_id?: string;
  matched_txn_id?: string;
  error?: string;
}

interface UploadResult {
  total_files: number;
  processed: number;
  filed: number;
  matched: number;
  unmatched: number;
  duplicates: number;
  errors: number;
  results: FileResult[];
}

type UploadState = "idle" | "uploading" | "complete";

export function BulkUploadDialog({ onSuccess }: BulkUploadDialogProps) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [uploadState, setUploadState] = useState<UploadState>("idle");
  const [progress, setProgress] = useState({ current: 0, total: 0, message: "" });
  const [result, setResult] = useState<UploadResult | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = e.target.files;
    if (!selectedFiles || selectedFiles.length === 0) return;

    const validFiles: File[] = [];
    const invalidFiles: string[] = [];

    for (const file of Array.from(selectedFiles)) {
      if (file.name.toLowerCase().endsWith(".pdf")) {
        validFiles.push(file);
      } else {
        invalidFiles.push(file.name);
      }
    }

    if (invalidFiles.length > 0) {
      toast.error(`Skipped ${invalidFiles.length} non-PDF files`);
    }

    setFiles(validFiles);
  };

  const handleUpload = useCallback(async () => {
    if (files.length === 0) return;

    setUploadState("uploading");
    setProgress({ current: 0, total: files.length, message: "Starting upload..." });

    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file);
    }

    try {
      const response = await apiFetch("/api/reconciliation/bulk-upload", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json();
        toast.error(err.error || "Upload failed");
        setUploadState("idle");
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        toast.error("No response body");
        setUploadState("idle");
        return;
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
              setProgress({
                current: event.current || 0,
                total: event.total || files.length,
                message: event.message || "Processing...",
              });
            } else if (event.event === "complete") {
              const uploadResult = event.result as UploadResult;
              setResult(uploadResult);
              setUploadState("complete");

              if (uploadResult.matched > 0) {
                toast.success(
                  `Matched ${uploadResult.matched} invoice${uploadResult.matched !== 1 ? "s" : ""} to transactions`
                );
                onSuccess();
              } else if (uploadResult.filed > 0) {
                toast.info(
                  `Filed ${uploadResult.filed} invoice${uploadResult.filed !== 1 ? "s" : ""} (no matches found)`
                );
                onSuccess();
              }
            } else if (event.event === "error") {
              toast.error(event.message || "Upload failed");
              setUploadState("idle");
            }
          } catch {
            // Ignore parse errors
          }
        }
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Upload failed");
      setUploadState("idle");
    }
  }, [files, onSuccess]);

  const handleOpenChange = (isOpen: boolean) => {
    if (uploadState === "uploading" && !isOpen) return;
    setOpen(isOpen);
    if (!isOpen) {
      setFiles([]);
      setUploadState("idle");
      setProgress({ current: 0, total: 0, message: "" });
      setResult(null);
    }
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline">Upload Invoices</Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Upload Invoice PDFs</DialogTitle>
          <DialogDescription>
            Upload invoice PDFs to match against unmatched transactions
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {uploadState === "idle" && (
            <>
              <div className="space-y-2">
                <label className="text-sm font-medium">Invoice Files</label>
                <div className="flex items-center gap-2">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".pdf"
                    multiple
                    onChange={handleFileChange}
                    className="hidden"
                  />
                  <Button
                    variant="outline"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    Choose PDFs
                  </Button>
                  <span className="text-sm text-muted-foreground">
                    {files.length === 0
                      ? "No files selected"
                      : `${files.length} PDF${files.length !== 1 ? "s" : ""} selected`}
                  </span>
                </div>
              </div>

              {files.length > 0 && (
                <div className="space-y-2 max-h-48 overflow-y-auto">
                  {files.map((file, index) => (
                    <div
                      key={index}
                      className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
                    >
                      <span className="truncate">{file.name}</span>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 w-6 p-0"
                        onClick={() => removeFile(index)}
                      >
                        ×
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {uploadState === "uploading" && (
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <span className="h-4 w-4 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
                <span className="text-sm">
                  Processing {progress.current}/{progress.total}...
                </span>
              </div>
              <p className="text-sm text-muted-foreground">{progress.message}</p>
            </div>
          )}

          {uploadState === "complete" && result && (
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-4 text-center">
                <div className="rounded-lg bg-green-50 p-3">
                  <div className="text-2xl font-bold text-green-600">{result.matched}</div>
                  <div className="text-xs text-green-700">Matched</div>
                </div>
                <div className="rounded-lg bg-amber-50 p-3">
                  <div className="text-2xl font-bold text-amber-600">{result.unmatched}</div>
                  <div className="text-xs text-amber-700">Unmatched</div>
                </div>
                <div className="rounded-lg bg-red-50 p-3">
                  <div className="text-2xl font-bold text-red-600">{result.errors}</div>
                  <div className="text-xs text-red-700">Errors</div>
                </div>
              </div>

              {result.duplicates > 0 && (
                <p className="text-sm text-muted-foreground">
                  {result.duplicates} duplicate{result.duplicates !== 1 ? "s" : ""} skipped
                </p>
              )}

              {result.results.length > 0 && (
                <div className="space-y-2 max-h-48 overflow-y-auto">
                  {result.results.map((r, index) => (
                    <div
                      key={index}
                      className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
                    >
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        {r.success && r.matched_txn_id && (
                          <span className="h-2 w-2 rounded-full bg-green-500" />
                        )}
                        {r.success && !r.matched_txn_id && (
                          <span className="h-2 w-2 rounded-full bg-amber-500" />
                        )}
                        {!r.success && (
                          <span className="h-2 w-2 rounded-full bg-red-500" />
                        )}
                        <span className="truncate">
                          {r.file.split("/").pop()}
                        </span>
                      </div>
                      <div className="shrink-0 text-xs">
                        {r.success && r.matched_txn_id && (
                          <span className="text-green-600">Matched</span>
                        )}
                        {r.success && !r.matched_txn_id && (
                          <span className="text-amber-600">Filed</span>
                        )}
                        {!r.success && (
                          <span className="text-red-600 truncate max-w-32" title={r.error}>
                            {r.error}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <DialogFooter>
          {uploadState === "idle" && (
            <>
              <Button variant="outline" onClick={() => handleOpenChange(false)}>
                Cancel
              </Button>
              <Button onClick={handleUpload} disabled={files.length === 0}>
                Upload {files.length > 0 ? files.length : ""} PDF{files.length !== 1 ? "s" : ""}
              </Button>
            </>
          )}
          {uploadState === "uploading" && (
            <Button variant="outline" disabled>
              Uploading...
            </Button>
          )}
          {uploadState === "complete" && (
            <Button onClick={() => handleOpenChange(false)}>Done</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
