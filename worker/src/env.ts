import type { CaptionToolContainer } from "./container";
import type { WorkCoordinator } from "./orchestration/coordinator";

export interface Env {
  CAPTION_TOOL: DurableObjectNamespace<CaptionToolContainer>;
  WORK_COORDINATOR?: DurableObjectNamespace<WorkCoordinator>;
  ORCHESTRATION_ENABLED?: string;
  ORCHESTRATION_BASE_URL?: string;
  DATA: R2Bucket;
  DB: D1Database;
  ASSETS?: Fetcher;

  /** Feature flag — set "0" to disable MCP route (still proxies /api). */
  MCP_ENABLED?: string;
  INTERNAL_API_KEY?: string;

  FIREBASE_PROJECT_ID?: string;
  FIREBASE_API_KEY?: string;
  FIREBASE_APP_ID?: string;
  FIREBASE_AUTH_DOMAIN?: string;

  R2_ENDPOINT?: string;
  R2_ACCESS_KEY_ID?: string;
  R2_SECRET_ACCESS_KEY?: string;
  R2_BUCKET?: string;

  OPENAI_API_KEY?: string;
  OPENAI_MODEL?: string;
  ANTHROPIC_API_KEY?: string;
  /** Claude Code subscription token (`claude setup-token`). Not an API key. */
  CLAUDE_CODE_OAUTH_TOKEN?: string;
  CLAUDE_CLI_MODEL?: string;
  XAI_API_KEY?: string;
  GEMINI_API_KEY?: string;
  PERPLEXITY_API_KEY?: string;
  SCRAPE_CREATORS_API_KEY?: string;

  META_APP_ID?: string;
  META_APP_SECRET?: string;
  META_REDIRECT_URI?: string;
  META_GRAPH_VERSION?: string;

  TIKTOK_APP_ID?: string;
  TIKTOK_APP_SECRET?: string;
  TIKTOK_REDIRECT_URI?: string;

  DUOPLUS_API_KEY?: string;
  /** Alias forwarded as DUOPLUS_API_BASE for Python. */
  DUOPLUS_BASE_URL?: string;
  DUOPLUS_API_BASE?: string;
  SPARK_CALLBACK_SECRET?: string;
  SPARK_CALLBACK_BASE_URL?: string;
  PUBLIC_UI_ORIGIN?: string;
  HOOT_API_KEY?: string;
  WEBSHARE_API_KEY?: string;
  PROVISION_WORKER_TOKEN?: string;
  PROVISION_OWNER_UID?: string;

  /** Per-uid daily spark cap for MCP keys (default 20). */
  MCP_SPARK_DAY_CAP?: string;
  /** Dev MCP key → uid mapping when Firebase unset (local only). */
  MCP_DEV_KEY?: string;
  MCP_DEV_UID?: string;
  /** Override container TikTok HTTP signer (default "0" on Containers). */
  TIKTOK_SIGNER_ENABLED?: string;
  ALLOW_DEV_NO_AUTH?: string;
  USERSTORE_PRIMARY?: string;
  USERSTORE_DUAL_WRITE?: string;
  USERSTORE_SHADOW_READ?: string;
  CLOUDFLARE_STATE_BASE_URL?: string;
  DATA_ENCRYPTION_KEY_V1?: string;
  DATA_ENCRYPTION_ACTIVE_KID?: string;
  /** Days of R2 media to keep. Worker cron only; does not wake the container. */
  RETENTION_DAYS?: string;
}

/** Secrets forwarded into the Python container process. */
export function containerEnvVars(env: Env): Record<string, string> {
  const keys: (keyof Env)[] = [
    "FIREBASE_PROJECT_ID",
    "FIREBASE_API_KEY",
    "FIREBASE_APP_ID",
    "FIREBASE_AUTH_DOMAIN",
    "R2_ENDPOINT",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CLI_MODEL",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
    "PERPLEXITY_API_KEY",
    "SCRAPE_CREATORS_API_KEY",
    "META_APP_ID",
    "META_APP_SECRET",
    "META_REDIRECT_URI",
    "META_GRAPH_VERSION",
    "TIKTOK_APP_ID",
    "TIKTOK_APP_SECRET",
    "TIKTOK_REDIRECT_URI",
    "DUOPLUS_API_KEY",
    "DUOPLUS_BASE_URL",
    "DUOPLUS_API_BASE",
    "SPARK_CALLBACK_SECRET",
    "SPARK_CALLBACK_BASE_URL",
    "PUBLIC_UI_ORIGIN",
    "INTERNAL_API_KEY",
    "ORCHESTRATION_ENABLED",
    "ORCHESTRATION_BASE_URL",
    "HOOT_API_KEY",
    "WEBSHARE_API_KEY",
    "PROVISION_WORKER_TOKEN",
    "PROVISION_OWNER_UID",
    "USERSTORE_PRIMARY",
    "USERSTORE_DUAL_WRITE",
    "USERSTORE_SHADOW_READ",
    "CLOUDFLARE_STATE_BASE_URL",
    "DATA_ENCRYPTION_KEY_V1",
    "DATA_ENCRYPTION_ACTIVE_KID",
  ];
  const out: Record<string, string> = {};
  for (const k of keys) {
    const v = env[k];
    if (typeof v === "string" && v) out[k] = v;
  }
  // Container must accept internal MCP calls from this Worker.
  if (env.INTERNAL_API_KEY) out.INTERNAL_API_KEY = env.INTERNAL_API_KEY;
  const duoBase = env.DUOPLUS_API_BASE || env.DUOPLUS_BASE_URL;
  if (duoBase) out.DUOPLUS_API_BASE = duoBase;
  // AdsPower lives on a local host, not in the Cloudflare container.
  out.PROVISION_EMBED_WORKER = "0";
  out.CLOUDFLARE = "1";
  // Signer binds 8081 (not uvicorn 8080). Opt out with Worker secret "0".
  if (env.TIKTOK_SIGNER_ENABLED) {
    out.TIKTOK_SIGNER_ENABLED = env.TIKTOK_SIGNER_ENABLED;
  }
  out.ALLOW_DEV_NO_AUTH = env.ALLOW_DEV_NO_AUTH || "";
  if (!out.ALLOW_DEV_NO_AUTH) delete out.ALLOW_DEV_NO_AUTH;
  return out;
}
