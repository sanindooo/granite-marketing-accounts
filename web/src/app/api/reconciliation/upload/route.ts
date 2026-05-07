import { spawn } from "child_process";
import { createInterface } from "readline";
import { writeFile, mkdir, unlink } from "fs/promises";
import { join } from "path";
import { randomUUID } from "crypto";
import { z } from "zod";
import { getGraniteBinary, getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

const VALID_ACCOUNTS = ["amex", "wise", "tide", "monzo"] as const;
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB
const SUBPROCESS_TIMEOUT_MS = 120_000; // 2 minutes

const uploadSchema = z.object({
  account: z.enum(VALID_ACCOUNTS),
  fiscalYear: z
    .string()
    .regex(/^FY-\d{4}-\d{2}$/)
    .optional(),
});

export async function POST(request: Request) {
  const formData = await request.formData();
  const file = formData.get("file") as File | null;
  const account = formData.get("account") as string | null;
  const fiscalYear = formData.get("fiscalYear") as string | null;

  if (!file) {
    return new Response(JSON.stringify({ error: "No file provided" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  if (file.size > MAX_FILE_SIZE) {
    return new Response(
      JSON.stringify({ error: `File too large. Maximum size is ${MAX_FILE_SIZE / 1024 / 1024}MB` }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const fileName = file.name.toLowerCase();
  if (!fileName.endsWith(".pdf") && !fileName.endsWith(".csv")) {
    return new Response(
      JSON.stringify({ error: "Invalid file type. Only PDF and CSV files are accepted" }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const result = uploadSchema.safeParse({
    account,
    fiscalYear: fiscalYear || undefined,
  });

  if (!result.success) {
    return new Response(
      JSON.stringify({ error: "Invalid request", details: result.error.issues }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const projectRoot = getProjectRoot();
  const granitePath = getGraniteBinary();

  const tmpDir = join(projectRoot, ".tmp", "uploads");
  await mkdir(tmpDir, { recursive: true });

  const ext = file.name.endsWith(".pdf") ? ".pdf" : ".csv";
  const tmpPath = join(tmpDir, `${randomUUID()}${ext}`);

  const bytes = await file.arrayBuffer();
  await writeFile(tmpPath, Buffer.from(bytes));

  const args = ["reconcile", "upload", tmpPath, "--account", result.data.account];

  if (result.data.fiscalYear) {
    args.push("--fy", result.data.fiscalYear);
  }

  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    start(controller) {
      const proc = spawn(granitePath, args, {
        cwd: projectRoot,
        shell: false,
        env: { ...process.env },
      });

      let stdout = "";
      let timedOut = false;

      const timeout = setTimeout(() => {
        timedOut = true;
        proc.kill("SIGTERM");
      }, SUBPROCESS_TIMEOUT_MS);

      const rl = createInterface({ input: proc.stderr });
      rl.on("line", (line) => {
        try {
          const event = JSON.parse(line);
          if (event.event === "progress") {
            const sseData = `data: ${JSON.stringify(event)}\n\n`;
            controller.enqueue(encoder.encode(sseData));
          }
        } catch {
          // Non-JSON stderr line, ignore
        }
      });

      proc.stdout.on("data", (data) => {
        stdout += data.toString();
      });

      proc.on("error", (err) => {
        clearTimeout(timeout);
        const errorEvent = `data: ${JSON.stringify({
          event: "error",
          message: err.message,
        })}\n\n`;
        controller.enqueue(encoder.encode(errorEvent));
        controller.close();

        unlink(tmpPath).catch(() => {});
      });

      proc.on("close", (code) => {
        clearTimeout(timeout);
        let finalEvent;
        if (timedOut) {
          finalEvent = { event: "error", message: "Processing timed out" };
        } else if (code === 0) {
          try {
            const parsed = JSON.parse(stdout);
            finalEvent = { event: "complete", result: parsed };
          } catch {
            finalEvent = { event: "complete", result: { status: "success" } };
          }
        } else {
          try {
            const parsed = JSON.parse(stdout);
            finalEvent = { event: "error", ...parsed };
          } catch {
            finalEvent = { event: "error", message: stdout || "Command failed" };
          }
        }

        controller.enqueue(encoder.encode(`data: ${JSON.stringify(finalEvent)}\n\n`));
        controller.close();

        unlink(tmpPath).catch(() => {});
      });

      request.signal.addEventListener("abort", () => {
        clearTimeout(timeout);
        proc.kill("SIGTERM");
        rl.close();
        unlink(tmpPath).catch(() => {});
      });
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}
