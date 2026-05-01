import Database from "better-sqlite3";
import { z } from "zod";
import { getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

const cancelSchema = z.object({
  operation: z.enum(["ingest_email", "ingest_invoice", "reconcile"]),
});

export async function POST(request: Request) {
  const body = await request.json();
  const result = cancelSchema.safeParse(body);

  if (!result.success) {
    return new Response(JSON.stringify({ error: "Invalid request" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const { operation } = result.data;

  const dbPath = `${getProjectRoot()}/.state/pipeline.db`;

  try {
    const db = new Database(dbPath);

    // Mark all running records for this operation as cancelled
    const stmt = db.prepare(`
      UPDATE runs
      SET status = 'cancelled',
          ended_at = datetime('now'),
          completed_at = datetime('now')
      WHERE operation = ? AND status = 'running'
    `);

    const info = stmt.run(operation);
    db.close();

    return new Response(
      JSON.stringify({
        success: true,
        cancelled: info.changes,
        message: `Cancelled ${info.changes} running ${operation} job(s)`
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (error) {
    return new Response(
      JSON.stringify({ error: "Failed to cancel run" }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
}
