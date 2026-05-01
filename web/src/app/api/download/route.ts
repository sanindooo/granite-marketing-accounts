import { NextResponse } from "next/server";
import { Readable } from "node:stream";
import archiver from "archiver";
import pLimit from "p-limit";
import { z } from "zod";
import { getInvoicesByIds } from "@/lib/queries/invoices";
import { downloadFileFromDrive } from "@/lib/drive";
import { markInvoicesExported } from "@/lib/actions/exports";

const MAX_INVOICES = 100;
const CONCURRENCY = 5;

// invoice_id is sha256(msg_id||idx)[:16] (see execution/invoice/filer.py:_invoice_id),
// not a UUID. Validate the actual hex shape so a typo regresses loudly.
const INVOICE_ID = z
  .string()
  .regex(/^[a-f0-9]{16}$/, "invoice_id must be 16 hex chars");

const downloadSchema = z.object({
  invoiceIds: z.array(INVOICE_ID).min(1).max(MAX_INVOICES),
});

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const result = downloadSchema.safeParse(body);

    if (!result.success) {
      console.error("download: invalid body", result.error.flatten());
      return NextResponse.json(
        { error: "Invalid invoice IDs", issues: result.error.flatten() },
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
    const exportedIds: string[] = [];

    // Wire client disconnect to abort the archive so we don't waste bandwidth
    // and don't credit exports that the client never received.
    request.signal.addEventListener("abort", () => archive.abort());

    const limit = pLimit(CONCURRENCY);
    const tasks = invoicesWithFiles.map((invoice) =>
      limit(async () => {
        if (!invoice?.drive_file_id) return;
        try {
          const stream = await downloadFileFromDrive(invoice.drive_file_id);
          const vendorName = (invoice.vendor_name || invoice.vendor_name_raw)
            .replace(/[^a-zA-Z0-9]/g, "_")
            .slice(0, 30);
          const filename = `${invoice.invoice_date}_${vendorName}_${invoice.invoice_number}.pdf`;

          // Wait until archiver has finished reading THIS entry's stream
          // before recording the export. archive.append() is non-blocking;
          // archive.finalize() resolves when the buffer is written, not when
          // the client received bytes — so we mark per-entry on stream end.
          await new Promise<void>((resolve, reject) => {
            stream.once("end", () => {
              exportedIds.push(invoice.invoice_id);
              resolve();
            });
            stream.once("error", reject);
            archive.append(stream, { name: filename });
          });
        } catch (err) {
          console.error(`Failed to download ${invoice.invoice_id}:`, err);
          // Do NOT push to exportedIds.
        }
      })
    );

    Promise.all(tasks)
      .then(() => archive.finalize())
      .catch((err) => {
        console.error("Archive error:", err);
        archive.abort();
      });

    // Use Readable.toWeb instead of PassThrough → ReadableStream — preserves
    // backpressure end-to-end (archiver issues #613/#571/#321).
    const responseBody = Readable.toWeb(archive) as ReadableStream;

    // Mark exports only AFTER archive emits 'end' (zip fully written) and the
    // request was not aborted. A client that disconnects between 'end' and the
    // OS flushing TCP buffers will still get marked — accept that trade-off.
    archive.on("end", () => {
      if (!request.signal.aborted && exportedIds.length > 0) {
        markInvoicesExported(exportedIds).catch((e) =>
          console.error("markInvoicesExported failed:", e)
        );
      }
    });

    return new Response(responseBody, {
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
