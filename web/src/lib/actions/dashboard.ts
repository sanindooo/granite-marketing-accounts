"use server";

import {
  getDashboardMetrics as getMetrics,
  getLastRuns as getRuns,
  getSyncCoverage as getCoverage,
  getActiveRun as getActive,
  getPendingActions as getPending,
  type DashboardMetrics,
  type LastRun,
  type SyncCoverage,
  type ActiveRun,
  type PendingAction,
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

export async function fetchActiveRun(
  operation: string
): Promise<Result<ActiveRun | null>> {
  try {
    const run = getActive(operation);
    return { ok: true, data: run };
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
