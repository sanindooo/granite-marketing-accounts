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

export type PipelineCommand = keyof typeof COMMANDS;

export async function runPipelineCommand(
  command: PipelineCommand,
  options?: { fiscalYear?: string }
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

    args.push("--json");

    const proc = spawn("granite", args, {
      cwd: process.cwd().replace("/web", ""),
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
