import { spawn } from "child_process";
import { z } from "zod";
import { getGraniteBinary, getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

const VALID_STATES = ["personal", "verified", "ignore"] as const;

const resolveSchema = z.object({
  txnId: z.string().min(1),
  state: z.enum(VALID_STATES),
  invoiceId: z.string().optional(),
});

export async function POST(request: Request) {
  const body = await request.json();
  const result = resolveSchema.safeParse(body);

  if (!result.success) {
    return new Response(
      JSON.stringify({ error: "Invalid request", details: result.error.issues }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const { txnId, state, invoiceId } = result.data;

  if (state === "verified" && !invoiceId) {
    return new Response(
      JSON.stringify({ error: "verified state requires invoiceId" }),
      {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }
    );
  }

  const projectRoot = getProjectRoot();
  const granitePath = getGraniteBinary();

  const args = ["reconcile", "resolve", txnId, "--state", state];

  if (invoiceId) {
    args.push("--invoice-id", invoiceId);
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
        new Response(JSON.stringify({ error: err.message }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        })
      );
    });

    proc.on("close", (code) => {
      if (code === 0) {
        try {
          const parsed = JSON.parse(stdout);
          resolve(
            new Response(JSON.stringify(parsed), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            })
          );
        } catch {
          resolve(
            new Response(JSON.stringify({ status: "success" }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            })
          );
        }
      } else {
        try {
          const parsed = JSON.parse(stdout);
          resolve(
            new Response(JSON.stringify(parsed), {
              status: 400,
              headers: { "Content-Type": "application/json" },
            })
          );
        } catch {
          resolve(
            new Response(
              JSON.stringify({ error: stdout || stderr || "Command failed" }),
              {
                status: 500,
                headers: { "Content-Type": "application/json" },
              }
            )
          );
        }
      }
    });
  });
}
