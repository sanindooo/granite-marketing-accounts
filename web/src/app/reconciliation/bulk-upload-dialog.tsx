"use client";

import { useState, useRef, useCallback, useEffect } from "react";
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

interface JobStatus {
  jobId: string;
  status: "pending" | "running" | "complete" | "failed";
  progress: {
    current: number;
    total: number;
    message: string | null;
  };
  result?: {
    matched?: number;
    unmatched?: number;
    errors?: number;
    duplicates?: number;
  };
  error?: string;
}

// Track active jobs across component remounts
const activeJobs = new Map<string, { toastId: string | number; onSuccess: () => void }>();

export function BulkUploadDialog({ onSuccess }: BulkUploadDialogProps) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Poll for job status
  useEffect(() => {
    if (activeJobs.size === 0) return;

    const pollInterval = setInterval(async () => {
      for (const [jobId, { toastId, onSuccess: jobOnSuccess }] of activeJobs) {
        try {
          const response = await apiFetch(`/api/jobs/${jobId}`);
          if (!response.ok) continue;

          const status: JobStatus = await response.json();

          if (status.status === "running") {
            toast.loading(
              `Processing ${status.progress.current}/${status.progress.total}...`,
              { id: toastId }
            );
          } else if (status.status === "complete") {
            activeJobs.delete(jobId);
            const matched = status.result?.matched || 0;
            const filed = (status.result?.unmatched || 0) - (status.result?.errors || 0);

            if (matched > 0) {
              toast.success(
                `Matched ${matched} invoice${matched !== 1 ? "s" : ""} to transactions`,
                { id: toastId }
              );
            } else if (filed > 0) {
              toast.success(
                `Filed ${filed} invoice${filed !== 1 ? "s" : ""} (no matches found)`,
                { id: toastId }
              );
            } else {
              toast.success("Upload complete", { id: toastId });
            }
            jobOnSuccess();
          } else if (status.status === "failed") {
            activeJobs.delete(jobId);
            toast.error(status.error || "Upload failed", { id: toastId });
          }
        } catch {
          // Ignore polling errors, will retry
        }
      }
    }, 2000);

    return () => clearInterval(pollInterval);
  }, []);

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

    setUploading(true);

    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file);
    }

    try {
      const response = await apiFetch("/api/reconciliation/bulk-upload", {
        method: "POST",
        body: formData,
      });

      const data = await response.json();

      if (!response.ok) {
        toast.error(data.error || "Upload failed");
        setUploading(false);
        return;
      }

      // Job accepted - start polling
      const toastId = toast.loading(
        `Processing ${data.fileCount} invoice${data.fileCount !== 1 ? "s" : ""} in background...`
      );

      activeJobs.set(data.jobId, { toastId, onSuccess });

      // Close dialog so user can continue working
      setOpen(false);
      setFiles([]);
      setUploading(false);

      // Reset file input
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Upload failed");
      setUploading(false);
    }
  }, [files, onSuccess]);

  const handleOpenChange = (isOpen: boolean) => {
    if (uploading && !isOpen) return;
    setOpen(isOpen);
    if (!isOpen) {
      setFiles([]);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
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
            Upload invoice PDFs to match against unmatched transactions.
            Processing runs in the background so you can continue working.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
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
                disabled={uploading}
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
                    disabled={uploading}
                  >
                    ×
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={uploading}>
            Cancel
          </Button>
          <Button onClick={handleUpload} disabled={files.length === 0 || uploading}>
            {uploading ? (
              <>
                <span className="mr-2 h-4 w-4 rounded-full border-2 border-white border-t-transparent animate-spin" />
                Starting...
              </>
            ) : (
              `Upload ${files.length > 0 ? files.length : ""} PDF${files.length !== 1 ? "s" : ""}`
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
