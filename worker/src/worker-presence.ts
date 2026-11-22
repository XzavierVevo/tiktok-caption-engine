/** Edge-cached provision worker presence (AdsPower / online chip).

The desktop worker refreshes this about every 30 seconds. A crashed host
never sends online=false, so a read older than 10 minutes is offline even
when the stored flag is still true. That stale flag used to start a Business
Center with nobody to claim it.
*/

import type { Env } from "./env";

export type WorkerPresence = {
  online: boolean;
  lastSeenAt: string;
  meta: Record<string, unknown>;
  hostId?: string | null;
  hostName?: string | null;
  adspowerHealthy?: boolean | null;
  edge?: boolean;
};

function r2Key(uid: string): string {
  return `provision-worker-presence/${encodeURIComponent(uid)}.json`;
}

export async function writeWorkerPresence(
  env: Env,
  uid: string,
  body: { online?: boolean; meta?: Record<string, unknown> },
): Promise<WorkerPresence> {
  const meta =
    body.meta && typeof body.meta === "object" && !Array.isArray(body.meta)
      ? (body.meta as Record<string, unknown>)
      : {};
  const onlineFlag = body.online !== false;
  const lastSeenAt = new Date().toISOString();
  const presence: WorkerPresence = {
    online: onlineFlag,
    lastSeenAt,
    meta,
    hostId: (meta.hostId as string) || null,
    hostName: (meta.hostName as string) || null,
    adspowerHealthy:
      typeof meta.adspowerHealthy === "boolean" ? meta.adspowerHealthy : null,
    edge: true,
  };
  await env.DATA.put(r2Key(uid), JSON.stringify(presence), {
    httpMetadata: { contentType: "application/json" },
  });
  return presence;
}

export function presenceProxyHeaders(
  presence: WorkerPresence | null,
): Record<string, string> {
  if (!presence) return {};
  const headers: Record<string, string> = {
    "x-provision-edge-worker-online": presence.online ? "1" : "0",
  };
  if (presence.adspowerHealthy === true) {
    headers["x-provision-edge-adspower-healthy"] = "1";
  } else if (presence.adspowerHealthy === false) {
    headers["x-provision-edge-adspower-healthy"] = "0";
  }
  if (presence.lastSeenAt) {
    headers["x-provision-edge-worker-seen"] = String(presence.lastSeenAt);
  }
  return headers;
}

export async function resolvePresence(
  env: Env,
  uid: string,
): Promise<WorkerPresence | null> {
  const ownerUid = (env.PROVISION_OWNER_UID || "").trim() || uid;
  let presence = uid ? await readWorkerPresence(env, uid) : null;
  if (
    ownerUid &&
    ownerUid !== uid &&
    (!presence || !presence.online || presence.adspowerHealthy !== true)
  ) {
    const ownerPresence = await readWorkerPresence(env, ownerUid);
    if (ownerPresence && (ownerPresence.online || !presence?.online)) {
      presence = ownerPresence;
    }
  }
  return presence;
}

export async function readWorkerPresence(
  env: Env,
  uid: string,
): Promise<WorkerPresence | null> {
  const obj = await env.DATA.get(r2Key(uid));
  if (!obj) return null;
  try {
    const raw = (await obj.json()) as WorkerPresence;
    if (!raw || typeof raw !== "object") return null;
    const meta =
      raw.meta && typeof raw.meta === "object" && !Array.isArray(raw.meta)
        ? raw.meta
        : {};
    const lastSeenAt = String(raw.lastSeenAt || "");
    const seenMs = Date.parse(lastSeenAt);
    const stale = !Number.isFinite(seenMs) || Date.now() - seenMs > 10 * 60 * 1000;
    const flaggedOn = raw.online !== false;
    return {
      online: flaggedOn && !stale,
      lastSeenAt,
      meta,
      hostId: (raw.hostId as string) || (meta.hostId as string) || null,
      hostName: (raw.hostName as string) || (meta.hostName as string) || null,
      adspowerHealthy:
        typeof raw.adspowerHealthy === "boolean"
          ? raw.adspowerHealthy
          : typeof meta.adspowerHealthy === "boolean"
            ? meta.adspowerHealthy
            : null,
      edge: true,
    };
  } catch {
    return null;
  }
}
