/** R2 snapshots published by Python (`app/edge_cache.py`) for idle edge reads. */

import type { Env } from "./env";

export type EdgeActivity = {
  uid?: string;
  hasWork?: boolean;
  pendingSteps?: number;
  runtimeJobs?: number;
  updatedAt?: string;
};

function activityKey(uid: string): string {
  return `edge-cache/${uid}/activity.json`;
}
function inventoryKey(uid: string): string {
  return `edge-cache/${uid}/inventory.json`;
}
function provisionJobsKey(uid: string): string {
  return `edge-cache/${uid}/provision-jobs.json`;
}
function jobsActiveKey(uid: string): string {
  return `edge-cache/${uid}/jobs-active.json`;
}

async function readJson<T>(
  env: Env,
  key: string,
): Promise<T | null> {
  const obj = await env.DATA.get(key);
  if (!obj) return null;
  try {
    return (await obj.json()) as T;
  } catch {
    return null;
  }
}

export async function readActivity(
  env: Env,
  uid: string,
): Promise<EdgeActivity | null> {
  return readJson<EdgeActivity>(env, activityKey(uid));
}

/** True when nothing has explicitly marked work in progress. A missing file is idle. */
export function isConfidentlyIdle(activity: EdgeActivity | null): boolean {
  return !hasActiveWork(activity);
}

export function hasActiveWork(activity: EdgeActivity | null): boolean {
  return !!activity && activity.hasWork === true;
}

export async function readInventorySnapshot(
  env: Env,
  uid: string,
): Promise<Record<string, unknown> | null> {
  return readJson<Record<string, unknown>>(env, inventoryKey(uid));
}

export async function readProvisionJobsSnapshot(
  env: Env,
  uid: string,
): Promise<Record<string, unknown> | null> {
  return readJson<Record<string, unknown>>(env, provisionJobsKey(uid));
}

export async function readJobsActiveSnapshot(
  env: Env,
  uid: string,
): Promise<Record<string, unknown> | null> {
  return readJson<Record<string, unknown>>(env, jobsActiveKey(uid));
}

export async function waitInventorySnapshot(
  env: Env,
  uid: string,
  since: number,
  timeoutSec: number,
): Promise<{
  changed: boolean;
  generation: number;
  inventory: Record<string, unknown> | null;
  edge: boolean;
}> {
  const timeoutMs = Math.max(1000, Math.min(25_000, timeoutSec * 1000));
  const deadline = Date.now() + timeoutMs;
  const sinceI = Number(since) || 0;

  while (Date.now() < deadline) {
    const inv = await readInventorySnapshot(env, uid);
    const gen = Number(inv?.generation ?? 0) || 0;
    if (gen > sinceI) {
      return { changed: true, generation: gen, inventory: inv, edge: true };
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  const inv = await readInventorySnapshot(env, uid);
  const gen = Number(inv?.generation ?? sinceI) || sinceI;
  return { changed: false, generation: gen, inventory: inv, edge: true };
}
