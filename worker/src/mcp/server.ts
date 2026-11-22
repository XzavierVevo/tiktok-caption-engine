import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { z } from "zod";
import type { Env } from "../env";
import type { AuthContext } from "../auth/firebase";
import { MCP_INSTRUCTIONS } from "./instructions";
import { containerInternal } from "../proxy";
import { presenceProxyHeaders, resolvePresence } from "../worker-presence";
import { assertQuota, QuotaError } from "../quotas";
import toolDefs from "./tool-defs.json";

type ToolDef = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  confirm?: boolean;
};

const CONFIRM = new Set(
  (toolDefs as ToolDef[]).filter((t) => t.confirm).map((t) => t.name),
);

type Pending = { name: string; arguments: Record<string, unknown> };

export type McpSession = {
  auth: AuthContext;
  threadId: string;
  pendingConfirms: Map<string, Pending>;
};

async function loadSession(env: Env, auth: AuthContext): Promise<McpSession> {
  const session: McpSession = {
    auth,
    threadId: "",
    pendingConfirms: new Map(),
  };
  const res = await containerInternal(env, "/internal/mcp/session", {
    uid: auth.uid,
    keyId: auth.keyId || auth.mode,
    action: "get",
  });
  if (res.ok) {
    const data = (await res.json()) as {
      threadId?: string;
      pendingConfirms?: Record<string, Pending>;
    };
    session.threadId = String(data.threadId || "");
    for (const [k, v] of Object.entries(data.pendingConfirms || {})) {
      if (v && v.name) session.pendingConfirms.set(k, v);
    }
  }
  return session;
}

async function saveSession(env: Env, session: McpSession): Promise<void> {
  const pending: Record<string, Pending> = {};
  for (const [k, v] of session.pendingConfirms.entries()) pending[k] = v;
  await containerInternal(env, "/internal/mcp/session", {
    uid: session.auth.uid,
    keyId: session.auth.keyId || session.auth.mode,
    action: "set",
    threadId: session.threadId,
    pendingConfirms: pending,
  });
}

/** JSON Schema → Zod shape so MCP clients see required fields and types. */
function jsonTypeToZod(prop: Record<string, unknown>): z.ZodTypeAny {
  const desc = typeof prop.description === "string" ? prop.description : undefined;
  const apply = (t: z.ZodTypeAny) => (desc ? t.describe(desc) : t);
  const enums = prop.enum;
  if (Array.isArray(enums) && enums.length > 0 && enums.every((x) => typeof x === "string")) {
    return apply(z.enum(enums as [string, ...string[]]));
  }
  const typ = prop.type;
  if (typ === "string") return apply(z.string());
  if (typ === "integer") return apply(z.number().int());
  if (typ === "number") return apply(z.number());
  if (typ === "boolean") return apply(z.boolean());
  if (typ === "array") {
    const items = (prop.items || {}) as Record<string, unknown>;
    return apply(z.array(jsonTypeToZod(items)));
  }
  if (typ === "object") return apply(z.object(jsonSchemaToZodShape(prop)).passthrough());
  return apply(z.any());
}

function jsonSchemaToZodShape(schema: Record<string, unknown>): Record<string, z.ZodTypeAny> {
  const props = (schema.properties || {}) as Record<string, Record<string, unknown>>;
  const required = new Set(
    Array.isArray(schema.required) ? (schema.required as string[]) : [],
  );
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const [key, prop] of Object.entries(props)) {
    let t = jsonTypeToZod(prop || {});
    if (!required.has(key)) t = t.optional();
    shape[key] = t;
  }
  return shape;
}

function shapeFromSchema(schema: Record<string, unknown>): Record<string, z.ZodTypeAny> {
  const shape = jsonSchemaToZodShape(schema || { type: "object", properties: {} });
  if (!shape.threadId) shape.threadId = z.string().optional();
  if (!shape.idempotencyKey) shape.idempotencyKey = z.string().optional();
  return shape;
}

export async function handleMcpRequest(
  request: Request,
  env: Env,
  auth: AuthContext,
  sessionHint?: McpSession,
): Promise<Response> {
  // initialize and tools/list must not load the Python session.
  const session: McpSession = sessionHint || {
    auth,
    threadId: "",
    pendingConfirms: new Map(),
  };
  let hydrated = Boolean(sessionHint);
  const ensureSession = async (): Promise<McpSession> => {
    if (hydrated) return session;
    const loaded = await loadSession(env, auth);
    session.threadId = loaded.threadId;
    session.pendingConfirms = loaded.pendingConfirms;
    hydrated = true;
    return session;
  };
  const server = new McpServer(
    { name: "caption-tool", version: "1.0.0" },
    { instructions: MCP_INSTRUCTIONS },
  );

  for (const def of toolDefs as ToolDef[]) {
    const inputSchema = shapeFromSchema(def.inputSchema || { type: "object", properties: {} });
    server.registerTool(
      def.name,
      {
        description: def.description,
        inputSchema: inputSchema as never,
      },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (async (args: any) => {
        const live = await ensureSession();
        try {
          const result = await runTool(env, auth, live, def.name, args || {});
          await saveSession(env, live);
          return result;
        } catch (exc) {
          const msg = exc instanceof Error ? exc.message : String(exc);
          const status = (exc as { status?: number }).status;
          await saveSession(env, live).catch(() => {});
          return {
            content: [
              {
                type: "text" as const,
                text: JSON.stringify({ ok: false, error: msg, status }),
              },
            ],
            isError: true,
          };
        }
      }) as never,
    );
  }

  const transport = new WebStandardStreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  await server.connect(transport);
  return transport.handleRequest(request);
}

async function runTool(
  env: Env,
  auth: AuthContext,
  session: McpSession,
  name: string,
  args: Record<string, unknown>,
) {
  const requestId = crypto.randomUUID();

  if (name === "create_thread") {
    const res = await containerInternal(
      env,
      "/internal/mcp/threads",
      { uid: auth.uid, action: "create", title: args.title || "MCP" },
      { requestId },
    );
    const data = await res.json();
    if (res.ok && (data as { threadId?: string }).threadId) {
      session.threadId = String((data as { threadId: string }).threadId);
    }
    return textResult(data, { isError: !res.ok });
  }

  if (name === "list_threads") {
    const res = await containerInternal(
      env,
      "/internal/mcp/threads",
      { uid: auth.uid, action: "list", limit: args.limit || 20 },
      { requestId },
    );
    return textResult(await res.json(), { isError: !res.ok });
  }

  if (name === "set_active_thread") {
    const tid = String(args.threadId || "").trim();
    if (!tid) throw new Error("threadId required");
    session.threadId = tid;
    return textResult({ ok: true, threadId: tid });
  }

  if (name === "confirm_tool") {
    const cid = String(args.confirmId || "").trim();
    const pending = session.pendingConfirms.get(cid);
    if (!pending) throw new Error("Unknown or expired confirmId");
    session.pendingConfirms.delete(cid);
    return runTool(env, auth, session, pending.name, {
      ...pending.arguments,
      _confirmed: true,
    });
  }

  if (name === "get_spark_job") {
    const res = await containerInternal(
      env,
      "/internal/mcp/call",
      {
        uid: auth.uid,
        threadId: session.threadId,
        name: "get_spark_job",
        arguments: args,
      },
      { requestId },
    );
    const data = await res.json();
    return textResult(data, { isError: !res.ok });
  }

  const needsThread = new Set([
    "fetch_spark",
    "start_carousel_upload",
    "list_plans",
    "set_render_plan",
    "set_carousel_plan",
    "set_captions",
    "add_captions",
    "preview_plan",
  ]);
  if (needsThread.has(name) && !session.threadId) {
    const res = await containerInternal(
      env,
      "/internal/mcp/threads",
      { uid: auth.uid, action: "create", title: "MCP" },
      { requestId },
    );
    const data = (await res.json()) as { threadId?: string };
    if (data.threadId) session.threadId = data.threadId;
  }

  const threadId = String(args.threadId || session.threadId || "").trim();
  if (threadId) session.threadId = threadId;

  const mcpMode = auth.mcpMode || "confirm";
  if (CONFIRM.has(name) && mcpMode !== "trusted" && !args._confirmed) {
    const confirmId = crypto.randomUUID().slice(0, 12);
    const clean = { ...args };
    delete clean._confirmed;
    session.pendingConfirms.set(confirmId, { name, arguments: clean });
    return textResult({
      ok: false,
      requiresConfirm: true,
      confirmId,
      tool: name,
      summary: `Confirm running ${name}. Call confirm_tool with confirmId=${confirmId}.`,
      arguments: clean,
    });
  }

  try {
    await assertQuota(env, {
      uid: auth.uid,
      tool: name,
      keyId: auth.keyId || "",
      requestId,
    });
  } catch (exc) {
    if (exc instanceof QuotaError) throw exc;
  }

  const presence = await resolvePresence(env, auth.uid).catch(() => null);
  const res = await containerInternal(
    env,
    "/internal/mcp/call",
    {
      uid: auth.uid,
      threadId: session.threadId,
      name,
      arguments: args,
      idempotencyKey: args.idempotencyKey || null,
      mcpMode,
      keyId: auth.keyId || "",
    },
    { requestId, headers: presenceProxyHeaders(presence) },
  );
  const data = await res.json().catch(async () => ({
    ok: false,
    error: await res.text().catch(() => `HTTP ${res.status}`),
  }));
  return textResult(data, { isError: !res.ok });
}

function textResult(data: unknown, opts?: { isError?: boolean }) {
  return {
    content: [
      {
        type: "text" as const,
        text: typeof data === "string" ? data : JSON.stringify(data, null, 2),
      },
    ],
    ...(opts?.isError ? { isError: true as const } : {}),
  };
}
