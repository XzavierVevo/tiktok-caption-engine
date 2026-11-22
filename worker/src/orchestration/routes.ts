import type { Context } from "hono";
import type { Env } from "../env";

export async function schedulerRoute(c: Context<{ Bindings: Env }>): Promise<Response> {
  const expected = c.env.INTERNAL_API_KEY?.trim();
  const received = c.req.header("x-internal-api-key")?.trim();
  if (!expected || !received) return c.json({ detail: "Forbidden" }, 403);
  // Fixed-length digest comparison also avoids leaking the secret length.
  const digest = async (s: string) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([digest(expected), digest(received)]);
  let diff = 0; for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  if (diff) return c.json({ detail: "Forbidden" }, 403);
  if (!c.env.WORK_COORDINATOR) return c.json({ detail: "Scheduler not deployed" }, 503);
  // One installation-wide authority includes shared physical resources across tenants.
  const stub = c.env.WORK_COORDINATOR.get(c.env.WORK_COORDINATOR.idFromName("installation-v1"));
  return stub.fetch(c.req.raw);
}
