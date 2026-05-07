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

interface UploadDialogProps {
  accounts: string[];
  onSuccess: () => void;
}

interface FileUploadState {
  file: File;
  status: "pending" | "uploading" | "success" | "error";
  error?: string;
  added?: number;
  matched?: number;
  skipped?: number;
}

interface UploadResult {
  status: string;
  transactions?: {
    total: number;
    new: number;
    duplicates: number;
  };
  matching?: {
    invoice_matches: number;
    email_matches: number;
  };
}

export function UploadDialog({ accounts, onSuccess }: UploadDialogProps) {
  const [open, setOpen] = useState(false);
  const [account, setAccount] = useState<string>("");
  const [files, setFiles] = useState<FileUploadState[]>([]);
  const [uploading, setUploading] = useState(false);
  const [currentFileIndex, setCurrentFileIndex] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = e.target.files;
    if (!selectedFiles || selectedFiles.length === 0) return;

    const validFiles: FileUploadState[] = [];
    const invalidFiles: string[] = [];

    for (const file of Array.from(selectedFiles)) {
      const ext = file.name.toLowerCase();
      if (ext.endsWith(".pdf") || ext.endsWith(".csv")) {
        validFiles.push({ file, status: "pending" });
      } else {
        invalidFiles.push(file.name);
      }
    }

    if (invalidFiles.length > 0) {
      toast.error(`Skipped ${invalidFiles.length} invalid files (only PDF/CSV allowed)`);
    }

    setFiles(validFiles);
  };

  const uploadSingleFile = async (fileState: FileUploadState): Promise<FileUploadState> => {
    const formData = new FormData();
    formData.append("file", fileState.file);
    formData.append("account", account);

    try {
      const response = await apiFetch("/api/reconciliation/upload", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json();
        return { ...fileState, status: "error", error: err.error || "Upload failed" };
      }

      const reader = response.body?.getReader();
      if (!reader) {
        return { ...fileState, status: "error", error: "No response body" };
      }

      const decoder = new TextDecoder();
      let buffer = "";
      let result: FileUploadState = { ...fileState, status: "uploading" };

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

            if (event.event === "complete") {
              const uploadResult = event.result as UploadResult;
              result = {
                ...fileState,
                status: "success",
                added: uploadResult.transactions?.new || 0,
                matched: uploadResult.matching?.invoice_matches || 0,
                skipped: uploadResult.transactions?.duplicates || 0,
              };
            } else if (event.event === "error") {
              result = { ...fileState, status: "error", error: event.message || "Upload failed" };
            }
          } catch {
            // Ignore parse errors
          }
        }
      }

      return result.status === "uploading" ? { ...fileState, status: "success", added: 0 } : result;
    } catch (err) {
      return { ...fileState, status: "error", error: err instanceof Error ? err.message : "Upload failed" };
    }
  };

  const handleUpload = useCallback(async () => {
    if (files.length === 0 || !account) return;

    setUploading(true);
    setCurrentFileIndex(0);

    const results: FileUploadState[] = [];

    for (let i = 0; i < files.length; i++) {
      setCurrentFileIndex(i);
      setFiles((prev) =>
        prev.map((f, idx) => (idx === i ? { ...f, status: "uploading" } : f))
      );

      const result = await uploadSingleFile(files[i]);
      results.push(result);

      setFiles((prev) =>
        prev.map((f, idx) => (idx === i ? result : f))
      );
    }

    setUploading(false);

    const successful = results.filter((r) => r.status === "success");
    const failed = results.filter((r) => r.status === "error");
    const totalAdded = successful.reduce((sum, r) => sum + (r.added || 0), 0);
    const totalMatched = successful.reduce((sum, r) => sum + (r.matched || 0), 0);
    const totalSkipped = successful.reduce((sum, r) => sum + (r.skipped || 0), 0);

    if (successful.length > 0) {
      if (totalAdded === 0 && totalSkipped > 0) {
        toast.info(`All ${totalSkipped} transactions already imported (duplicates skipped)`);
      } else {
        toast.success(
          `Added ${totalAdded} transactions from ${successful.length} file${successful.length > 1 ? "s" : ""} (${totalMatched} matched)`
        );
      }
    }
    if (failed.length > 0) {
      toast.error(`${failed.length} files failed to upload`);
    }

    if (successful.length > 0) {
      onSuccess();
    }

    if (failed.length === 0) {
      setOpen(false);
    }
  }, [files, account, onSuccess]);

  const handleOpenChange = (isOpen: boolean) => {
    if (uploading && !isOpen) return;
    setOpen(isOpen);
    if (!isOpen) {
      setFiles([]);
      setAccount("");
      setCurrentFileIndex(0);
    }
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button>Upload Statements</Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Upload Bank Statements</DialogTitle>
          <DialogDescription>
            Upload PDF or CSV statements to import transactions
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <label className="text-sm font-medium">Account</label>
            <select
              value={account}
              onChange={(e) => setAccount(e.target.value)}
              disabled={uploading}
              className="flex h-9 w-full rounded-lg border border-input bg-transparent px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            >
              <option value="">Select account</option>
              {accounts.map((acc) => (
                <option key={acc} value={acc}>
                  {acc.charAt(0).toUpperCase() + acc.slice(1)}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">Statement Files</label>
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf,.csv"
                multiple
                onChange={handleFileChange}
                className="hidden"
                disabled={uploading}
              />
              <Button
                variant="outline"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              >
                Choose Files
              </Button>
              <span className="text-sm text-muted-foreground">
                {files.length === 0
                  ? "No files selected"
                  : `${files.length} file${files.length > 1 ? "s" : ""} selected`}
              </span>
            </div>
          </div>

          {files.length > 0 && (
            <div className="space-y-2 max-h-48 overflow-y-auto">
              {files.map((fileState, index) => (
                <div
                  key={index}
                  className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
                >
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    {fileState.status === "pending" && (
                      <span className="h-2 w-2 rounded-full bg-gray-400" />
                    )}
                    {fileState.status === "uploading" && (
                      <span className="h-2 w-2 rounded-full bg-blue-500 animate-pulse" />
                    )}
                    {fileState.status === "success" && (
                      <span className="h-2 w-2 rounded-full bg-green-500" />
                    )}
                    {fileState.status === "error" && (
                      <span className="h-2 w-2 rounded-full bg-red-500" />
                    )}
                    <span className="truncate">{fileState.file.name}</span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {fileState.status === "success" && (
                      <span className="text-xs text-green-600">
                        +{fileState.added}
                      </span>
                    )}
                    {fileState.status === "error" && (
                      <span className="text-xs text-red-600 truncate max-w-32" title={fileState.error}>
                        {fileState.error}
                      </span>
                    )}
                    {fileState.status === "pending" && !uploading && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 w-6 p-0"
                        onClick={() => removeFile(index)}
                      >
                        ×
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={uploading}>
            {uploading ? "Close" : "Cancel"}
          </Button>
          <Button onClick={handleUpload} disabled={files.length === 0 || !account || uploading}>
            {uploading
              ? `Uploading ${currentFileIndex + 1}/${files.length}...`
              : `Upload ${files.length > 0 ? files.length : ""} File${files.length !== 1 ? "s" : ""}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
