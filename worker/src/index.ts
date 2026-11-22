/**
 * Cloudflare Worker front door — thin gateway.
 *
 * - Workers Assets serve static/ + fonts (no container wake for HTML)
 * - Idle status polls read R2 edge-cache (no Container Memory bill)
 * - Mutating / busy /api/* proxied to CaptionToolContainer (FastAPI brain)
 * - /mcp Streamable HTTP MCP (auth → quotas → /internal/mcp/call)
 * - /api/spark/callback handled here then persisted via Python
 * - Cron deletes old R2 media without starting the container
 */
import { Hono } from "hono";
import { schedulerRoute } from "./orchestration/routes";
export { WorkCoordinator } from "./orchestration/coordinator";
import type { Env } from "./env";
import { CaptionToolContainer } from "./container";
import { proxyToContainer, requestIdFrom, touchContainer } from "./proxy";
import { resolveMcpAuth, HttpError } from "./auth/mcp";
import { handleMcpRequest } from "./mcp/server";
import { handleOAuthToken } from "./auth/oauth-exchange";
import { handleTikTokOAuth } from "./tiktok-oauth";
import { handleSparkCallback } from "./spark-callback";
import { containerInternal } from "./proxy";
import { bearerFrom, verifyFirebaseToken } from "./auth/firebase";
import {
  presenceProxyHeaders,
  readWorkerPresence,
  resolvePresence,
  writeWorkerPresence,
} from "./worker-presence";
import {
  hasActiveWork,
  readActivity,
  readInventorySnapshot,
  readJobsActiveSnapshot,
  readProvisionJobsSnapshot,
  waitInventorySnapshot,
} from "./edge-cache";
import { authorized, stateBatch, stateDelete, stateGet, stateHealth, stateList, statePut, sparkPhoneLock } from "./state";
import { handleGo } from "./affiliate-edge";
import { retainRemoteMedia } from "./retention";

export { CaptionToolContainer };

type Variables = {
  requestId: string;
};

const app = new Hono<{ Bindings: Env; Variables: Variables }>();

app.use("*", async (c, next) => {
  c.set("requestId", requestIdFrom(c.req.raw));
  await next();
});

/** Never wake the container for a health probe — idle billing depends on it. */
app.get("/healthz", (c) => c.json({ ok: true, worker: true, edge: true }));

// Private container→Worker state plane. These routes never proxy to Python.
app.get("/_internal/state/health", stateHealth);
app.post("/_internal/state/get", stateGet);
app.post("/_internal/state/list", stateList);
app.post("/_internal/state/put", statePut);
app.post("/_internal/state/delete", stateDelete);
app.post("/_internal/state/batch", stateBatch);
app.post("/_internal/state/spark-phone-lock", sparkPhoneLock);
app.post("/_internal/orchestration/:operation", schedulerRoute);

/** Python calls this while a job is running. Does not start a sleeping container. */
app.post("/_internal/container-touch", async (c) => {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  return c.json(await touchContainer(c.env));
});

/** Firebase public web config — served from the Worker so sign-in
 *  recognition never waits on a cold Python container. */
app.get("/api/auth-config", (c) => {
  const projectId = (c.env.FIREBASE_PROJECT_ID || "").trim();
  const allowDev = (c.env.ALLOW_DEV_NO_AUTH || "").trim() === "1";
  if (!projectId) {
    return c.json({
      mode: allowDev ? "dev" : "unconfigured",
      firebase: null,
    });
  }
  return c.json({
    mode: "firebase",
    firebase: {
      apiKey: (c.env.FIREBASE_API_KEY || "").trim(),
      authDomain:
        (c.env.FIREBASE_AUTH_DOMAIN || "").trim() ||
        `${projectId}.firebaseapp.com`,
      projectId,
      appId: (c.env.FIREBASE_APP_ID || "").trim(),
    },
  });
});

app.post("/mcp/oauth/token", async (c) => handleOAuthToken(c.req.raw, c.env));

app.get("/oauth/tiktok", async (c) =>
  handleTikTokOAuth(c.req.raw, c.env, (path, body) =>
    containerInternal(c.env, path, body),
  ),
);

// Spark callback — Worker verifies secret, Python persists (Firestore).
app.all("/api/spark/callback", async (c) => handleSparkCallback(c.req.raw, c.env));

// Stable Instant Page URL. D1 redirect only — ad clicks must not boot the container.
app.get("/go", (c) => handleGo(c.req.raw, c.env));
app.get("/go/*", (c) => handleGo(c.req.raw, c.env));

async function resolveFirebaseUid(
  request: Request,
  env: Env,
): Promise<string | null> {
  const token = bearerFrom(request);
  if (!token) return null;
  try {
    return (await verifyFirebaseToken(token, env)).uid;
  } catch {
    if ((env.ALLOW_DEV_NO_AUTH || "").trim() === "1") {
      return (env.PROVISION_OWNER_UID || "dev").trim() || "dev";
    }
    return null;
  }
}

/**
 * Desktop provision worker protocol.
 * Validate PROVISION_WORKER_TOKEN on the Worker (secrets apply immediately),
 * then inject the internal key so a stale container without that secret still
 * accepts the request after edge auth.
 *
 * Heartbeats stay on R2 only — never wake Python just to mirror presence.
 * /worker/next returns empty at the edge when activity.hasWork === false.
 */
async function proxyProvisionWorker(
  request: Request,
  env: Env,
  requestId: string,
): Promise<Response> {
  const expected = (env.PROVISION_WORKER_TOKEN || "").trim();
  const got = (request.headers.get("x-provision-worker-token") || "").trim();
  if (!expected) {
    return Response.json(
      { detail: "edge: PROVISION_WORKER_TOKEN is not configured on the Worker" },
      { status: 503 },
    );
  }
  if (got !== expected) {
    return Response.json(
      {
        detail: "edge: Invalid provision worker token",
        hint: "local .env PROVISION_WORKER_TOKEN must match wrangler secret",
      },
      { status: 403 },
    );
  }

  const url = new URL(request.url);
  const isHeartbeat =
    request.method === "POST" &&
    /\/api\/provision\/worker\/heartbeat\/?$/.test(url.pathname);
  const isNext =
    request.method === "GET" &&
    /\/api\/provision\/worker\/next\/?$/.test(url.pathname);

  if (isHeartbeat) {
    const ownerUid = (
      url.searchParams.get("ownerUid") ||
      env.PROVISION_OWNER_UID ||
      ""
    ).trim();
    if (!ownerUid) {
      return Response.json({ detail: "ownerUid required" }, { status: 400 });
    }
    const rawText = await request.text();
    let body: { online?: boolean; meta?: Record<string, unknown> } = {};
    try {
      body = rawText ? (JSON.parse(rawText) as typeof body) : {};
    } catch {
      body = {};
    }
    const presence = await writeWorkerPresence(env, ownerUid, body);
    // Do NOT proxyToContainer here — that reset sleepAfter and billed memory
    // on every desktop heartbeat even with zero queued work.
    return Response.json({
      ok: true,
      ownerUid,
      serverTime: Date.now() / 1000,
      online: presence.online,
      edge: true,
      adspowerHealthy: presence.adspowerHealthy,
    });
  }

  if (isNext) {
    const ownerUid = (
      url.searchParams.get("ownerUid") ||
      env.PROVISION_OWNER_UID ||
      ""
    ).trim();
    if (!ownerUid) {
      return Response.json({ detail: "ownerUid required" }, { status: 400 });
    }
    const activity = await readActivity(env, ownerUid);
    // Missing activity is idle. Only an explicit hasWork flag may boot Python.
    if (!hasActiveWork(activity)) {
      return Response.json({
        ok: true,
        ownerUid,
        step: null,
        edge: true,
        idle: true,
      });
    }
  }

  const extra: Record<string, string> = {
    "x-provision-worker-edge-ok": "1",
  };
  const internal = (env.INTERNAL_API_KEY || "").trim();
  if (internal) extra["x-internal-api-key"] = internal;
  return proxyToContainer(request, env, { requestId, extraHeaders: extra });
}

/** BCs chip — edge read so AdsPower/online updates without waking Python. */
app.get("/api/provision/worker-status", async (c) => {
  const token = bearerFrom(c.req.raw);
  if (!token) {
    return c.json({ detail: "Not authenticated" }, 401);
  }
  let uid = "";
  try {
    const auth = await verifyFirebaseToken(token, c.env);
    uid = auth.uid;
  } catch {
    if ((c.env.ALLOW_DEV_NO_AUTH || "").trim() === "1") {
      uid = (c.env.PROVISION_OWNER_UID || "dev").trim() || "dev";
    } else {
      return c.json({ detail: "Invalid token" }, 401);
    }
  }
  const presence = await readWorkerPresence(c.env, uid);
  if (presence) {
    return c.json({
      online: presence.online,
      lastSeenAt: presence.lastSeenAt,
      meta: presence.meta,
      hostId: presence.hostId,
      hostName: presence.hostName,
      adspowerHealthy: presence.adspowerHealthy,
      edge: true,
    });
  }
  // No edge presence yet — do not wake Python; chip stays offline until heartbeat.
  return c.json({
    online: false,
    lastSeenAt: null,
    meta: {},
    hostId: null,
    hostName: null,
    adspowerHealthy: null,
    edge: true,
  });
});

app.all("/api/provision/worker", async (c) =>
  proxyProvisionWorker(c.req.raw, c.env, c.get("requestId")),
);
app.all("/api/provision/worker/", async (c) =>
  proxyProvisionWorker(c.req.raw, c.env, c.get("requestId")),
);
app.all("/api/provision/worker/:path{.+}", async (c) =>
  proxyProvisionWorker(c.req.raw, c.env, c.get("requestId")),
);

/** Idle inventory / job polls — R2 snapshots; wake only while hasWork. */
app.get("/api/provision/inventory", async (c) => {
  const uid = await resolveFirebaseUid(c.req.raw, c.env);
  if (!uid) return c.json({ detail: "Not authenticated" }, 401);
  const activity = await readActivity(c.env, uid);
  if (hasActiveWork(activity)) {
    return proxyToContainer(c.req.raw, c.env, { requestId: c.get("requestId") });
  }
  const snap = await readInventorySnapshot(c.env, uid);
  if (snap) {
    return c.json({ ...snap, edge: true });
  }
  return c.json({
    profiles: [],
    generation: 0,
    edge: true,
    stale: true,
  });
});

app.get("/api/provision/inventory/wait", async (c) => {
  const uid = await resolveFirebaseUid(c.req.raw, c.env);
  if (!uid) return c.json({ detail: "Not authenticated" }, 401);
  const activity = await readActivity(c.env, uid);
  if (hasActiveWork(activity)) {
    return proxyToContainer(c.req.raw, c.env, { requestId: c.get("requestId") });
  }
  const url = new URL(c.req.url);
  const since = Number(url.searchParams.get("since") || 0) || 0;
  const timeout = Number(url.searchParams.get("timeout") || 20) || 20;
  const result = await waitInventorySnapshot(c.env, uid, since, timeout);
  return c.json(result);
});

app.get("/api/provision/jobs", async (c) => {
  const uid = await resolveFirebaseUid(c.req.raw, c.env);
  if (!uid) return c.json({ detail: "Not authenticated" }, 401);
  const activity = await readActivity(c.env, uid);
  if (hasActiveWork(activity)) {
    return proxyToContainer(c.req.raw, c.env, { requestId: c.get("requestId") });
  }
  const snap = await readProvisionJobsSnapshot(c.env, uid);
  if (snap) {
    return c.json({ ...snap, edge: true });
  }
  return c.json({ jobs: [], active: [], edge: true, stale: true });
});

app.get("/api/jobs/active", async (c) => {
  const uid = await resolveFirebaseUid(c.req.raw, c.env);
  if (!uid) return c.json({ detail: "Not authenticated" }, 401);
  const activity = await readActivity(c.env, uid);
  if (hasActiveWork(activity)) {
    return proxyToContainer(c.req.raw, c.env, { requestId: c.get("requestId") });
  }
  const snap = await readJobsActiveSnapshot(c.env, uid);
  if (snap) {
    return c.json({ ...snap, edge: true });
  }
  return c.json({
    batches: [],
    uploads: [],
    facebook: [],
    provisions: [],
    logins: [],
    hooks: [],
    edge: true,
    stale: true,
  });
});

// Official MCP (Streamable HTTP). Feature-flagged.
app.all("/mcp", async (c) => {
  if ((c.env.MCP_ENABLED || "1").trim() === "0") {
    return c.json({ detail: "MCP disabled" }, 503);
  }
  try {
    const auth = await resolveMcpAuth(c.req.raw, c.env);
    return handleMcpRequest(c.req.raw, c.env, auth);
  } catch (exc) {
    if (exc instanceof HttpError) {
      return c.json({ detail: exc.message }, exc.status as 401);
    }
    return c.json({ detail: String(exc) }, 500);
  }
});

app.all("/mcp/*", async (c) => {
  // Allow /mcp/… aliases used by some clients.
  const url = new URL(c.req.url);
  url.pathname = "/mcp";
  const rewritten = new Request(url.toString(), c.req.raw);
  if ((c.env.MCP_ENABLED || "1").trim() === "0") {
    return c.json({ detail: "MCP disabled" }, 503);
  }
  try {
    const auth = await resolveMcpAuth(rewritten, c.env);
    return handleMcpRequest(rewritten, c.env, auth);
  } catch (exc) {
    if (exc instanceof HttpError) {
      return c.json({ detail: exc.message }, exc.status as 401);
    }
    return c.json({ detail: String(exc) }, 500);
  }
});

/** Inject edge R2 worker presence so Python start/continue ignore stale KV. */
async function proxyWithEdgeWorkerPresence(
  request: Request,
  env: Env,
  requestId: string,
): Promise<Response> {
  const extra: Record<string, string> = {};
  const token = bearerFrom(request);
  if (token) {
    let uid = "";
    try {
      uid = (await verifyFirebaseToken(token, env)).uid;
    } catch {
      uid = "";
    }
    const ownerUid = (env.PROVISION_OWNER_UID || "").trim() || uid;
    const presence = uid ? await resolvePresence(env, uid) : null;
    if (presence) {
      Object.assign(extra, presenceProxyHeaders(presence));
      // Start/continue wake the container anyway — sync KV so gates see AdsPower up.
      if (presence.online && presence.adspowerHealthy === true && ownerUid) {
        const workerTok = (env.PROVISION_WORKER_TOKEN || "").trim();
        const internal = (env.INTERNAL_API_KEY || "").trim();
        if (workerTok) {
          const hbHeaders: Record<string, string> = {
            "content-type": "application/json",
            "x-provision-worker-token": workerTok,
            "x-provision-worker-edge-ok": "1",
          };
          if (internal) hbHeaders["x-internal-api-key"] = internal;
          const hbUrl =
            `https://container/api/provision/worker/heartbeat` +
            `?ownerUid=${encodeURIComponent(ownerUid)}`;
          try {
            await proxyToContainer(
              new Request(hbUrl, {
                method: "POST",
                headers: hbHeaders,
                body: JSON.stringify({
                  online: true,
                  meta: presence.meta || { adspowerHealthy: true },
                }),
              }),
              env,
              { requestId: `${requestId}-hb-sync` },
            );
          } catch (err) {
            console.error("edge→container heartbeat sync failed:", err);
          }
        }
      }
    }
  }
  return proxyToContainer(request, env, { requestId, extraHeaders: extra });
}

app.post("/api/provision/start", async (c) =>
  proxyWithEdgeWorkerPresence(c.req.raw, c.env, c.get("requestId")),
);
app.post("/api/provision/profiles/:profileId/resume", async (c) =>
  proxyWithEdgeWorkerPresence(c.req.raw, c.env, c.get("requestId")),
);

async function savedSparksPage(c: {
  req: { raw: Request };
  env: Env;
  json: (body: unknown, status?: number) => Response;
}): Promise<Response> {
  const uid = await resolveFirebaseUid(c.req.raw, c.env);
  if (!uid) return c.json({ detail: "Missing authentication token" }, 401);
  const row = await c.env.DB.prepare(
    "SELECT body_json FROM documents WHERE uid=? AND collection=? AND doc_id=? AND deleted_at IS NULL",
  )
    .bind(uid, "kv", "sparks_page")
    .first<{ body_json: string | null }>();
  if (row?.body_json) {
    try {
      const saved = JSON.parse(row.body_json) as Record<string, unknown>;
      if (saved && typeof saved === "object") return c.json({ ...saved, edge: true });
    } catch {
      /* empty edge payload below */
    }
  }
  return c.json({ ok: true, sparks: [], offers: [], edge: true, stale: true });
}

// Saved Sparks page. Opening it reads D1. Refresh must not replace this list
// with the container copy: that copy drops the uploaded carousels and stores
// bare tiktok.com/video links that 404.
app.get("/api/provision/sparks", (c) => savedSparksPage(c));
app.post("/api/provision/sparks/refresh", (c) => savedSparksPage(c));

function publicOperations(raw: Record<string, unknown>): Record<string, unknown> {
  const tasks = Array.isArray(raw.tasks) ? raw.tasks : [];
  return {
    ...raw,
    tasks: tasks.map((task) => {
      if (!task || typeof task !== "object") return task;
      const copy = { ...(task as Record<string, unknown>) };
      delete copy.token;
      return copy;
    }),
  };
}

function operationsAreActive(raw: Record<string, unknown>): boolean {
  const workflows = Array.isArray(raw.workflows) ? raw.workflows : [];
  return workflows.some((row) => {
    const state = String((row as { state?: string }).state || "");
    return state === "active" || state === "paused";
  });
}

/** Dock poll. Coordinator snapshot at the edge unless a workflow is actually running. */
app.get("/api/operations", async (c) => {
  const uid = await resolveFirebaseUid(c.req.raw, c.env);
  if (!uid) return c.json({ detail: "Not authenticated" }, 401);
  const coordinator = c.env.WORK_COORDINATOR;
  if ((c.env.ORCHESTRATION_ENABLED || "").trim() !== "1" || !coordinator) {
    return c.json({ enabled: false, workflows: [], tasks: [], edge: true });
  }
  try {
    const stub = coordinator.get(coordinator.idFromName("installation-v1"));
    const res = await stub.fetch(new Request("https://coordinator/snapshot", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenant: uid }),
    }));
    if (!res.ok) return c.json({ detail: "Central scheduler unavailable" }, 503);
    const raw = (await res.json()) as Record<string, unknown>;
    if (operationsAreActive(raw)) {
      return proxyToContainer(c.req.raw, c.env, { requestId: c.get("requestId") });
    }
    return c.json({ enabled: true, ...publicOperations(raw), edge: true });
  } catch (err) {
    console.error("operations snapshot failed", err);
    return c.json({ detail: "Central scheduler unavailable" }, 503);
  }
});

/** Real application routes. Scanners and unknown /api paths stay on the Worker. */
const API_ROOTS = new Set([
  "images", "tiktok", "batch", "oauth", "meta", "facebook", "sources",
  "pinterest", "library", "preview", "jobs", "catalog", "catalog.csv",
  "outputs", "output", "operations", "spark", "cookies", "plans",
  "workspace", "hooks", "mcp", "provision", "chat", "settings", "config",
  "public", "postback", "track", "auth-config",
]);

app.all("/api/*", async (c) => {
  const parts = new URL(c.req.url).pathname.split("/").filter(Boolean);
  const root = (parts[1] || "").toLowerCase();
  if (parts[0] !== "api" || !API_ROOTS.has(root)) {
    return c.json({ detail: "Not found" }, 404);
  }
  return proxyToContainer(c.req.raw, c.env, { requestId: c.get("requestId") });
});

// Static assets from Workers Assets. A miss is a 404, not a container start.
app.all("*", async (c) => {
  const path = new URL(c.req.url).pathname;
  if (c.env.ASSETS && !path.startsWith("/api") && !path.startsWith("/mcp") && !path.startsWith("/_internal")) {
    const assetRes = await c.env.ASSETS.fetch(c.req.raw);
    if (assetRes.status !== 404) {
      const ct = (assetRes.headers.get("content-type") || "").toLowerCase();
      if (ct.includes("text/html")) {
        const headers = new Headers(assetRes.headers);
        headers.set("Cache-Control", "no-cache, must-revalidate");
        return new Response(assetRes.body, {
          status: assetRes.status,
          statusText: assetRes.statusText,
          headers,
        });
      }
      return assetRes;
    }
    return new Response(assetRes.body, {
      status: 404,
      statusText: assetRes.statusText,
      headers: assetRes.headers,
    });
  }
  return c.json({ detail: "Not found" }, 404);
});

export default {
  fetch: app.fetch,

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(
      retainRemoteMedia(env).then((result) => {
        console.log("retention cron removed", result.removed);
      }).catch((err) => {
        console.error("retention cron failed", err);
      }),
    );
  },
} satisfies ExportedHandler<Env>;
