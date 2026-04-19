"use client";

import { useState, useEffect } from "react";
import { useQueryStates, parseAsString, parseAsBoolean } from "nuqs";
import { useDebouncedCallback } from "use-debounce";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { getAvailableFYs, getCurrentFY } from "@/lib/fiscal";
import type { VendorRow } from "@/lib/types";

interface InvoiceFiltersProps {
  vendors: VendorRow[];
  categories: string[];
}

export function InvoiceFilters({ vendors, categories }: InvoiceFiltersProps) {
  const [filters, setFilters] = useQueryStates(
    {
      fy: parseAsString.withDefault(getCurrentFY()),
      vendor: parseAsString,
      category: parseAsString,
      status: parseAsString.withDefault("all"),
      search: parseAsString,
      dateFrom: parseAsString,
      dateTo: parseAsString,
      exceptions: parseAsBoolean.withDefault(false),
    },
    { shallow: true }
  );

  const [localSearch, setLocalSearch] = useState(filters.search || "");

  useEffect(() => {
    setLocalSearch(filters.search || "");
  }, [filters.search]);

  const debouncedSetSearch = useDebouncedCallback((value: string) => {
    setFilters({ search: value || null });
  }, 300);

  const fys = getAvailableFYs(true); // include "all" option

  const clearFilters = () => {
    setLocalSearch("");
    setFilters({
      fy: getCurrentFY(),
      vendor: null,
      category: null,
      status: "all",
      search: null,
      dateFrom: null,
      dateTo: null,
      exceptions: false,
    });
  };

  const hasActiveFilters =
    filters.vendor ||
    filters.category ||
    filters.status !== "all" ||
    filters.search ||
    filters.dateFrom ||
    filters.dateTo ||
    filters.exceptions;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Select
          value={filters.fy}
          onValueChange={(value) => setFilters({ fy: value })}
        >
          <SelectTrigger className="w-36">
            <SelectValue placeholder="Fiscal Year">
              {filters.fy === "all" ? "All Years" : filters.fy}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {fys.map((fy) => (
              <SelectItem key={fy} value={fy}>
                {fy === "all" ? "All Years" : fy}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={filters.vendor || "all"}
          onValueChange={(value) =>
            setFilters({ vendor: value === "all" ? null : value })
          }
        >
          <SelectTrigger className="w-44">
            <SelectValue placeholder="All vendors" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All vendors</SelectItem>
            {vendors.map((v) => (
              <SelectItem key={v.vendor_id} value={v.vendor_id}>
                {v.canonical_name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={filters.category || "all"}
          onValueChange={(value) =>
            setFilters({ category: value === "all" ? null : value })
          }
        >
          <SelectTrigger className="w-36">
            <SelectValue placeholder="All categories" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All categories</SelectItem>
            {categories.map((cat) => (
              <SelectItem key={cat} value={cat}>
                <span className="capitalize">{cat}</span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={filters.status}
          onValueChange={(value) => setFilters({ status: value })}
        >
          <SelectTrigger className="w-36">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="matched">Matched</SelectItem>
            <SelectItem value="unmatched">Unmatched</SelectItem>
            <SelectItem value="pending">Pending</SelectItem>
          </SelectContent>
        </Select>

        <Input
          type="date"
          value={filters.dateFrom || ""}
          onChange={(e) =>
            setFilters({ dateFrom: e.target.value || null })
          }
          className="w-36"
          placeholder="From"
        />

        <Input
          type="date"
          value={filters.dateTo || ""}
          onChange={(e) => setFilters({ dateTo: e.target.value || null })}
          className="w-36"
          placeholder="To"
        />
      </div>

      <div className="flex items-center gap-3">
        <Input
          type="search"
          value={localSearch}
          onChange={(e) => {
            setLocalSearch(e.target.value);
            debouncedSetSearch(e.target.value);
          }}
          placeholder="Search vendor or invoice #..."
          className="max-w-xs"
        />

        <Button
          variant={filters.exceptions ? "default" : "outline"}
          size="sm"
          onClick={() => setFilters({ exceptions: !filters.exceptions })}
        >
          Exceptions only
        </Button>

        {hasActiveFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            Clear filters
          </Button>
        )}
      </div>
    </div>
  );
}
