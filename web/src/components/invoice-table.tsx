"use client";

import { useState } from "react";
import Link from "next/link";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import type { InvoiceRow } from "@/lib/types";

interface InvoiceTableProps {
  data: InvoiceRow[];
  selectable?: boolean;
  selectedIds?: Set<string>;
  onSelectionChange?: (ids: Set<string>) => void;
}

function formatCurrency(amount: string | null, currency: string): string {
  if (!amount) return "-";
  const num = parseFloat(amount);
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: currency || "GBP",
  }).format(num);
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export function InvoiceTable({
  data,
  selectable = false,
  selectedIds = new Set(),
  onSelectionChange,
}: InvoiceTableProps) {
  const [sorting, setSorting] = useState<SortingState>([
    { id: "invoice_date", desc: true },
  ]);

  const columns: ColumnDef<InvoiceRow>[] = [
    ...(selectable
      ? [
          {
            id: "select",
            header: ({ table }: { table: ReturnType<typeof useReactTable<InvoiceRow>> }) => (
              <Checkbox
                checked={table.getIsAllPageRowsSelected()}
                onCheckedChange={(value) => {
                  table.toggleAllPageRowsSelected(!!value);
                  if (onSelectionChange) {
                    const newIds = new Set(
                      value ? data.map((r) => r.invoice_id) : []
                    );
                    onSelectionChange(newIds);
                  }
                }}
              />
            ),
            cell: ({ row }: { row: { original: InvoiceRow } }) => (
              <Checkbox
                checked={selectedIds.has(row.original.invoice_id)}
                onCheckedChange={(value) => {
                  if (onSelectionChange) {
                    const newIds = new Set(selectedIds);
                    if (value) {
                      newIds.add(row.original.invoice_id);
                    } else {
                      newIds.delete(row.original.invoice_id);
                    }
                    onSelectionChange(newIds);
                  }
                }}
              />
            ),
            enableSorting: false,
          } as ColumnDef<InvoiceRow>,
        ]
      : []),
    {
      accessorKey: "invoice_date",
      header: "Date",
      cell: ({ row }) => formatDate(row.original.invoice_date),
    },
    {
      accessorKey: "vendor_name",
      header: "Vendor",
      cell: ({ row }) =>
        row.original.vendor_name || row.original.vendor_name_raw,
    },
    {
      accessorKey: "invoice_number",
      header: "Invoice #",
    },
    {
      accessorKey: "amount_gross",
      header: "Amount",
      cell: ({ row }) =>
        formatCurrency(row.original.amount_gross, row.original.currency),
    },
    {
      accessorKey: "amount_gross_gbp",
      header: "GBP",
      cell: ({ row }) =>
        row.original.currency === "GBP"
          ? "-"
          : formatCurrency(row.original.amount_gross_gbp, "GBP"),
    },
    {
      accessorKey: "category",
      header: "Category",
      cell: ({ row }) => (
        <Badge variant="secondary" className="capitalize">
          {row.original.category}
        </Badge>
      ),
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <Link
          href={`/invoices/${row.original.invoice_id}`}
          className="text-sm text-blue-600 hover:underline"
        >
          View
        </Link>
      ),
    },
  ];

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <TableHead
                  key={header.id}
                  className={
                    header.column.getCanSort() ? "cursor-pointer select-none" : ""
                  }
                  onClick={header.column.getToggleSortingHandler()}
                >
                  {header.isPlaceholder
                    ? null
                    : flexRender(
                        header.column.columnDef.header,
                        header.getContext()
                      )}
                  {{
                    asc: " ↑",
                    desc: " ↓",
                  }[header.column.getIsSorted() as string] ?? null}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.length === 0 ? (
            <TableRow>
              <TableCell
                colSpan={columns.length}
                className="h-24 text-center text-muted-foreground"
              >
                No invoices found.
              </TableCell>
            </TableRow>
          ) : (
            table.getRowModel().rows.map((row) => (
              <TableRow key={row.id}>
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}
