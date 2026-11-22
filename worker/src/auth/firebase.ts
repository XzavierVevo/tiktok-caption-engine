/**
 * Firebase ID token verification via Google's JWKS (no Admin SDK).
 * Container still verifies tokens itself; Worker uses this for MCP/quotas gates.
 */
import * as jose from "jose";
import type { Env } from "../env";

const JWKS = jose.createRemoteJWKSet(
  new URL("https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"),
);

export type AuthContext = {
  uid: string;
  mode: "firebase" | "mcp" | "dev";
  /** trusted = skip confirm tools; confirm = require confirm_tool */
  mcpMode?: "trusted" | "confirm";
  keyId?: string;
};

export async function verifyFirebaseToken(
  token: string,
  env: Env,
): Promise<AuthContext> {
  const projectId = (env.FIREBASE_PROJECT_ID || "").trim();
  if (!projectId) {
    throw new Error("FIREBASE_PROJECT_ID not configured");
  }
  const { payload } = await jose.jwtVerify(token, JWKS, {
    issuer: `https://securetoken.google.com/${projectId}`,
    audience: projectId,
  });
  const uid = String(payload.sub || "");
  if (!uid) throw new Error("Token has no subject");
  return { uid, mode: "firebase" };
}

export function bearerFrom(request: Request): string {
  const h = request.headers.get("authorization") || "";
  if (h.toLowerCase().startsWith("bearer ")) return h.slice(7).trim();
  const url = new URL(request.url);
  return (url.searchParams.get("auth") || "").trim();
}
