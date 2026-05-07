import { spawn } from "child_process";
import { z } from "zod";
import { getGraniteBinary, getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

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

    const exitCode = await new Promise<number>((resolve) => {
      const proc = spawn(granitePath, args, {
        cwd: projectRoot,
        shell: false,
        env: { ...process.env },
      });

      proc.on("close", (code) => resolve(code ?? 1));
      proc.on("error", () => resolve(1));
    });

    if (exitCode === 0) {
      resolved++;
    } else {
      errors.push(txnId);
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
