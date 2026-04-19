"use client";

import { useQueryState, parseAsString } from "nuqs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { getAvailableFYs, getCurrentFY } from "@/lib/fiscal";

function formatFyLabel(fy: string): string {
  if (fy === "all") return "All Years";
  return fy;
}

export function FYSelector() {
  const fys = getAvailableFYs(true); // include "all" option
  const [fy, setFy] = useQueryState("fy", parseAsString.withDefault(getCurrentFY()));

  return (
    <Select value={fy} onValueChange={(value) => setFy(value)}>
      <SelectTrigger className="w-36">
        <SelectValue placeholder="Fiscal Year">
          {formatFyLabel(fy)}
        </SelectValue>
      </SelectTrigger>
      <SelectContent>
        {fys.map((f) => (
          <SelectItem key={f} value={f}>
            {formatFyLabel(f)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
