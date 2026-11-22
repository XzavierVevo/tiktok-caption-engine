/**
 * Token exchange for MCP clients that prefer OAuth-style flows (e.g. ChatGPT).
 * POST /mcp/oauth/token  { "idToken": "<Firebase ID token>", "mode": "trusted"|"confirm", "label": "..." }
 * → { access_token: "ctk_...", token_type: "bearer", mode }
 *
 * Claude / OpenClaw can skip this and use a Settings-created key as Bearer.
 */
import type { Env } from "../env";
import { verifyFirebaseToken } from "./firebase";
import { containerInternal } from "../proxy";

export async function handleOAuthToken(
  request: Request,
  env: Env,
): Promise<Response> {
  if (request.method !== "POST") {
    return Response.json({ detail: "POST only" }, { status: 405 });
  }
  let body: { idToken?: string; mode?: string; label?: string } = {};
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return Response.json({ detail: "Invalid JSON" }, { status: 400 });
  }
  const idToken = (body.idToken || "").trim();
  if (!idToken) {
    return Response.json({ detail: "idToken required" }, { status: 400 });
  }
  try {
    const auth = await verifyFirebaseToken(idToken, env);
    const res = await containerInternal(env, "/internal/mcp/mint-key", {
      uid: auth.uid,
      mode: body.mode === "confirm" ? "confirm" : "trusted",
      label: body.label || "OAuth exchange",
    });
    if (!res.ok) {
      const detail = await res.text();
      return Response.json({ detail }, { status: res.status });
    }
    const data = (await res.json()) as { key?: string; mode?: string; id?: string };
    return Response.json({
      access_token: data.key,
      token_type: "bearer",
      mode: data.mode || "trusted",
      key_id: data.id,
    });
  } catch (exc) {
    return Response.json({ detail: String(exc) }, { status: 401 });
  }
}
