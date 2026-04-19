"use client";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface StaleRun {
  runId: string;
  operation: string;
  startedAt: string;
  runningFor: string;
  statsJson?: string;
}

interface StaleRunModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  staleRuns: StaleRun[];
  operationLabel: string;
  onCancelAndStart: () => void;
  onKeepWaiting: () => void;
}

function formatDuration(startedAt: string): string {
  const start = new Date(startedAt);
  const now = new Date();
  const diffMs = now.getTime() - start.getTime();

  const hours = Math.floor(diffMs / (1000 * 60 * 60));
  const minutes = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));

  if (hours > 24) {
    const days = Math.floor(hours / 24);
    return `${days} day${days > 1 ? 's' : ''} ago`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m ago`;
  }
  return `${minutes}m ago`;
}

function parseStats(statsJson: string | undefined): Record<string, number> | null {
  if (!statsJson || statsJson === '{}') return null;
  try {
    return JSON.parse(statsJson);
  } catch {
    return null;
  }
}

export function StaleRunModal({
  open,
  onOpenChange,
  staleRuns,
  operationLabel,
  onCancelAndStart,
  onKeepWaiting,
}: StaleRunModalProps) {
  const count = staleRuns.length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span className="inline-flex h-2 w-2 rounded-full bg-amber-500 animate-pulse" />
            Existing Run Detected
          </DialogTitle>
          <DialogDescription>
            {count === 1
              ? `There is already a ${operationLabel} job running.`
              : `There are ${count} ${operationLabel} jobs still marked as running.`
            }
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 py-4">
          {staleRuns.map((run) => {
            const stats = parseStats(run.statsJson);
            return (
              <div
                key={run.runId}
                className="rounded-lg border bg-muted/50 p-3 text-sm"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">Run ID</span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {run.runId.slice(0, 20)}...
                  </span>
                </div>
                <div className="flex items-center justify-between mt-1">
                  <span className="text-muted-foreground">Started</span>
                  <span>{new Date(run.startedAt).toLocaleString()}</span>
                </div>
                <div className="flex items-center justify-between mt-1">
                  <span className="text-muted-foreground">Running for</span>
                  <span className="text-amber-600 font-medium">
                    {formatDuration(run.startedAt)}
                  </span>
                </div>
                {stats && Object.keys(stats).length > 0 && (
                  <div className="mt-2 pt-2 border-t">
                    <span className="text-muted-foreground text-xs">Progress:</span>
                    <div className="flex gap-3 mt-1">
                      {Object.entries(stats).map(([key, value]) => (
                        <span key={key} className="text-xs">
                          {key}: <span className="font-medium">{value}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <p className="text-sm text-muted-foreground">
          These jobs may be stuck or still processing. If they&apos;ve been running for a long time,
          they likely need to be cancelled.
        </p>

        <DialogFooter className="flex-col sm:flex-row gap-2">
          <Button
            variant="outline"
            onClick={onKeepWaiting}
            className="sm:flex-1"
          >
            Keep Waiting
          </Button>
          <Button
            onClick={onCancelAndStart}
            className="sm:flex-1 bg-amber-600 hover:bg-amber-700"
          >
            Cancel {count > 1 ? 'All' : ''} & Start New
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
