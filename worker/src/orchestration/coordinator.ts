import { DurableObject } from "cloudflare:workers";
import type { Env } from "../env";
import { SchedulerStore } from "./store";

/** Private RPC only; the front-door validates the installation service secret.
 * No network await occurs inside a scheduler transaction.
 */
export class WorkCoordinator extends DurableObject<Env> {
  private store: SchedulerStore;
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.store = new SchedulerStore(ctx.storage.sql);
    ctx.storage.transactionSync(() => this.store.init());
  }
  private async arm(): Promise<void> {
    const at = this.store.nextAlarm();
    if (at !== null) await this.ctx.storage.setAlarm(Math.max(Date.now() + 1000, at));
    else await this.ctx.storage.deleteAlarm();
  }
  async alarm(): Promise<void> {
    this.ctx.storage.transactionSync(() => this.store.tick());
    await this.arm();
  }
  async fetch(request: Request): Promise<Response> {
    if (request.method !== "POST") return Response.json({ detail: "POST required" }, { status: 405 });
    try {
      const raw = await request.text();
      if (raw.length > 600_000) return Response.json({ detail: "Request too large" }, { status: 413 });
      const p = JSON.parse(raw);
      const op = new URL(request.url).pathname.split("/").pop();
      const result = this.ctx.storage.transactionSync(() => {
        switch (op) {
          case "submit": return this.store.submit(p.tenant, p.requestKey, p.tasks);
          case "claim": return this.store.claim(p.tenant, p.worker, p.kinds, p.specific, p.requestId);
          case "heartbeat": return this.store.heartbeat(p.tenant, p.token, p.worker, p.checkpoint);
          case "complete": return this.store.complete(p.tenant, p.token, p.worker, p.outcome);
          case "reconcile": return this.store.reconcile(p.tenant, p.workflow, p.task, p.token, p.externalId ?? null, p.outcome);
          case "control": return this.store.control(p.tenant, p.workflow, p.action);
          case "snapshot": return this.store.snapshot(p.tenant, p.workflow);
          case "events": return this.store.events(p.tenant, p.after ?? 0);
          case "resource": this.store.resource(p.key, p.capacity, p.enabled ?? true); return { ok: true };
          case "ensure-resource": this.store.ensureResource(p.key, p.capacity); return { ok: true };
          case "cooldown": this.store.cooldown(p.key, p.until); return { ok: true };
          case "health": return { ok: true, protocol: 1 };
          default: throw new Error("Unknown scheduler operation");
        }
      });
      await this.arm();
      return Response.json(result);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "Scheduler request failed";
      return Response.json({ detail }, { status: detail === "Lease lost" ? 409 : 400 });
    }
  }
}
