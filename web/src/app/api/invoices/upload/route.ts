import { NextResponse } from "next/server";
import { spawn } from "child_process";
import { writeFile, mkdir, unlink } from "fs/promises";
import { join } from "path";
import { randomUUID } from "crypto";
import { getGraniteBinary, getProjectRoot } from "@/lib/spawn-granite";

export async function POST(request: Request) {
  try {
    const formData = await request.formData();
    const msgId = formData.get("msgId") as string;
    const pdf = formData.get("pdf") as File;

    if (!msgId || typeof msgId !== "string" || !msgId.trim()) {
      return NextResponse.json(
        { error: "Missing or invalid msgId" },
        { status: 400 }
      );
    }

    if (!pdf || !(pdf instanceof File) || !pdf.name.toLowerCase().endsWith(".pdf")) {
      return NextResponse.json(
        { error: "Missing or invalid PDF file" },
        { status: 400 }
      );
    }

    const projectRoot = getProjectRoot();
    const tmpDir = join(projectRoot, ".tmp", "uploads");
    await mkdir(tmpDir, { recursive: true });

    const tmpPath = join(tmpDir, `${randomUUID()}.pdf`);
    const bytes = await pdf.arrayBuffer();
    await writeFile(tmpPath, Buffer.from(bytes));

    const granitePath = getGraniteBinary();

    return new Promise<Response>((resolve) => {
      const proc = spawn(
        granitePath,
        ["ingest", "invoice", "upload-pdf", msgId, tmpPath],
        {
          cwd: projectRoot,
          shell: false,
          env: { ...process.env },
        }
      );

      let stdout = "";
      let stderr = "";

      proc.stdout.on("data", (data) => {
        stdout += data.toString();
      });

      proc.stderr.on("data", (data) => {
        stderr += data.toString();
      });

      proc.on("error", async (err) => {
        await unlink(tmpPath).catch(() => {});
        resolve(NextResponse.json({ error: err.message }, { status: 500 }));
      });

      proc.on("close", async (code) => {
        await unlink(tmpPath).catch(() => {});

        if (code !== 0) {
          try {
            const parsed = JSON.parse(stdout || stderr);
            resolve(
              NextResponse.json(
                { error: parsed.message || "Upload failed" },
                { status: 500 }
              )
            );
          } catch {
            resolve(
              NextResponse.json(
                { error: stderr || stdout || "Upload failed" },
                { status: 500 }
              )
            );
          }
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
    console.error("Upload error:", error);
    return NextResponse.json(
      { error: "Failed to process upload" },
      { status: 500 }
    );
  }
}

export const runtime = "nodejs";
