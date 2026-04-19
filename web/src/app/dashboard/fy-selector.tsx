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

export function FYSelector() {
  const fys = getAvailableFYs();
  const [fy, setFy] = useQueryState("fy", parseAsString.withDefault(getCurrentFY()));

  return (
    <Select value={fy} onValueChange={(value) => setFy(value)}>
      <SelectTrigger className="w-36">
        <SelectValue placeholder="Fiscal Year" />
      </SelectTrigger>
      <SelectContent>
        {fys.map((f) => (
          <SelectItem key={f} value={f}>
            {f}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
