import { spawn } from "child_process";
import { writeFile, mkdir } from "fs/promises";
import { join } from "path";
import { randomUUID } from "crypto";
import Database from "better-sqlite3";
import { getProjectRoot } from "@/lib/spawn-granite";

export const runtime = "nodejs";

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB per file
const MAX_TOTAL_SIZE = 200 * 1024 * 1024; // 200MB total
const MAX_FILES = 100;

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

  // Write files to temp directory
  const tmpDir = join(projectRoot, ".tmp", "bulk-uploads");
  await mkdir(tmpDir, { recursive: true });

  const tmpPaths: string[] = [];
  const fileNames: string[] = [];

  for (const file of files) {
    const tmpPath = join(tmpDir, `${randomUUID()}.pdf`);
    const bytes = await file.arrayBuffer();
    await writeFile(tmpPath, Buffer.from(bytes));
    tmpPaths.push(tmpPath);
    fileNames.push(file.name);
  }

  // Create job record
  const jobId = `job_${randomUUID().slice(0, 8)}`;
  const now = new Date().toISOString();

  const db = new Database(join(projectRoot, "granite.db"));
  try {
    db.exec(`
      CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        job_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        progress_current INTEGER DEFAULT 0,
        progress_total INTEGER DEFAULT 0,
        progress_message TEXT,
        result_json TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      )
    `);

    db.prepare(`
      INSERT INTO jobs (job_id, job_type, status, progress_total, progress_message, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `).run(jobId, "bulk_upload", "pending", files.length, "Queued...", now, now);
  } finally {
    db.close();
  }

  // Spawn background worker (detached)
  const pythonPath = join(projectRoot, ".venv", "bin", "python");
  const workerArgs = [
    "-m",
    "execution.jobs.bulk_upload_worker",
    jobId,
    ...tmpPaths,
  ];

  if (fiscalYear) {
    workerArgs.push("--fy", fiscalYear);
  }

  const worker = spawn(pythonPath, workerArgs, {
    cwd: projectRoot,
    detached: true,
    stdio: "ignore",
    env: { ...process.env },
  });

  worker.unref();

  return new Response(
    JSON.stringify({
      status: "accepted",
      jobId,
      fileCount: files.length,
      fileNames,
    }),
    {
      status: 202,
      headers: { "Content-Type": "application/json" },
    }
  );
}
