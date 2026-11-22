import type { Context } from "hono";
import type { Env } from "./env";

const MAX_BODY_BYTES = 900_000;
const MAX_BATCH = 100;

type StateInput = {
  uid?: string;
  collection?: string;
  docId?: string;
  body?: unknown;
  bodySha256?: string;
  expectedVersion?: number | null;
  storageRel?: string | null;
};

function safePart(value: unknown, max = 128): string | null {
  const text = String(value ?? "").trim();
  if (!text || text.length > max || !/^[A-Za-z0-9._~-]+$/.test(text)) return null;
  return text;
}

function constantTimeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const aa = enc.encode(a);
  const bb = enc.encode(b);
  let diff = aa.length ^ bb.length;
  const size = Math.max(aa.length, bb.length);
  for (let i = 0; i < size; i++) diff |= (aa[i % Math.max(aa.length, 1)] || 0) ^ (bb[i % Math.max(bb.length, 1)] || 0);
  return diff === 0;
}

export function authorized(c: { env: Env; req: { header: (name: string) => string | undefined } }): boolean {
  const expected = (c.env.INTERNAL_API_KEY || "").trim();
  const got = (c.req.header("x-internal-api-key") || "").trim();
  return Boolean(expected && got && constantTimeEqual(expected, got));
}

async function jsonInput(c: Context<{ Bindings: Env }>): Promise<StateInput> {
  const size = Number(c.req.header("content-length") || 0);
  if (size > MAX_BODY_BYTES) throw new Response("payload too large", { status: 413 });
  const raw = await c.req.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) throw new Response("payload too large", { status: 413 });
  try { return raw ? JSON.parse(raw) as StateInput : {}; }
  catch { throw new Response("invalid json", { status: 400 }); }
}

function key(input: StateInput): { uid: string; collection: string; docId: string } | null {
  const uid = safePart(input.uid, 256);
  const collection = safePart(input.collection);
  const docId = safePart(input.docId);
  return uid && collection && docId ? { uid, collection, docId } : null;
}

function fail(c: Context, error: unknown): Response {
  if (error instanceof Response) return error;
  console.error("D1 state operation failed", error instanceof Error ? error.message : String(error));
  return c.json({ detail: "state store unavailable" }, 503);
}

export async function stateHealth(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  try {
    const row = await c.env.DB.prepare("SELECT 1 AS ok").first<{ ok: number }>();
    return c.json({ ok: row?.ok === 1, backend: "d1", schema: 1 });
  } catch (error) { return fail(c, error); }
}

export async function stateGet(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  try {
    const input = await jsonInput(c); const k = key(input);
    if (!k) return c.json({ detail: "invalid key" }, 400);
    const row = await c.env.DB.prepare(
      "SELECT body_json,body_sha256,version,updated_at,deleted_at,storage_rel FROM documents WHERE uid=? AND collection=? AND doc_id=?",
    ).bind(k.uid, k.collection, k.docId).first<Record<string, unknown>>();
    if (!row || row.deleted_at != null) return c.json({ found: false });
    return c.json({ found: true, body: row.body_json ? JSON.parse(String(row.body_json)) : null,
      bodySha256: row.body_sha256, version: row.version, updatedAt: row.updated_at, storageRel: row.storage_rel });
  } catch (error) { return fail(c, error); }
}

export async function stateList(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  try {
    const input = await jsonInput(c);
    const uid = safePart(input.uid, 256); const collection = safePart(input.collection);
    if (!uid || !collection) return c.json({ detail: "invalid key" }, 400);
    const result = await c.env.DB.prepare(
      "SELECT doc_id,body_json,body_sha256,version,updated_at,storage_rel FROM documents WHERE uid=? AND collection=? AND deleted_at IS NULL ORDER BY updated_at,doc_id",
    ).bind(uid, collection).all<Record<string, unknown>>();
    return c.json({ rows: (result.results || []).map((row) => ({ docId: row.doc_id,
      body: row.body_json ? JSON.parse(String(row.body_json)) : null, bodySha256: row.body_sha256,
      version: row.version, updatedAt: row.updated_at, storageRel: row.storage_rel })) });
  } catch (error) { return fail(c, error); }
}

async function put(env: Env, input: StateInput): Promise<{ conflict?: boolean; version?: number; updatedAt?: number }> {
  const k = key(input); if (!k) throw new Response("invalid key", { status: 400 });
  const bodyJson = input.body == null ? null : JSON.stringify(input.body);
  if (bodyJson && new TextEncoder().encode(bodyJson).byteLength > 786_432) throw new Response("inline document too large", { status: 413 });
  const hash = String(input.bodySha256 || ""); if (!hash) throw new Response("bodySha256 required", { status: 400 });
  const now = Date.now(); const expected = input.expectedVersion;
  if (typeof expected === "number") {
    const res = await env.DB.prepare(
      "UPDATE documents SET body_json=?,body_sha256=?,storage_rel=?,version=version+1,updated_at=?,deleted_at=NULL WHERE uid=? AND collection=? AND doc_id=? AND version=? AND deleted_at IS NULL",
    ).bind(bodyJson, hash, input.storageRel || null, now, k.uid, k.collection, k.docId, expected).run();
    if (!res.meta.changes) return { conflict: true };
  } else {
    await env.DB.prepare(
      "INSERT INTO documents(uid,collection,doc_id,body_json,body_sha256,version,updated_at,deleted_at,storage_rel) VALUES(?,?,?,?,?,1,?,NULL,?) " +
      "ON CONFLICT(uid,collection,doc_id) DO UPDATE SET body_json=excluded.body_json,body_sha256=excluded.body_sha256,storage_rel=excluded.storage_rel,version=documents.version+1,updated_at=excluded.updated_at,deleted_at=NULL",
    ).bind(k.uid, k.collection, k.docId, bodyJson, hash, now, input.storageRel || null).run();
  }
  const row = await env.DB.prepare("SELECT version,updated_at FROM documents WHERE uid=? AND collection=? AND doc_id=?")
    .bind(k.uid, k.collection, k.docId).first<{ version: number; updated_at: number }>();
  return { version: row?.version, updatedAt: row?.updated_at };
}

export async function statePut(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  try { const result = await put(c.env, await jsonInput(c)); return result.conflict ? c.json({ detail: "version conflict" }, 409) : c.json({ ok: true, ...result }); }
  catch (error) { return fail(c, error); }
}

export async function stateDelete(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  try {
    const input = await jsonInput(c); const k = key(input); if (!k) return c.json({ detail: "invalid key" }, 400);
    const now = Date.now();
    await c.env.DB.prepare(
      "INSERT INTO documents(uid,collection,doc_id,body_json,body_sha256,version,updated_at,deleted_at,storage_rel) VALUES(?,?,?,NULL,'tombstone',1,?,?,NULL) " +
      "ON CONFLICT(uid,collection,doc_id) DO UPDATE SET body_json=NULL,body_sha256='tombstone',storage_rel=NULL,version=documents.version+1,updated_at=excluded.updated_at,deleted_at=excluded.deleted_at",
    ).bind(k.uid, k.collection, k.docId, now, now).run();
    return c.json({ ok: true, deletedAt: now });
  } catch (error) { return fail(c, error); }
}

export async function stateBatch(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  try {
    const raw = await c.req.json<{ items?: StateInput[] }>(); const items = raw.items || [];
    if (!Array.isArray(items) || items.length > MAX_BATCH) return c.json({ detail: "batch limit is 100" }, 400);
    const results = [];
    for (const item of items) results.push(await put(c.env, item));
    return c.json({ ok: true, results });
  } catch (error) { return fail(c, error); }
}

/** Atomic phone ownership. Uses server time; expiry only recovers crashed hosts. */
export async function sparkPhoneLock(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (!authorized(c)) return c.json({ detail: "forbidden" }, 403);
  try {
    const input = await c.req.json<{ phoneId?: string; owner?: string; action?: string }>();
    const phone = safePart(input.phoneId); const owner = safePart(input.owner);
    if (!phone || !owner) return c.json({ detail: "invalid phone lock" }, 400);
    const now = Date.now(); const expiresAt = now + 120_000;
    const body = JSON.stringify({ owner, expiresAt });
    let result;
    if (input.action === "acquire") {
      result = await c.env.DB.prepare(
        "INSERT INTO documents(uid,collection,doc_id,body_json,body_sha256,version,updated_at,deleted_at,storage_rel) " +
        "VALUES('_system','spark_phone_locks',?,?,'phone-lock',1,?,NULL,NULL) " +
        "ON CONFLICT(uid,collection,doc_id) DO UPDATE SET body_json=excluded.body_json,version=documents.version+1,updated_at=excluded.updated_at,deleted_at=NULL " +
        "WHERE documents.deleted_at IS NOT NULL OR json_extract(documents.body_json,'$.expiresAt')<=? OR json_extract(documents.body_json,'$.owner')=?",
      ).bind(phone, body, now, now, owner).run();
    } else if (input.action === "renew") {
      result = await c.env.DB.prepare(
        "UPDATE documents SET body_json=?,version=version+1,updated_at=? " +
        "WHERE uid='_system' AND collection='spark_phone_locks' AND doc_id=? AND deleted_at IS NULL " +
        "AND json_extract(body_json,'$.owner')=? AND json_extract(body_json,'$.expiresAt')>?",
      ).bind(body, now, phone, owner, now).run();
    } else if (input.action === "release") {
      result = await c.env.DB.prepare(
        "UPDATE documents SET body_json=?,version=version+1,updated_at=? " +
        "WHERE uid='_system' AND collection='spark_phone_locks' AND doc_id=? AND deleted_at IS NULL " +
        "AND json_extract(body_json,'$.owner')=?",
      ).bind(JSON.stringify({ owner: "", expiresAt: 0 }), now, phone, owner).run();
    } else {
      return c.json({ detail: "invalid lock action" }, 400);
    }
    return c.json({ ok: Boolean(result.meta.changes), expiresAt });
  } catch (error) { return fail(c, error); }
}
