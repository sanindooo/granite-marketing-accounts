import { spawn } from "child_process";

export const runtime = "nodejs";

// Resets processed_at on every email currently in the Needs Attention list
// so the next /api/pipeline/stream {command:"processInvoices"} run will
// re-classify and re-extract them. Useful after a fix lands (Google reauth,
// new URL extractor, etc.) to sweep the backlog without picking each email.
export async function POST(request: Request) {
  const projectRoot = process.cwd().replace("/web", "");
  const granitePath = `${projectRoot}/.venv/bin/granite`;

  return new Promise<Response>((resolve) => {
    const proc = spawn(
      granitePath,
      ["ingest", "invoice", "retry-errors"],
      {
        cwd: projectRoot,
        shell: false,
        env: { ...process.env },
      }
    );

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
          const parsed = JSON.parse(stdout.trim().split("\n").pop() || "{}");
          resolve(Response.json({ ok: true, result: parsed }));
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
        resolve(
          Response.json({ ok: false, error: payload }, { status: 500 })
        );
      }
    });

    request.signal.addEventListener("abort", () => {
      proc.kill("SIGTERM");
    });
  });
}
