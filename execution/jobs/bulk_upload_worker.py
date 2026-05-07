#!/usr/bin/env python3
"""Background worker for bulk invoice upload jobs.

This script is spawned by the API and runs detached from the HTTP request.
It executes the CLI command and updates the job record with progress/results.

Usage:
    python -m execution.jobs.bulk_upload_worker <job_id> <file1.pdf> [file2.pdf ...]
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, UTC
from pathlib import Path

# Project root for finding the CLI and database
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def get_db_path() -> Path:
    """Get the database path."""
    return PROJECT_ROOT / "granite.db"


def update_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    progress_current: int | None = None,
    progress_total: int | None = None,
    progress_message: str | None = None,
    result_json: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update job record in database."""
    updates = []
    params = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if progress_current is not None:
        updates.append("progress_current = ?")
        params.append(progress_current)
    if progress_total is not None:
        updates.append("progress_total = ?")
        params.append(progress_total)
    if progress_message is not None:
        updates.append("progress_message = ?")
        params.append(progress_message)
    if result_json is not None:
        updates.append("result_json = ?")
        params.append(result_json)
    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)

    updates.append("updated_at = ?")
    params.append(datetime.now(UTC).isoformat())
    params.append(job_id)

    conn.execute(
        f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?",
        params,
    )
    conn.commit()


def run_bulk_upload(job_id: str, file_paths: list[str], fiscal_year: str | None = None) -> None:
    """Run the bulk upload CLI command and track progress."""
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))

    try:
        # Mark job as running
        update_job(
            conn,
            job_id,
            status="running",
            progress_total=len(file_paths),
            progress_message="Starting upload...",
        )

        # Build CLI command
        cli_path = PROJECT_ROOT / ".venv" / "bin" / "granite"
        if not cli_path.exists():
            cli_path = PROJECT_ROOT / ".venv" / "bin" / "python"
            args = [str(cli_path), "-m", "execution.cli", "reconcile", "bulk-upload"] + file_paths
        else:
            args = [str(cli_path), "reconcile", "bulk-upload"] + file_paths

        if fiscal_year:
            args.extend(["--fy", fiscal_year])

        # Run CLI and capture output
        proc = subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Read stderr for progress events
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("event") == "progress":
                    update_job(
                        conn,
                        job_id,
                        progress_current=event.get("current", 0),
                        progress_total=event.get("total", len(file_paths)),
                        progress_message=event.get("message", "Processing..."),
                    )
            except json.JSONDecodeError:
                pass  # Non-JSON stderr line

        # Wait for completion
        stdout, _ = proc.communicate()

        if proc.returncode == 0:
            # Parse result from stdout
            try:
                result = json.loads(stdout)
                update_job(
                    conn,
                    job_id,
                    status="complete",
                    progress_current=len(file_paths),
                    progress_message="Complete",
                    result_json=stdout,
                )
            except json.JSONDecodeError:
                update_job(
                    conn,
                    job_id,
                    status="complete",
                    progress_current=len(file_paths),
                    progress_message="Complete",
                    result_json=json.dumps({"status": "success"}),
                )
        else:
            # Command failed
            error_msg = stdout.strip() if stdout else "Command failed"
            try:
                error_data = json.loads(stdout)
                error_msg = error_data.get("message", error_msg)
            except json.JSONDecodeError:
                pass

            update_job(
                conn,
                job_id,
                status="failed",
                error_message=error_msg,
            )

    except Exception as e:
        update_job(
            conn,
            job_id,
            status="failed",
            error_message=str(e),
        )
    finally:
        conn.close()

        # Clean up temp files
        for path in file_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python -m execution.jobs.bulk_upload_worker <job_id> <file1.pdf> [file2.pdf ...]", file=sys.stderr)
        sys.exit(1)

    job_id = sys.argv[1]

    # Check for --fy flag
    fiscal_year = None
    file_paths = []
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--fy" and i + 1 < len(sys.argv):
            fiscal_year = sys.argv[i + 1]
            i += 2
        else:
            file_paths.append(sys.argv[i])
            i += 1

    run_bulk_upload(job_id, file_paths, fiscal_year)
