import { join } from "path";
import Database from "better-sqlite3";
import { getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

interface JobRow {
  job_id: string;
  job_type: string;
  status: string;
  progress_current: number;
  progress_total: number;
  progress_message: string | null;
  result_json: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export async function GET(
  request: Request,
  { params }: { params: Promise<{ jobId: string }> }
) {
  const { jobId } = await params;

  if (!jobId || !jobId.startsWith("job_")) {
    return new Response(JSON.stringify({ error: "Invalid job ID" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const projectRoot = getProjectRoot();
  const db = new Database(join(projectRoot, "granite.db"), { readonly: true });

  try {
    const row = db.prepare("SELECT * FROM jobs WHERE job_id = ?").get(jobId) as JobRow | undefined;

    if (!row) {
      return new Response(JSON.stringify({ error: "Job not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }

    const response: Record<string, unknown> = {
      jobId: row.job_id,
      type: row.job_type,
      status: row.status,
      progress: {
        current: row.progress_current,
        total: row.progress_total,
        message: row.progress_message,
      },
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };

    if (row.status === "complete" && row.result_json) {
      try {
        response.result = JSON.parse(row.result_json);
      } catch {
        response.result = { status: "success" };
      }
    }

    if (row.status === "failed" && row.error_message) {
      response.error = row.error_message;
    }

    return new Response(JSON.stringify(response), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } finally {
    db.close();
  }
}
