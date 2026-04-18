"use server";

import { spawn } from "child_process";
import { z } from "zod";
import type { Result, CliOutput } from "@/lib/types";

const COMMANDS = {
  syncEmails: ["ingest", "email", "ms365"],
  processInvoices: ["ingest", "invoice", "process"],
  runReconciliation: ["reconcile", "run"],
} as const;

const FiscalYearSchema = z.string().regex(/^FY-\d{4}-\d{2}$/);
const DateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const SenderSchema = z.string().min(1).max(100);

export type PipelineCommand = keyof typeof COMMANDS;

export interface PipelineOptions {
  fiscalYear?: string;
  limit?: number;
  sender?: string;
  dateFrom?: string;
  dateTo?: string;
}

export async function runPipelineCommand(
  command: PipelineCommand,
  options?: PipelineOptions
): Promise<Result<CliOutput>> {
  return new Promise((resolve) => {
    const args: string[] = [...COMMANDS[command]];

    if (options?.fiscalYear) {
      try {
        const fy = FiscalYearSchema.parse(options.fiscalYear);
        args.push("--fy", fy);
      } catch {
        resolve({
          ok: false,
          error: {
            code: "INVALID_FY",
            message: "Invalid fiscal year format",
          },
        });
        return;
      }
    }

    // Sender search (for email sync)
    if (options?.sender && command === "syncEmails") {
      try {
        const sender = SenderSchema.parse(options.sender);
        args.push("--sender", sender);
      } catch {
        resolve({
          ok: false,
          error: { code: "INVALID_SENDER", message: "Invalid sender search" },
        });
        return;
      }
    }

    // Date range filters (for email sync)
    if (options?.dateFrom && command === "syncEmails") {
      try {
        const date = DateSchema.parse(options.dateFrom);
        args.push("--from", date);
      } catch {
        resolve({
          ok: false,
          error: { code: "INVALID_DATE", message: "Invalid from date" },
        });
        return;
      }
    }

    if (options?.dateTo && command === "syncEmails") {
      try {
        const date = DateSchema.parse(options.dateTo);
        args.push("--to", date);
      } catch {
        resolve({
          ok: false,
          error: { code: "INVALID_DATE", message: "Invalid to date" },
        });
        return;
      }
    }

    if (options?.limit && command === "processInvoices") {
      args.push("--limit", String(options.limit));
    }

    // Only add --json for commands that support it
    if (command === "processInvoices" || command === "runReconciliation") {
      args.push("--json");
    }

    const projectRoot = process.cwd().replace("/web", "");
    const granitePath = `${projectRoot}/.venv/bin/granite`;

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
      resolve({
        ok: false,
        error: {
          code: "SPAWN_ERROR",
          message: err.message,
        },
      });
    });

    proc.on("close", (code) => {
      if (code !== 0) {
        try {
          const parsed = JSON.parse(stdout || stderr) as CliOutput;
          if (parsed.error_code === "needs_reauth") {
            resolve({
              ok: false,
              error: {
                code: "NEEDS_REAUTH",
                message: parsed.message || "Authentication required",
                userMessage: `Run \`granite ops reauth ms365\` in terminal`,
              },
            });
            return;
          }
          resolve({
            ok: false,
            error: {
              code: parsed.error_code || "CLI_ERROR",
              message: parsed.message || "Command failed",
              userMessage: parsed.user_message,
            },
          });
        } catch {
          resolve({
            ok: false,
            error: {
              code: "CLI_ERROR",
              message: stderr || stdout || "Command failed",
            },
          });
        }
        return;
      }

      try {
        const parsed = JSON.parse(stdout) as CliOutput;
        resolve({ ok: true, data: parsed });
      } catch {
        resolve({
          ok: true,
          data: { status: "success", message: stdout.trim() },
        });
      }
    });
  });
}
