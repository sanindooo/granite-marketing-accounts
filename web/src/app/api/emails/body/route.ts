import { NextResponse } from "next/server";
import { spawn } from "child_process";
import { z } from "zod";

const bodySchema = z.object({
  msgId: z.string().min(1),
});

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const result = bodySchema.safeParse(body);

    if (!result.success) {
      return NextResponse.json(
        { error: "Invalid request", details: result.error.issues },
        { status: 400 }
      );
    }

    const { msgId } = result.data;

    const projectRoot = process.cwd().replace("/web", "");
    const granitePath = `${projectRoot}/.venv/bin/granite`;

    return new Promise<Response>((resolve) => {
      const proc = spawn(granitePath, ["ingest", "email", "body", msgId], {
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
          NextResponse.json({ error: err.message }, { status: 500 })
        );
      });

      proc.on("close", (code) => {
        if (code !== 0) {
          resolve(
            NextResponse.json(
              { error: stderr || stdout || "Failed to fetch email body" },
              { status: 500 }
            )
          );
          return;
        }

        try {
          const parsed = JSON.parse(stdout);
          resolve(NextResponse.json(parsed));
        } catch {
          resolve(
            NextResponse.json(
              { error: "Invalid response from CLI" },
              { status: 500 }
            )
          );
        }
      });
    });
  } catch (error) {
    console.error("Email body error:", error);
    return NextResponse.json(
      { error: "Failed to fetch email body" },
      { status: 500 }
    );
  }
}

export const runtime = "nodejs";
