import { spawn } from "child_process";
import { z } from "zod";
import { getGraniteBinary, getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

const MAX_TRANSACTIONS = 100;
const SUBPROCESS_TIMEOUT_MS = 10_000; // 10 seconds per transaction

const VALID_REASONS = [
  "personal",
  "transfer_to_self",
  "travel",
  "food",
  "subscription",
  "bank_fee",
  "other",
] as const;

const bulkResolveSchema = z.object({
  txnIds: z.array(z.string().min(1)).min(1),
  reason: z.enum(VALID_REASONS),
  note: z.string().optional(),
});

export async function POST(request: Request) {
  const body = await request.json();
  const result = bulkResolveSchema.safeParse(body);

  if (!result.success) {
    return new Response(
      JSON.stringify({ error: "Invalid request", details: result.error.issues }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const { txnIds, reason, note } = result.data;

  if (txnIds.length > MAX_TRANSACTIONS) {
    return new Response(
      JSON.stringify({ error: `Too many transactions. Maximum is ${MAX_TRANSACTIONS}` }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  if (reason === "other" && !note?.trim()) {
    return new Response(
      JSON.stringify({ error: "Custom note required for 'other' reason" }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const projectRoot = getProjectRoot();
  const granitePath = getGraniteBinary();

  // Map reasons to CLI states
  const stateMap: Record<string, string> = {
    personal: "personal",
    transfer_to_self: "personal",
    travel: "personal",
    food: "personal",
    subscription: "ignore",
    bank_fee: "ignore",
    other: "personal",
  };

  const state = stateMap[reason] || "personal";
  const resolveNote = reason === "other" ? note : reason.replace(/_/g, " ");

  let resolved = 0;
  const errors: string[] = [];

  // Process each transaction
  for (const txnId of txnIds) {
    const args = ["reconcile", "resolve", txnId, "--state", state];
    if (resolveNote) {
      args.push("--note", resolveNote);
    }

    const result = await new Promise<{ code: number; stdout: string }>((resolve) => {
      const proc = spawn(granitePath, args, {
        cwd: projectRoot,
        shell: false,
        env: { ...process.env },
      });

      let stdout = "";
      let resolved = false;

      const timeout = setTimeout(() => {
        if (!resolved) {
          resolved = true;
          proc.kill("SIGTERM");
          resolve({ code: 1, stdout: "Timed out" });
        }
      }, SUBPROCESS_TIMEOUT_MS);

      proc.stdout.on("data", (data) => {
        stdout += data.toString();
      });
      proc.stderr.on("data", (data) => {
        stdout += data.toString();
      });

      proc.on("close", (code) => {
        if (!resolved) {
          resolved = true;
          clearTimeout(timeout);
          resolve({ code: code ?? 1, stdout });
        }
      });
      proc.on("error", (err) => {
        if (!resolved) {
          resolved = true;
          clearTimeout(timeout);
          resolve({ code: 1, stdout: err.message });
        }
      });
    });

    if (result.code === 0) {
      resolved++;
    } else {
      // Try to extract error message from CLI output
      let errorMsg = txnId;
      try {
        const parsed = JSON.parse(result.stdout);
        if (parsed.message) {
          errorMsg = `${txnId}: ${parsed.message}`;
        }
      } catch {
        // JSON parse failed, use raw output if available
        if (result.stdout.trim()) {
          errorMsg = `${txnId}: ${result.stdout.trim().slice(0, 100)}`;
        }
      }
      errors.push(errorMsg);
    }
  }

  if (errors.length > 0 && resolved === 0) {
    return new Response(
      JSON.stringify({
        status: "error",
        message: `Failed to resolve any transactions`,
        errors,
      }),
      {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  return new Response(
    JSON.stringify({
      status: "success",
      resolved,
      total: txnIds.length,
      errors: errors.length > 0 ? errors : undefined,
    }),
    {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }
  );
}
