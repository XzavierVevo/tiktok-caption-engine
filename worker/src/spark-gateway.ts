/** Spark-only deployment: durable callbacks and phone leases, no app/container rollout. */
import { Hono } from "hono";
import type { Env } from "./env";
import { sparkPhoneLock } from "./state";

const app = new Hono<{ Bindings: Env }>();
function equal(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}
app.get("/healthz", c => c.json({ ok: true, service: "spark-gateway" }));
app.post("/_internal/state/spark-phone-lock", sparkPhoneLock);
app.use("/api/spark/*", async (c, next) => {
  const secret = c.env.INTERNAL_API_KEY || "";
  const token = c.req.header("x-internal-api-key") || c.req.query("token") || "";
  if (!secret || !token || !equal(secret, token)) return c.json({ detail: "forbidden" }, 403);
  await next();
});
const taskKey = (s: string) => /^[A-Za-z0-9._-]{1,160}$/.test(s);
app.post("/api/spark/callback", async c => {
  const task = c.req.query("task") || "";
  if (!taskKey(task)) return c.json({ detail: "task required" }, 400);
  const raw = await c.req.text();
  if (new TextEncoder().encode(raw).length > 16384) return c.json({ detail: "payload too large" }, 413);
  let body: Record<string, unknown>;
  try { body = JSON.parse(raw); } catch { return c.json({ detail: "JSON required" }, 400); }
  if (!body || typeof body !== "object" || Array.isArray(body)) return c.json({ detail: "object required" }, 400);
  const code = String(body.spark_code || body.sparkCode || "").trim();
  const postUrl = String(body.post_url || body.postUrl || "").trim();
  if (!/^#[A-Za-z0-9+/=_-]{20,512}$/.test(code) || !/^https:\/\/(www\.)?tiktok\.com\/@[^/]+\/(video|photo)\/\d+$/.test(postUrl)) {
    return c.json({ detail: "valid code and post URL required" }, 400);
  }
  const data = { sparkCode: code, postUrl, taskName: task, receivedAt: Date.now() / 1000, source: "spark_gateway" };
  const json = JSON.stringify(data);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(json));
  const hash = Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, "0")).join("");
  const result = await c.env.DB.prepare(
    "INSERT INTO documents(uid,collection,doc_id,body_json,body_sha256,version,updated_at,deleted_at,storage_rel) " +
    "VALUES('_system','spark_callbacks',?,?,?,1,?,NULL,NULL) " +
    "ON CONFLICT(uid,collection,doc_id) DO UPDATE SET body_json=excluded.body_json,body_sha256=excluded.body_sha256,version=documents.version+1,updated_at=excluded.updated_at " +
    "WHERE json_extract(documents.body_json,'$.sparkCode')=? AND json_extract(documents.body_json,'$.postUrl')=?",
  ).bind(task, json, hash, Date.now(), code, postUrl).run();
  if (!result.meta.changes) return c.json({ detail: "task result conflict" }, 409);
  return c.json({ ok: true, taskName: task });
});
app.get("/api/spark/result", async c => {
  const task = c.req.query("task") || "";
  if (!taskKey(task)) return c.json({ detail: "task required" }, 400);
  const row = await c.env.DB.prepare(
    "SELECT body_json FROM documents WHERE uid='_system' AND collection='spark_callbacks' AND doc_id=? AND deleted_at IS NULL",
  ).bind(task).first<{ body_json: string }>();
  return c.json({ ok: true, result: row ? JSON.parse(row.body_json) : null });
});
export default app;
