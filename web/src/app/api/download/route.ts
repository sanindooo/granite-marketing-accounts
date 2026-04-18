import { NextResponse } from "next/server";
import archiver from "archiver";
import pLimit from "p-limit";
import { PassThrough } from "stream";
import { z } from "zod";
import { getInvoicesByIds } from "@/lib/queries/invoices";
import { downloadFileFromDrive } from "@/lib/drive";

const MAX_INVOICES = 100;
const CONCURRENCY = 5;

const downloadSchema = z.object({
  invoiceIds: z.array(z.string().uuid()).min(1).max(MAX_INVOICES),
});

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const result = downloadSchema.safeParse(body);

    if (!result.success) {
      return NextResponse.json(
        { error: "Invalid invoice IDs" },
        { status: 400 }
      );
    }

    const { invoiceIds } = result.data;
    const invoices = getInvoicesByIds(invoiceIds);

    const invoicesWithFiles = invoices.filter((i) => i?.drive_file_id);

    if (invoicesWithFiles.length === 0) {
      return NextResponse.json(
        { error: "No invoices have PDF files available" },
        { status: 400 }
      );
    }

    const archive = archiver("zip", { zlib: { level: 1 } });
    const passThrough = new PassThrough();

    archive.pipe(passThrough);

    const limit = pLimit(CONCURRENCY);

    const downloadPromises = invoicesWithFiles.map((invoice) =>
      limit(async () => {
        if (!invoice?.drive_file_id) return;

        try {
          const stream = await downloadFileFromDrive(invoice.drive_file_id);
          const vendorName = (invoice.vendor_name || invoice.vendor_name_raw)
            .replace(/[^a-zA-Z0-9]/g, "_")
            .slice(0, 30);
          const filename = `${invoice.invoice_date}_${vendorName}_${invoice.invoice_number}.pdf`;

          archive.append(stream, { name: filename });
        } catch (err) {
          console.error(
            `Failed to download ${invoice.invoice_id}:`,
            err
          );
        }
      })
    );

    Promise.all(downloadPromises)
      .then(() => archive.finalize())
      .catch((err) => {
        console.error("Archive error:", err);
        archive.abort();
      });

    const readableStream = new ReadableStream({
      start(controller) {
        passThrough.on("data", (chunk) => {
          controller.enqueue(chunk);
        });
        passThrough.on("end", () => {
          controller.close();
        });
        passThrough.on("error", (err) => {
          controller.error(err);
        });
      },
    });

    return new Response(readableStream, {
      headers: {
        "Content-Type": "application/zip",
        "Content-Disposition": `attachment; filename="invoices.zip"`,
      },
    });
  } catch (err) {
    console.error("Download error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Download failed" },
      { status: 500 }
    );
  }
}
