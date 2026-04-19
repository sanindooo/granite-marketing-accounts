"use server";

import {
  getDashboardMetrics as getMetrics,
  getLastRuns as getRuns,
  getSyncCoverage as getCoverage,
  getPendingActions as getPending,
  getRunningJobs as getRunning,
  getFxErrors as getFx,
  getFxErrorCount as getFxCount,
  type DashboardMetrics,
  type LastRun,
  type SyncCoverage,
  type PendingAction,
  type RunningJob,
  type FxError,
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

export async function fetchPendingActions(fy?: string): Promise<Result<PendingAction[]>> {
  try {
    const actions = getPending(fy);
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
    const { cancelRunningJobs } = await import("@/lib/queries/dashboard");
    const cancelled = cancelRunningJobs(operation);
    return { ok: true, data: { cancelled } };
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

export async function fetchFxErrors(): Promise<Result<FxError[]>> {
  try {
    const errors = getFx();
    return { ok: true, data: errors };
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

export async function fetchFxErrorCount(): Promise<Result<number>> {
  try {
    const count = getFxCount();
    return { ok: true, data: count };
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
