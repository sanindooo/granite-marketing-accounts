import { spawn } from "child_process";

export const runtime = "nodejs";

// Triggers `granite ops reauth google`, which deletes the cached OAuth token,
// opens the user's browser for the InstalledAppFlow, and writes a fresh
// refresh-token-capable token to .state/token.json. Blocking on the user
// completing the browser flow (including 2FA if their account requires it).
export async function POST(request: Request) {
  const projectRoot = process.cwd().replace("/web", "");
  const granitePath = `${projectRoot}/.venv/bin/granite`;

  return new Promise<Response>((resolve) => {
    const proc = spawn(granitePath, ["ops", "reauth", "google"], {
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
        Response.json(
          { ok: false, error: err.message },
          { status: 500 }
        )
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
          payload = { message: stderr || stdout || "Reauth failed" };
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
