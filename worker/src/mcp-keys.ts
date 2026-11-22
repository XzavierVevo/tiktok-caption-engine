/** Verify ctk_ MCP keys from D1. Does not start the container. */

import type { Env } from "./env";
import type { AuthContext } from "./auth/firebase";
import { sha256Hex, verificationHash } from "./go-routing";

type KeyRow = {
  id?: string;
  hash?: string;
  mode?: string;
  revoked?: boolean;
  lastUsedAt?: number | null;
};

function xorEqual(a: string, b: string): boolean {
  const aa = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  let diff = aa.length ^ bb.length;
  const size = Math.max(aa.length, bb.length);
  for (let i = 0; i < size; i++) diff |= (aa[i] || 0) ^ (bb[i] || 0);
  return diff === 0;
}

function parseObject(raw: string | null | undefined): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const data = JSON.parse(raw) as unknown;
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;
    if ((data as { _enc?: string })._enc === "A256GCM") return null;
    return data as Record<string, unknown>;
  } catch {
    return null;
  }
}

function splitKey(token: string): { kid: string; secret: string } | null {
  const raw = token.trim();
  if (!raw.startsWith("ctk_")) return null;
  const rest = raw.slice(4);
  const cut = rest.indexOf("_");
  if (cut <= 0 || cut === rest.length - 1) return null;
  return { kid: rest.slice(0, cut), secret: rest.slice(cut + 1) };
}

async function readDoc(env: Env, uid: string, collection: string, docId: string): Promise<Record<string, unknown> | null> {
  const row = await env.DB.prepare(
    "SELECT body_json FROM documents WHERE uid=? AND collection=? AND doc_id=? AND deleted_at IS NULL",
  ).bind(uid, collection, docId).first<{ body_json: string | null }>();
  return parseObject(row?.body_json);
}

async function writeDoc(env: Env, uid: string, collection: string, docId: string, body: Record<string, unknown>): Promise<void> {
  const hash = await verificationHash(body);
  const now = Date.now();
  await env.DB.prepare(
    "INSERT INTO documents(uid,collection,doc_id,body_json,body_sha256,version,updated_at,deleted_at,storage_rel) VALUES(?,?,?,?,?,1,?,NULL,NULL) " +
      "ON CONFLICT(uid,collection,doc_id) DO UPDATE SET body_json=excluded.body_json,body_sha256=excluded.body_sha256,storage_rel=excluded.storage_rel,version=documents.version+1,updated_at=excluded.updated_at,deleted_at=NULL",
  ).bind(uid, collection, docId, JSON.stringify(body), hash, now).run();
}

/** Same lookup as app.mcp_keys.verify_key. */
export async function verifyMcpKey(env: Env, token: string): Promise<AuthContext | null> {
  const parts = splitKey(token);
  if (!parts) return null;
  const index = await readDoc(env, "_system", "mcp_key_index", parts.kid);
  const uid = String(index?.uid || "").trim();
  if (!uid) return null;
  const store = await readDoc(env, uid, "kv", "mcp_keys");
  const keys = Array.isArray(store?.keys) ? (store.keys as KeyRow[]) : [];
  const digest = await sha256Hex(parts.secret);
  for (const row of keys) {
    if (!row || row.id !== parts.kid) continue;
    if (row.revoked) return null;
    if (!xorEqual(String(row.hash || ""), digest)) return null;
    row.lastUsedAt = Date.now() / 1000;
    try {
      await writeDoc(env, uid, "kv", "mcp_keys", store || { keys });
    } catch (err) {
      console.error("mcp key lastUsedAt write failed", err);
    }
    return {
      uid,
      mode: "mcp",
      mcpMode: row.mode === "trusted" ? "trusted" : "confirm",
      keyId: parts.kid,
    };
  }
  return null;
}
