"use server";

import {
  getDashboardMetrics as getMetrics,
  getLastRuns as getRuns,
  type DashboardMetrics,
  type LastRun,
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
