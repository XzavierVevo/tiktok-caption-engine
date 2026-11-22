import { getContainer } from "@cloudflare/containers";
import type { Env } from "./env";

export function requestIdFrom(request: Request): string {
  return (
    request.headers.get("x-request-id") ||
    request.headers.get("cf-ray") ||
    crypto.randomUUID()
  );
}

export type ProxyOpts = {
  requestId?: string;
  extraHeaders?: Record<string, string>;
};

/** Proxy any request to the singleton Python container, injecting correlation id. */
export async function proxyToContainer(
  request: Request,
  env: Env,
  opts: ProxyOpts = {},
): Promise<Response> {
  const rid = opts.requestId || requestIdFrom(request);
  const headers = new Headers(request.headers);
  headers.set("x-request-id", rid);
  if (opts.extraHeaders) {
    for (const [k, v] of Object.entries(opts.extraHeaders)) {
      if (v) headers.set(k, v);
    }
  }
  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
    // @ts-expect-error duplex required for streaming bodies in Workers
    init.duplex = "half";
  }
  const url = new URL(request.url);
  const proxied = new Request(url.toString(), init);
  const retryClone = proxied.clone();
  const stub = getContainer(env.CAPTION_TOOL, "app");
  // CaptionToolContainer.ensureReady may destroy+restart a wedged instance (~3 min).
  // Keep this above that so the outer abort does not cancel recovery mid-start.
  const FETCH_TIMEOUT_MS = 210_000;

  // Rollouts leave the instance inactive while the DO still thinks it is
  // healthy. Explicitly start before fetch so /healthz and /inventory work.
  try {
    await stub.startAndWaitForPorts({
      ports: [8080],
      cancellationOptions: {
        instanceGetTimeoutMS: 90_000,
        portReadyTimeoutMS: 180_000,
        waitInterval: 500,
      },
    });
  } catch (err) {
    console.error("container startAndWaitForPorts failed:", err);
  }

  let res: Response;
  try {
    res = await stub.fetch(proxied, {
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("container fetch timed out/failed; kicking forceRestart", msg);
    try {
      await (
        stub as unknown as { forceRestart: () => Promise<unknown> }
      ).forceRestart();
    } catch (restartErr) {
      console.error("forceRestart failed:", restartErr);
    }
    return new Response(
      "Production API container was hung and is restarting. Wait ~1 minute, then retry Start warm-up.",
      {
        status: 503,
        headers: {
          "content-type": "text/plain; charset=utf-8",
          "x-request-id": rid,
        },
      },
    );
  }
  if (res.status === 500) {
    const body = await res.text();
    if (/not running|consider calling start|Error proxying/i.test(body)) {
      console.error("container fetch stale; retrying start", body.slice(0, 200));
      try {
        await stub.startAndWaitForPorts({
          ports: [8080],
          cancellationOptions: {
            instanceGetTimeoutMS: 90_000,
            portReadyTimeoutMS: 180_000,
            waitInterval: 500,
          },
        });
        res = await stub.fetch(retryClone, {
          signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return new Response(`Failed to start container: ${msg}`, {
          status: 503,
          headers: { "content-type": "text/plain; charset=utf-8", "x-request-id": rid },
        });
      }
    } else {
      res = new Response(body, {
        status: 500,
        statusText: res.statusText,
        headers: res.headers,
      });
    }
  }

  const out = new Headers(res.headers);
  out.set("x-request-id", rid);
  return new Response(res.body, {
    status: res.status,
    statusText: res.statusText,
    headers: out,
  });
}

/** Renew sleepAfter on an already-running container. Never starts one. */
export async function touchContainer(env: Env): Promise<{ ok: boolean }> {
  const stub = getContainer(env.CAPTION_TOOL, "app") as unknown as {
    touch: () => Promise<{ ok: boolean }>;
  };
  return stub.touch();
}

/** JSON POST to container internal routes (Worker → Python brain). */
export async function containerInternal(
  env: Env,
  path: string,
  body: unknown,
  opts: { requestId?: string; headers?: Record<string, string> } = {},
): Promise<Response> {
  const rid = opts.requestId || crypto.randomUUID();
  const key = env.INTERNAL_API_KEY || "";
  const headers: Record<string, string> = {
    "content-type": "application/json",
    "x-request-id": rid,
    ...(opts.headers || {}),
  };
  if (key) headers["x-internal-api-key"] = key;
  const req = new Request(`http://container${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body ?? {}),
  });
  return proxyToContainer(req, env, { requestId: rid });
}
