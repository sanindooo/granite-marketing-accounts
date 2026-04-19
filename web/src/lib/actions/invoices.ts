"use server";

import {
  getInvoices,
  getInvoiceById,
  getVendors,
  getCategories,
  getExceptionInvoices,
  type InvoiceFilters,
} from "@/lib/queries/invoices";
import type { Result, InvoiceRow, VendorRow } from "@/lib/types";

export async function fetchInvoices(
  filters: InvoiceFilters
): Promise<Result<InvoiceRow[]>> {
  try {
    if (filters.status === "all" && !filters.vendor && !filters.category && !filters.search && !filters.dateFrom && !filters.dateTo) {
      const invoices = getInvoices({ fy: filters.fy, limit: filters.limit });
      return { ok: true, data: invoices };
    }
    const invoices = getInvoices(filters);
    return { ok: true, data: invoices };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchExceptionInvoices(
  fy?: string
): Promise<Result<InvoiceRow[]>> {
  try {
    const invoices = getExceptionInvoices(fy);
    return { ok: true, data: invoices };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchInvoice(
  invoiceId: string
): Promise<Result<InvoiceRow>> {
  try {
    const invoice = getInvoiceById(invoiceId);
    if (!invoice) {
      return {
        ok: false,
        error: { code: "NOT_FOUND", message: "Invoice not found" },
      };
    }
    return { ok: true, data: invoice };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchVendors(): Promise<Result<VendorRow[]>> {
  try {
    const vendors = getVendors();
    return { ok: true, data: vendors };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchCategories(): Promise<Result<string[]>> {
  try {
    const categories = getCategories();
    return { ok: true, data: categories };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}
