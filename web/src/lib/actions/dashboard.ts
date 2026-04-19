"use server";

import {
  getDashboardMetrics as getMetrics,
  getLastRuns as getRuns,
  getSyncCoverage as getCoverage,
  getPendingActions as getPending,
  getRunningJobs as getRunning,
  type DashboardMetrics,
  type LastRun,
  type SyncCoverage,
  type PendingAction,
  type RunningJob,
} from "@/lib/queries/dashboard";
import type { Result } from "@/lib/types";

export async function fetchDashboardMetrics(
  fy: string
): Promise<Result<DashboardMetrics>> {
  try {
    const metrics = getMetrics(fy);
    return { ok: true, data: metrics };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchLastRuns(): Promise<Result<LastRun[]>> {
  try {
    const runs = getRuns();
    return { ok: true, data: runs };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchSyncCoverage(): Promise<Result<SyncCoverage>> {
  try {
    const coverage = getCoverage();
    return { ok: true, data: coverage };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchPendingActions(): Promise<Result<PendingAction[]>> {
  try {
    const actions = getPending();
    return { ok: true, data: actions };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function cancelRun(
  operation: "ingest_email" | "ingest_invoice" | "reconcile"
): Promise<Result<{ cancelled: number }>> {
  try {
    const response = await fetch("http://localhost:3000/api/pipeline/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation }),
    });

    if (!response.ok) {
      throw new Error("Failed to cancel run");
    }

    const data = await response.json();
    return { ok: true, data: { cancelled: data.cancelled } };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "CANCEL_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}

export async function fetchRunningJobs(
  operation: "ingest_email" | "ingest_invoice" | "reconcile"
): Promise<Result<RunningJob[]>> {
  try {
    const jobs = getRunning(operation);
    return { ok: true, data: jobs };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "FETCH_ERROR",
        message: error instanceof Error ? error.message : "Unknown error",
      },
    };
  }
}
