import { NextResponse } from "next/server";
import archiver from "archiver";
import pLimit from "p-limit";
import { PassThrough } from "stream";
import { getInvoiceById } from "@/lib/queries/invoices";
import { downloadFileFromDrive } from "@/lib/drive";

const MAX_INVOICES = 100;
const CONCURRENCY = 5;

export async function POST(request: Request) {
  try {
    const { invoiceIds } = await request.json();

    if (!Array.isArray(invoiceIds) || invoiceIds.length === 0) {
      return NextResponse.json(
        { error: "No invoice IDs provided" },
        { status: 400 }
      );
    }

    if (invoiceIds.length > MAX_INVOICES) {
      return NextResponse.json(
        { error: `Maximum ${MAX_INVOICES} invoices allowed` },
        { status: 400 }
      );
    }

    const invoices = invoiceIds
      .map((id: string) => getInvoiceById(id))
      .filter(Boolean);

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
