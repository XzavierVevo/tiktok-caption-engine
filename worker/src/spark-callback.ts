import type { Env } from "./env";
import { containerInternal } from "./proxy";
import { requestIdFrom } from "./proxy";

/**
 * DuoPlus NET_REQUEST lands here. Persist via Python (Firestore-backed),
 * replacing ephemeral /tmp file store as the network of record.
 */
export async function handleSparkCallback(
  request: Request,
  env: Env,
): Promise<Response> {
  const rid = requestIdFrom(request);
  const url = new URL(request.url);
  const token = url.searchParams.get("token") || "";
  const task = url.searchParams.get("task") || "";
  const secret = (env.SPARK_CALLBACK_SECRET || "").trim();
  if (!secret) {
    if ((env.FIREBASE_PROJECT_ID || "").trim()) {
      return Response.json(
        { detail: "Spark callback secret is not configured" },
        { status: 503 },
      );
    }
  } else if (token !== secret) {
    return Response.json({ detail: "Invalid spark callback token" }, { status: 403 });
  }
  let body: unknown = null;
  const raw = await request.text();
  if (raw) {
    try {
      body = raw.trim().startsWith("{") ? JSON.parse(raw) : raw;
    } catch {
      body = raw;
    }
  }
  const res = await containerInternal(
    env,
    "/internal/spark/ingress",
    { taskName: task, body, token },
    { requestId: rid },
  );
  const data = await res.json().catch(() => ({ ok: false }));
  return Response.json(data, { status: res.status, headers: { "x-request-id": rid } });
}
