import { z } from "zod";
import { getCurrentFY } from "../fiscal";

export const invoiceFiltersSchema = z.object({
  fy: z
    .string()
    .regex(/^FY-\d{4}-\d{2}$/)
    .default(getCurrentFY),
  vendor: z.string().optional(),
  category: z.string().optional(),
  status: z.enum(["matched", "unmatched", "pending", "all"]).default("all"),
  search: z.string().optional(),
  dateFrom: z.string().optional(),
  dateTo: z.string().optional(),
  exceptions: z.coerce.boolean().default(false),
});

export type InvoiceFilters = z.infer<typeof invoiceFiltersSchema>;
