import { spawn } from "child_process";
import { z } from "zod";

export const runtime = "nodejs";

const bodySchema = z.object({
  // Optional: when present, retry only these specific message IDs (the
  // dashboard's "Retry selected" path). When absent, retry every email
  // currently in the Needs Attention list ("Retry all").
  msgIds: z.array(z.string().min(1).max(500)).max(200).optional(),
});

// Resets processed_at on emails currently in the Needs Attention list so the
// next /api/pipeline/stream {command:"processInvoices"} run will re-classify
// and re-extract them. Useful after a fix lands (Google reauth, new URL
// extractor, etc.) to sweep the backlog.
export async function POST(request: Request) {
  let parsed: z.infer<typeof bodySchema> = {};
  try {
    const text = await request.text();
    if (text.trim()) {
      const json = JSON.parse(text);
      const result = bodySchema.safeParse(json);
      if (!result.success) {
        return Response.json(
          { ok: false, error: "Invalid request body", issues: result.error.flatten() },
          { status: 400 }
        );
      }
      parsed = result.data;
    }
  } catch {
    // Empty body is fine — falls through as "retry all".
  }

  const projectRoot = process.cwd().replace("/web", "");
  const granitePath = `${projectRoot}/.venv/bin/granite`;

  const args = ["ingest", "invoice", "retry-errors"];
  if (parsed.msgIds && parsed.msgIds.length > 0) {
    for (const id of parsed.msgIds) {
      args.push("--msg-id", id);
    }
  }

  return new Promise<Response>((resolve) => {
    const proc = spawn(granitePath, args, {
      cwd: projectRoot,
      shell: false,
      env: { ...process.env },
    });

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("error", (err) => {
      resolve(
        Response.json({ ok: false, error: err.message }, { status: 500 })
      );
    });

    proc.on("close", (code) => {
      if (code === 0) {
        try {
          const result = JSON.parse(stdout.trim().split("\n").pop() || "{}");
          resolve(Response.json({ ok: true, result }));
        } catch {
          resolve(Response.json({ ok: true, result: { status: "success" } }));
        }
      } else {
        let payload: unknown;
        try {
          payload = JSON.parse(stdout.trim().split("\n").pop() || "{}");
        } catch {
          payload = { message: stderr || stdout || "Retry failed" };
        }
        resolve(Response.json({ ok: false, error: payload }, { status: 500 }));
      }
    });

    request.signal.addEventListener("abort", () => {
      proc.kill("SIGTERM");
    });
  });
}
