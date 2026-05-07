import { spawn } from "child_process";
import { createInterface } from "readline";
import { writeFile, mkdir, unlink } from "fs/promises";
import { join } from "path";
import { randomUUID } from "crypto";
import { getGraniteBinary, getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB per file
const MAX_TOTAL_SIZE = 50 * 1024 * 1024; // 50MB total
const MAX_FILES = 20;
const SUBPROCESS_TIMEOUT_MS = 300_000; // 5 minutes for bulk

export async function POST(request: Request) {
  const formData = await request.formData();
  const files = formData.getAll("files") as File[];
  const fiscalYear = formData.get("fiscalYear") as string | null;

  if (!files || files.length === 0) {
    return new Response(JSON.stringify({ error: "No files provided" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  if (files.length > MAX_FILES) {
    return new Response(
      JSON.stringify({ error: `Too many files. Maximum is ${MAX_FILES}` }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const invalidFiles = files.filter(
    (f) => !f.name.toLowerCase().endsWith(".pdf")
  );
  if (invalidFiles.length > 0) {
    return new Response(
      JSON.stringify({
        error: "Only PDF files are allowed",
        invalid: invalidFiles.map((f) => f.name),
      }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const oversizedFiles = files.filter((f) => f.size > MAX_FILE_SIZE);
  if (oversizedFiles.length > 0) {
    return new Response(
      JSON.stringify({
        error: `Some files exceed ${MAX_FILE_SIZE / 1024 / 1024}MB limit`,
        oversized: oversizedFiles.map((f) => f.name),
      }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const totalSize = files.reduce((sum, f) => sum + f.size, 0);
  if (totalSize > MAX_TOTAL_SIZE) {
    return new Response(
      JSON.stringify({
        error: `Total upload size exceeds ${MAX_TOTAL_SIZE / 1024 / 1024}MB limit`,
      }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const projectRoot = getProjectRoot();
  const granitePath = getGraniteBinary();

  const tmpDir = join(projectRoot, ".tmp", "bulk-uploads");
  await mkdir(tmpDir, { recursive: true });

  const tmpPaths: string[] = [];

  for (const file of files) {
    const tmpPath = join(tmpDir, `${randomUUID()}.pdf`);
    const bytes = await file.arrayBuffer();
    await writeFile(tmpPath, Buffer.from(bytes));
    tmpPaths.push(tmpPath);
  }

  const args = ["reconcile", "bulk-upload", ...tmpPaths];

  if (fiscalYear) {
    args.push("--fy", fiscalYear);
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

        for (const p of tmpPaths) {
          unlink(p).catch(() => {});
        }
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

        for (const p of tmpPaths) {
          unlink(p).catch(() => {});
        }
      });

      request.signal.addEventListener("abort", () => {
        clearTimeout(timeout);
        proc.kill("SIGTERM");
        rl.close();
        for (const p of tmpPaths) {
          unlink(p).catch(() => {});
        }
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
