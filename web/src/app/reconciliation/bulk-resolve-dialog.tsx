"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface BulkResolveDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  count: number;
  onResolve: (reason: string, note?: string) => Promise<void>;
}

const PRESET_REASONS = [
  { value: "personal", label: "Personal expense" },
  { value: "transfer_to_self", label: "Transfer to myself" },
  { value: "travel", label: "Travel expense" },
  { value: "food", label: "Food expense" },
  { value: "subscription", label: "Subscription (no invoice)" },
  { value: "bank_fee", label: "Bank fee" },
] as const;

export function BulkResolveDialog({
  open,
  onOpenChange,
  count,
  onResolve,
}: BulkResolveDialogProps) {
  const [selectedReason, setSelectedReason] = useState<string>("");
  const [customNote, setCustomNote] = useState("");
  const [isOther, setIsOther] = useState(false);
  const [resolving, setResolving] = useState(false);

  const handleSelectReason = (reason: string) => {
    if (reason === "other") {
      setIsOther(true);
      setSelectedReason("");
    } else {
      setIsOther(false);
      setSelectedReason(reason);
      setCustomNote("");
    }
  };

  const handleResolve = async () => {
    const reason = isOther ? "other" : selectedReason;
    const note = isOther ? customNote : undefined;

    if (!reason || (isOther && !customNote.trim())) return;

    setResolving(true);
    try {
      await onResolve(reason, note);
      onOpenChange(false);
      setSelectedReason("");
      setCustomNote("");
      setIsOther(false);
    } finally {
      setResolving(false);
    }
  };

  const canSubmit = isOther ? customNote.trim().length > 0 : selectedReason.length > 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Resolve {count} Transaction{count !== 1 ? "s" : ""}</DialogTitle>
          <DialogDescription>
            Mark these transactions as not needing an invoice
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <Label>Select a reason</Label>
            <div className="grid grid-cols-2 gap-2">
              {PRESET_REASONS.map((preset) => (
                <Button
                  key={preset.value}
                  variant={selectedReason === preset.value && !isOther ? "default" : "outline"}
                  size="sm"
                  className="justify-start h-auto py-2 px-3"
                  onClick={() => handleSelectReason(preset.value)}
                >
                  {preset.label}
                </Button>
              ))}
              <Button
                variant={isOther ? "default" : "outline"}
                size="sm"
                className="justify-start h-auto py-2 px-3 col-span-2"
                onClick={() => handleSelectReason("other")}
              >
                Other (custom note)
              </Button>
            </div>
          </div>

          {isOther && (
            <div className="space-y-2">
              <Label htmlFor="custom-note">Custom note</Label>
              <Input
                id="custom-note"
                placeholder="Enter a reason..."
                value={customNote}
                onChange={(e) => setCustomNote(e.target.value)}
                autoFocus
              />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={resolving}>
            Cancel
          </Button>
          <Button onClick={handleResolve} disabled={!canSubmit || resolving}>
            {resolving ? "Resolving..." : `Resolve ${count} Transaction${count !== 1 ? "s" : ""}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
