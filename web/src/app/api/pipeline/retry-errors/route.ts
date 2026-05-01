import { spawn } from "child_process";
import { z } from "zod";
import { getGraniteBinary, getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

// Body must explicitly request the destructive shape: either supply a
// non-empty msgIds list ("Retry selected") OR set all=true ("Retry all").
// Empty bodies / `{}` / `{msgIds: []}` are rejected with 400 — a missing
// click that hits this route can no longer wipe the diagnostic state on
// every Needs-Attention row by accident.
//
// MS Graph and Gmail msg_ids are base64url-shaped (letters, digits, `_`,
// `-`, optional `=`/`+`/`/` padding). Tightening the regex past z.string()
// blocks ANSI escape sequences and control characters from leaking into
// CLI stderr where they could rewrite the developer's terminal — a
// well-known attack class even when shell metacharacters are blocked.
const MSG_ID_PATTERN = /^[A-Za-z0-9_=+/\-]{20,400}$/;
const bodySchema = z
  .object({
    msgIds: z
      .array(z.string().regex(MSG_ID_PATTERN, "invalid msg_id"))
      .min(1)
      .max(200)
      .optional(),
    all: z.literal(true).optional(),
  })
  .refine((v) => Boolean(v.msgIds?.length) !== Boolean(v.all), {
    message: "Body must specify exactly one of: msgIds (non-empty array) or all=true",
  });

// Resets processed_at on emails currently in the Needs Attention list so the
// next /api/pipeline/stream {command:"processInvoices"} run will re-classify
// and re-extract them. Useful after a fix lands (Google reauth, new URL
// extractor, etc.) to sweep the backlog.
export async function POST(request: Request) {
  let parsed: z.infer<typeof bodySchema>;
  try {
    const text = await request.text();
    if (!text.trim()) {
      return Response.json(
        { ok: false, error: "Empty request body. Send {msgIds: [...]} or {all: true}." },
        { status: 400 }
      );
    }
    const json = JSON.parse(text);
    const result = bodySchema.safeParse(json);
    if (!result.success) {
      return Response.json(
        { ok: false, error: "Invalid request body", issues: result.error.flatten() },
        { status: 400 }
      );
    }
    parsed = result.data;
  } catch {
    return Response.json(
      { ok: false, error: "Body must be valid JSON" },
      { status: 400 }
    );
  }

  const projectRoot = getProjectRoot();
  const granitePath = getGraniteBinary();

  const args = ["ingest", "invoice", "retry-errors"];
  if (parsed.msgIds && parsed.msgIds.length > 0) {
    for (const id of parsed.msgIds) {
      args.push("--msg-id", id);
    }
  } else {
    args.push("--all");
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
