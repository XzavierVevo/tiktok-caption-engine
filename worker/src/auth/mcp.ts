import type { Env } from "../env";
import type { AuthContext } from "./firebase";
import { bearerFrom, verifyFirebaseToken } from "./firebase";
import { verifyMcpKey } from "../mcp-keys";

/**
 * Resolve auth for MCP:
 * 1) MCP_DEV_KEY (local) → MCP_DEV_UID / "dev"
 * 2) Long-lived MCP API key verified from D1 (no container)
 * 3) Firebase ID token
 */
export async function resolveMcpAuth(
  request: Request,
  env: Env,
): Promise<AuthContext> {
  const token = bearerFrom(request);
  if (!token) {
    throw new HttpError(401, "Missing Authorization Bearer token or MCP API key");
  }

  const devKey = (env.MCP_DEV_KEY || "").trim();
  if (devKey && token === devKey) {
    return {
      uid: (env.MCP_DEV_UID || "dev").trim() || "dev",
      mode: "dev",
      mcpMode: "trusted",
      keyId: "dev",
    };
  }

  // Prefer MCP API keys (ctk_...) over Firebase JWTs. D1 only — do not boot Python.
  if (token.startsWith("ctk_")) {
    const hit = await verifyMcpKey(env, token);
    if (!hit) throw new HttpError(401, "Invalid MCP API key");
    return hit;
  }
  if (token.length === 64 && !token.includes(".")) {
    throw new HttpError(401, "Invalid MCP API key");
  }

  try {
    const fb = await verifyFirebaseToken(token, env);
    return { ...fb, mcpMode: "confirm" };
  } catch (exc) {
    throw new HttpError(401, `Invalid authentication: ${String(exc)}`);
  }
}

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}
