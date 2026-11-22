import type { Env } from "./env";
import { containerInternal } from "./proxy";

/**
 * Per-uid MCP quotas stored via Python userstore (durable).
 * Worker checks before expensive tools (spark / upload / render).
 */
const HEAVY = new Set([
  "fetch_spark",
  "start_render",
  "start_tiktok_upload",
  "start_carousel_upload",
  "create_custom_sound",
  "start_facebook_ads",
  "add_source_url",
  "add_source_urls",
]);

export async function assertQuota(
  env: Env,
  opts: {
    uid: string;
    tool: string;
    keyId: string;
    requestId: string;
  },
): Promise<void> {
  if (!HEAVY.has(opts.tool)) return;
  const sparkCap = Number(env.MCP_SPARK_DAY_CAP || "20") || 20;
  const res = await containerInternal(
    env,
    "/internal/mcp/quota-check",
    {
      uid: opts.uid,
      tool: opts.tool,
      keyId: opts.keyId,
      sparkDayCap: sparkCap,
    },
    { requestId: opts.requestId },
  );
  if (res.status === 429) {
    const body = await res.json().catch(() => ({}));
    const msg =
      (body as { detail?: string }).detail ||
      (body as { error?: string }).error ||
      "Quota exceeded";
    throw new QuotaError(msg);
  }
  if (!res.ok) {
    console.warn("quota-check failed", res.status, opts.requestId);
  }
}

export class QuotaError extends Error {
  status = 429;
}
