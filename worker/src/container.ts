import { Container } from "@cloudflare/containers";
import type { Env } from "./env";
import { containerEnvVars } from "./env";

/**
 * Singleton FastAPI container (FFmpeg + Playwright + DuoPlus jobs).
 * Sleeps 30s after the last request. In-flight jobs renew that timer
 * through touch() and never through a health check or a restart.
 */
export class CaptionToolContainer extends Container<Env> {
  defaultPort = 8080;
  // Memory is billed until this idle window ends. Real work calls touch().
  sleepAfter = "30s";
  enableInternet = true;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx as never, env);
    this.envVars = containerEnvVars(env);
  }

  /**
   * Platform can report container.running while Python is wedged (health.healthy=0).
   * Probe /healthz; if it fails, destroy + restart so /api/provision/start is not
   * stuck forever on "Starting…".
   */
  override async fetch(request: Request): Promise<Response> {
    try {
      await this.ensureReady();
    } catch (err) {
      console.error("Container pre-start failed:", err);
    }
    return super.fetch(request);
  }

  /**
   * RPC from the Worker. Extends sleepAfter while Python still has a job.
   * Does not start, probe, or restart the instance.
   */
  async touch(): Promise<{ ok: boolean }> {
    if (this.ctx.container?.running !== true) return { ok: false };
    try {
      await this.renewActivityTimeout();
      return { ok: true };
    } catch (err) {
      console.error("container touch failed:", err);
      return { ok: false };
    }
  }

  /** RPC: force-kill and cold-start (ops recovery for hung Python). */
  async forceRestart(): Promise<{ ok: boolean; detail: string }> {
    try {
      if (this.ctx.container?.running) {
        await this.destroy();
      }
    } catch (err) {
      console.error("forceRestart destroy:", err);
    }
    try {
      await this.startAndWaitForPorts(this.startOpts());
      return { ok: true, detail: "restarted" };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return { ok: false, detail: msg };
    }
  }

  private startOpts() {
    return {
      ports: [this.defaultPort],
      cancellationOptions: {
        instanceGetTimeoutMS: 90_000,
        portReadyTimeoutMS: 180_000,
        waitInterval: 500,
      },
    };
  }

  private async ensureReady(): Promise<void> {
    const opts = this.startOpts();
    if (this.ctx.container?.running !== true) {
      await this.startAndWaitForPorts(opts);
      return;
    }
    if (await this.pingApp(4_000)) return;
    console.error(
      "Container running but /healthz failed; destroying and restarting",
    );
    try {
      await this.destroy();
    } catch (err) {
      console.error("Container destroy failed:", err);
    }
    await this.startAndWaitForPorts(opts);
  }

  private async pingApp(timeoutMs: number): Promise<boolean> {
    try {
      const container = this.ctx.container;
      if (!container?.running || this.defaultPort == null) return false;
      const tcp = container.getTcpPort(this.defaultPort);
      const res = await tcp.fetch("http://container/healthz", {
        method: "GET",
        signal: AbortSignal.timeout(timeoutMs),
      });
      return res.ok;
    } catch {
      return false;
    }
  }

  override onError(error: unknown) {
    // Log only — rethrowing leaves the DO stuck and blocks later starts.
    console.error("Container error:", error);
  }
}
