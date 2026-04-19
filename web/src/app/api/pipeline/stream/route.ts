import { spawn } from "child_process";
import { createInterface } from "readline";
import { z } from "zod";

export const runtime = "nodejs";

const COMMANDS = {
  syncEmails: ["ingest", "email", "ms365"],
  processInvoices: ["ingest", "invoice", "process"],
  runReconciliation: ["reconcile", "run"],
} as const;

const commandSchema = z.object({
  command: z.enum(["syncEmails", "processInvoices", "runReconciliation"]),
  fiscalYear: z.string().regex(/^FY-\d{4}-\d{2}$/).optional(),
  sender: z.string().min(1).max(100).optional(),
  dateFrom: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  dateTo: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  backfillFrom: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
  limit: z.number().int().min(1).max(100).optional(),
  reset: z.boolean().optional(),
  rescan: z.boolean().optional(),
  workers: z.number().int().min(1).max(20).optional(),
  model: z.enum(["claude", "openai"]).optional(),
});

type PipelineCommand = keyof typeof COMMANDS;

function buildArgs(command: PipelineCommand, options: z.infer<typeof commandSchema>): string[] {
  const args: string[] = [...COMMANDS[command]];

  if (options.fiscalYear && command === "runReconciliation") {
    args.push("--fy", options.fiscalYear);
  }

  if (options.sender && command === "syncEmails") {
    args.push("--sender", options.sender);
  }

  if (options.dateFrom && command === "syncEmails") {
    args.push("--from", options.dateFrom);
  }

  if (options.dateTo && command === "syncEmails") {
    args.push("--to", options.dateTo);
  }

  if (options.backfillFrom && command === "syncEmails") {
    args.push("--backfill-from", options.backfillFrom);
  }

  if (options.reset && command === "syncEmails") {
    args.push("--reset");
  }

  if (options.rescan && command === "syncEmails") {
    args.push("--rescan");
  }

  if (options.limit && command === "processInvoices") {
    args.push("--limit", String(options.limit));
  }

  if (options.fiscalYear && command === "processInvoices") {
    args.push("--fy", options.fiscalYear);
  }

  if (options.workers && command === "processInvoices") {
    args.push("--workers", String(options.workers));
  }

  if (options.model && command === "processInvoices") {
    args.push("--model", options.model);
  }

  return args;
}

export async function POST(request: Request) {
  const body = await request.json();
  const result = commandSchema.safeParse(body);

  if (!result.success) {
    return new Response(JSON.stringify({ error: "Invalid request" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const { command } = result.data;
  const args = buildArgs(command, result.data);

  const projectRoot = process.cwd().replace("/web", "");
  const granitePath = `${projectRoot}/.venv/bin/granite`;

  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    start(controller) {
      const proc = spawn(granitePath, args, {
        cwd: projectRoot,
        shell: false,
        env: { ...process.env },
      });

      let stdout = "";

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
        const errorEvent = `data: ${JSON.stringify({
          event: "error",
          message: err.message,
        })}\n\n`;
        controller.enqueue(encoder.encode(errorEvent));
        controller.close();
      });

      proc.on("close", (code) => {
        let finalEvent;
        if (code === 0) {
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
      });

      request.signal.addEventListener("abort", () => {
        proc.kill("SIGTERM");
        rl.close();
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
