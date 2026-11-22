/** Durable scheduling state. All methods run inside one storage transaction.
 * No provider calls, process-local locks, or wall-clock sleeps belong here.
 * This SQL surface is deliberately small so tests use real SQLite as well.
 */
export interface Sql {
  exec(query: string, ...bindings: (string | number | null)[]): Iterable<Record<string, unknown>>;
}
export type Demand = { key: string; units: number };
export type Task = {
  id: string; kind: string; input: Record<string, unknown>; after?: string[];
  resources?: Demand[]; notBefore?: number; priority?: number;
  maxAttempts?: number; effect?: "read" | "mutation";
};
type Row = Record<string, any>;
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const MAX_TASKS = 500;
const LEASE_MS = 90_000;
const json = (v: unknown) => JSON.stringify(v);
function canonical(v: any): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map(k => `${json(k)}:${canonical(v[k])}`).join(",")}}`;
  return json(v);
}
function name(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim() || value.length > 256) throw new Error(`Invalid ${label}`);
  return value;
}
function integer(value: unknown, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) throw new Error("Invalid integer");
  return value;
}
export class SchedulerStore {
  constructor(private sql: Sql, private now: () => number = Date.now, private uuid: () => string = () => crypto.randomUUID()) {}
  rows(q: string, ...args: (string | number | null)[]): Row[] { return [...this.sql.exec(q, ...args)]; }
  one(q: string, ...args: (string | number | null)[]): Row | undefined { return this.rows(q, ...args)[0]; }
  init(): void {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS workflows (
      id TEXT PRIMARY KEY, tenant TEXT NOT NULL, request_key TEXT NOT NULL, spec TEXT NOT NULL,
      state TEXT NOT NULL, created INTEGER NOT NULL, dispatched INTEGER NOT NULL DEFAULT 0,
      UNIQUE(tenant,request_key))`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS tasks (
      id TEXT NOT NULL, workflow TEXT NOT NULL, kind TEXT NOT NULL, input TEXT NOT NULL,
      deps TEXT NOT NULL, demands TEXT NOT NULL, state TEXT NOT NULL, due INTEGER NOT NULL,
      priority INTEGER NOT NULL, effect TEXT NOT NULL, max_attempts INTEGER NOT NULL,
      attempt INTEGER NOT NULL DEFAULT 0, worker TEXT, token TEXT, expires INTEGER,
      reason TEXT NOT NULL DEFAULT '', output TEXT, checkpoint TEXT, external_id TEXT,
      created INTEGER NOT NULL, PRIMARY KEY(workflow,id))`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(state,due,workflow)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS resources (
      key TEXT PRIMARY KEY, capacity INTEGER NOT NULL, due INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS grants (
      resource TEXT NOT NULL, workflow TEXT NOT NULL, task TEXT NOT NULL, token TEXT NOT NULL,
      units INTEGER NOT NULL, PRIMARY KEY(resource,workflow,task))`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS receipts (token TEXT PRIMARY KEY, tenant TEXT NOT NULL, response TEXT NOT NULL)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, workflow TEXT NOT NULL,
      task TEXT, kind TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL)`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS events_owner ON events(tenant,seq)`);
  }
  event(tenant: string, workflow: string, task: string | null, kind: string, body: unknown = {}): void {
    this.sql.exec("INSERT INTO events(tenant,workflow,task,kind,body,created) VALUES(?,?,?,?,?,?)", tenant, workflow, task, kind, json(body), this.now());
  }
  resource(key: string, capacity: number, enabled = true): void {
    name(key, "resource"); integer(capacity, 0, 1_000_000);
    this.sql.exec("INSERT INTO resources(key,capacity,enabled) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET capacity=excluded.capacity,enabled=excluded.enabled", key, capacity, enabled ? 1 : 0);
  }
  ensureResource(key: string, capacity: number): void {
    name(key, "resource"); integer(capacity, 0, 1_000_000);
    this.sql.exec("INSERT OR IGNORE INTO resources(key,capacity) VALUES(?,?)", key, capacity);
  }
  cooldown(key: string, until: number): void {
    integer(until, 0, Number.MAX_SAFE_INTEGER);
    if (!this.one("SELECT key FROM resources WHERE key=?", key)) throw new Error("Unknown resource");
    this.sql.exec("UPDATE resources SET due=MAX(due,?) WHERE key=?", until, key);
  }
  submit(tenant: string, requestKey: string, tasks: Task[]): Row {
    name(tenant, "tenant"); name(requestKey, "request key");
    if (!Array.isArray(tasks) || !tasks.length || tasks.length > MAX_TASKS) throw new Error("Workflow requires 1-500 tasks");
    if (json(tasks).length > 500_000) throw new Error("Workflow too large; use artifact references");
    const ids = new Set<string>();
    for (const t of tasks) {
      name(t.id, "task ID"); name(t.kind, "handler kind/version");
      if (ids.has(t.id)) throw new Error("Duplicate task ID"); ids.add(t.id);
      if (!t.input || typeof t.input !== "object" || Array.isArray(t.input)) throw new Error("Invalid task input");
      if (t.effect !== undefined && !["read", "mutation"].includes(t.effect)) throw new Error("Invalid effect");
      integer(t.notBefore ?? 0, 0, Number.MAX_SAFE_INTEGER);
      integer(t.priority ?? 0, 0, 100); integer(t.maxAttempts ?? 3, 1, 10);
      if (!Array.isArray(t.after ?? []) || !Array.isArray(t.resources ?? [])) throw new Error("Invalid dependencies/resources");
      const keys = new Set<string>();
      for (const d of t.resources ?? []) {
        name(d.key, "resource key"); integer(d.units, 1, 1_000_000);
        if (keys.has(d.key)) throw new Error("Duplicate resource demand"); keys.add(d.key);
      }
    }
    const graph = new Map(tasks.map(t => [t.id, t.after ?? []]));
    const done = new Set<string>(), visiting = new Set<string>();
    const visit = (id: string) => {
      if (!ids.has(id)) throw new Error("Unknown dependency");
      if (visiting.has(id)) throw new Error("Dependency cycle");
      if (done.has(id)) return;
      visiting.add(id); for (const dep of graph.get(id)!) visit(dep);
      visiting.delete(id); done.add(id);
    };
    tasks.forEach(t => visit(t.id));
    const spec = canonical(tasks);
    const old = this.one("SELECT * FROM workflows WHERE tenant=? AND request_key=?", tenant, requestKey);
    if (old) {
      if (old.spec !== spec) throw new Error("Request key already used with different input");
      return { id: old.id, existing: true };
    }
    const id = this.uuid(), now = this.now();
    this.sql.exec("INSERT INTO workflows(id,tenant,request_key,spec,state,created) VALUES(?,?,?,?,?,?)", id, tenant, requestKey, spec, "active", now);
    for (const t of tasks) this.sql.exec(`INSERT INTO tasks
      (id,workflow,kind,input,deps,demands,state,due,priority,effect,max_attempts,created)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`, t.id, id, t.kind, json(t.input), json(t.after ?? []), json(t.resources ?? []),
      (t.after?.length ? "blocked" : "ready"), t.notBefore ?? 0, t.priority ?? 0, t.effect ?? "mutation", t.maxAttempts ?? 3, now);
    this.event(tenant, id, null, "submitted", { count: tasks.length });
    return { id, existing: false };
  }
  workflow(tenant: string, id: string): Row {
    const row = this.one("SELECT * FROM workflows WHERE id=? AND tenant=?", id, tenant);
    if (!row) throw new Error("Unknown workflow");
    return row;
  }
  refresh(workflow: string): void {
    const w = this.one("SELECT * FROM workflows WHERE id=?", workflow)!;
    if (w.state === "cancelled") return;
    const tasks = this.rows("SELECT * FROM tasks WHERE workflow=?", workflow);
    const states = new Map(tasks.map(t => [t.id, t.state]));
    for (const t of tasks) if (t.state === "blocked") {
      const deps: string[] = JSON.parse(t.deps);
      if (deps.every(d => states.get(d) === "succeeded")) {
        this.sql.exec("UPDATE tasks SET state='ready',reason='' WHERE workflow=? AND id=?", workflow, t.id);
      } else {
        const failed = deps.some(d => ["failed", "cancelled"].includes(states.get(d)));
        this.sql.exec("UPDATE tasks SET reason=? WHERE workflow=? AND id=?", failed ? "dependency_failed" : "dependencies", workflow, t.id);
      }
    }
    const current = this.rows("SELECT state FROM tasks WHERE workflow=?", workflow).map(t => t.state);
    if (current.every(s => TERMINAL.has(s))) {
      const state = current.every(s => s === "succeeded") ? "succeeded" : "failed";
      this.sql.exec("UPDATE workflows SET state=? WHERE id=?", state, workflow);
    } else if (current.every(s => TERMINAL.has(s) || s === "blocked")) {
      // Failed dependencies leave explicit blocked children, not a running workflow forever.
      this.sql.exec("UPDATE workflows SET state='blocked' WHERE id=?", workflow);
    }
  }
  tick(): void {
    const now = this.now();
    const expired = this.rows("SELECT t.*,w.tenant,w.state AS workflow_state FROM tasks t JOIN workflows w ON w.id=t.workflow WHERE t.state='running' AND t.expires<=?", now);
    for (const t of expired) {
      if (t.effect === "mutation") {
        this.sql.exec("UPDATE tasks SET state='reconciling',reason='lease_expired_uncertain',expires=NULL WHERE workflow=? AND id=?", t.workflow, t.id);
        // Retain grants. Expiry cannot prove a remote task/browser action stopped.
      } else {
        const state = t.workflow_state === "cancelled" ? "cancelled" : t.attempt >= t.max_attempts ? "failed" : "ready";
        this.sql.exec("DELETE FROM grants WHERE workflow=? AND task=?", t.workflow, t.id);
        this.sql.exec("UPDATE tasks SET state=?,token=NULL,worker=NULL,expires=NULL,reason='lease_expired' WHERE workflow=? AND id=?", state, t.workflow, t.id);
      }
      this.event(t.tenant, t.workflow, t.id, "lease_expired"); this.refresh(t.workflow);
    }
  }
  claim(tenant: string, worker: string, kinds: string[], specific?: { workflow: string; task: string }, requestId?: string): Row {
    name(tenant, "tenant"); name(worker, "worker");
    if (!Array.isArray(kinds) || !kinds.length || kinds.length > 100) throw new Error("Invalid worker capabilities");
    kinds.forEach(k => name(k, "capability"));
    const receiptKey = requestId ? `claim:${worker}:${name(requestId, "claim request ID")}` : null;
    if (receiptKey) {
      const prior = this.one("SELECT response FROM receipts WHERE token=? AND tenant=?", receiptKey, tenant);
      if (prior) return JSON.parse(prior.response);
    }
    this.tick();
    // Workflow round robin, then priority + age. Examine blocked candidates too so
    // one resource-constrained task cannot hold up unrelated ready work.
    const tasks = this.rows(`SELECT t.*,w.tenant FROM tasks t JOIN workflows w ON w.id=t.workflow
      WHERE w.tenant=? AND w.state='active' AND t.state='ready' AND t.due<=?
      ORDER BY w.dispatched, (t.priority + (? - t.created)/60000) DESC,t.created,t.id`, tenant, this.now(), this.now());
    for (const t of tasks) {
      if (!kinds.includes(t.kind) || (specific && (t.workflow !== specific.workflow || t.id !== specific.task))) continue;
      const demands: Demand[] = JSON.parse(t.demands);
      let reason = "";
      for (const d of demands) {
        const resource = this.one("SELECT * FROM resources WHERE key=?", d.key);
        const used = Number(this.one("SELECT COALESCE(SUM(units),0) AS n FROM grants WHERE resource=?", d.key)!.n);
        if (!resource || !resource.enabled) { reason = `resource_unavailable:${d.key}`; break; }
        if (resource.due > this.now()) { reason = `cooldown:${d.key}:${resource.due}`; break; }
        if (used + d.units > resource.capacity) { reason = `resource_busy:${d.key}`; break; }
      }
      if (reason) {
        this.sql.exec("UPDATE tasks SET reason=? WHERE workflow=? AND id=?", reason, t.workflow, t.id); continue;
      }
      const token = this.uuid(), expires = this.now() + LEASE_MS;
      for (const d of demands) this.sql.exec("INSERT INTO grants(resource,workflow,task,token,units) VALUES(?,?,?,?,?)", d.key, t.workflow, t.id, token, d.units);
      this.sql.exec("UPDATE tasks SET state='running',worker=?,token=?,expires=?,attempt=attempt+1,reason='' WHERE workflow=? AND id=?", worker, token, expires, t.workflow, t.id);
      this.sql.exec("UPDATE workflows SET dispatched=(SELECT COALESCE(MAX(dispatched),0)+1 FROM workflows) WHERE id=?", t.workflow);
      this.event(tenant, t.workflow, t.id, "claimed", { worker, attempt: t.attempt + 1 });
      const response = { task: { id: t.id, workflow: t.workflow, kind: t.kind, input: JSON.parse(t.input), checkpoint: t.checkpoint ? JSON.parse(t.checkpoint) : null, token, expires, attempt: t.attempt + 1 } };
      if (receiptKey) this.sql.exec("INSERT INTO receipts(token,tenant,response) VALUES(?,?,?)", receiptKey, tenant, json(response));
      return response;
    }
    return { task: null };
  }
  owned(tenant: string, token: string, worker: string): Row {
    const t = this.one("SELECT t.*,w.tenant,w.state AS workflow_state FROM tasks t JOIN workflows w ON w.id=t.workflow WHERE t.token=? AND w.tenant=? AND t.worker=?", token, tenant, worker);
    if (!t || t.state !== "running" || t.expires <= this.now()) throw new Error("Lease lost");
    return t;
  }
  heartbeat(tenant: string, token: string, worker: string, checkpoint?: unknown): Row {
    const t = this.owned(tenant, token, worker);
    if (t.workflow_state === "cancelled") return { ok: false, cancel: true };
    const expires = this.now() + LEASE_MS;
    if (checkpoint !== undefined && json(checkpoint).length > 32_768) throw new Error("Checkpoint too large");
    this.sql.exec("UPDATE tasks SET expires=?,checkpoint=COALESCE(?,checkpoint) WHERE workflow=? AND id=?", expires, checkpoint === undefined ? null : json(checkpoint), t.workflow, t.id);
    return { ok: true, expires, paused: t.workflow_state === "paused" };
  }
  complete(tenant: string, token: string, worker: string, outcome: Row): Row {
    const receipt = this.one("SELECT response FROM receipts WHERE token=? AND tenant=?", token, tenant);
    if (receipt) {
      const saved = JSON.parse(receipt.response);
      if (saved.outcomeKey !== canonical(outcome)) throw new Error("Completion token already used with a different result");
      return { ok: saved.ok, state: saved.state };
    }
    const t = this.owned(tenant, token, worker);
    const allowed = ["success", "failed", "defer", "retry", "external", "ambiguous", "cancelled"];
    if (!allowed.includes(outcome.type)) throw new Error("Invalid outcome");
    if (json(outcome).length > 65_536) throw new Error("Result too large; store an artifact reference");
    const until = outcome.until ?? this.now(); integer(until, 0, Number.MAX_SAFE_INTEGER);
    if (outcome.type === "external") name(outcome.externalId, "external task ID");
    const cancelled = t.workflow_state === "cancelled";
    let state = ({ success: "succeeded", failed: "failed", defer: "ready", retry: "ready", external: "waiting_external", ambiguous: "reconciling", cancelled: "cancelled" } as Row)[outcome.type];
    if (outcome.type === "retry" && t.attempt >= t.max_attempts) state = "failed";
    if (cancelled && ["ready", "failed"].includes(state)) state = "cancelled";
    const retain = ["waiting_external", "reconciling"].includes(state);
    if (outcome.type === "external" && outcome.retainResources !== undefined) {
      if (!Array.isArray(outcome.retainResources)) throw new Error("Invalid retained resources");
      const demanded: string[] = JSON.parse(t.demands).map((d: Demand) => d.key);
      if (outcome.retainResources.some((key: string) => !demanded.includes(key))) throw new Error("Cannot retain an unowned resource");
      for (const key of demanded) if (!outcome.retainResources.includes(key)) {
        this.sql.exec("DELETE FROM grants WHERE workflow=? AND task=? AND resource=?", t.workflow, t.id, key);
      }
    }
    for (const c of outcome.cooldowns ?? []) {
      if (!JSON.parse(t.demands).some((d: Demand) => d.key === c.key)) throw new Error("Cooldown must reference a granted resource");
      this.cooldown(c.key, c.until);
    }
    this.sql.exec(`UPDATE tasks SET state=?,due=?,expires=NULL,reason=?,output=?,external_id=?,
      checkpoint=COALESCE(?,checkpoint),attempt=attempt-?,token=?,worker=? WHERE workflow=? AND id=?`,
      state, until, String(outcome.reason ?? "").slice(0, 1000), json(outcome.output ?? null), outcome.externalId ?? null,
      outcome.checkpoint === undefined ? null : json(outcome.checkpoint), outcome.type === "defer" ? 1 : 0,
      retain ? token : null, retain ? worker : null, t.workflow, t.id);
    if (!retain) this.sql.exec("DELETE FROM grants WHERE workflow=? AND task=?", t.workflow, t.id);
    this.event(tenant, t.workflow, t.id, "result", { state, output: outcome.output ?? null });
    this.refresh(t.workflow);
    const response = { ok: true, state };
    this.sql.exec("INSERT INTO receipts(token,tenant,response) VALUES(?,?,?)", token, tenant, json({ ...response, outcomeKey: canonical(outcome) }));
    return response;
  }
  reconcile(tenant: string, workflow: string, task: string, expectedToken: string, externalId: string | null, outcome: Row): Row {
    this.workflow(tenant, workflow);
    const t = this.one("SELECT * FROM tasks WHERE workflow=? AND id=?", workflow, task);
    if (!t || !["waiting_external", "reconciling"].includes(t.state) || t.token !== expectedToken || (t.external_id ?? null) !== externalId) throw new Error("Reconciliation identity mismatch");
    if (!["success", "failed", "cancelled"].includes(outcome.type) || !outcome.evidence) throw new Error("Verified terminal evidence required");
    // Issue a fresh internal completion token so the original external-wait receipt
    // remains immutable, and an old callback cannot resolve a newer attempt.
    const token = this.uuid();
    this.sql.exec("UPDATE tasks SET state='running',token=?,expires=? WHERE workflow=? AND id=?", token, this.now() + LEASE_MS, workflow, task);
    return this.complete(tenant, token, t.worker, outcome);
  }
  control(tenant: string, workflow: string, action: string): Row {
    const w = this.workflow(tenant, workflow);
    if (!["pause", "resume", "cancel"].includes(action)) throw new Error("Invalid control");
    if (TERMINAL.has(w.state)) return { state: w.state };
    const state = action === "cancel" ? "cancelled" : action === "pause" ? "paused" : "active";
    this.sql.exec("UPDATE workflows SET state=? WHERE id=?", state, workflow);
    if (action === "cancel") this.sql.exec("UPDATE tasks SET state='cancelled',reason='workflow_cancelled' WHERE workflow=? AND state IN ('ready','blocked')", workflow);
    this.event(tenant, workflow, null, action);
    return { state };
  }
  snapshot(tenant: string, workflow?: string): Row {
    this.tick();
    if (workflow) this.workflow(tenant, workflow);
    const workflows = this.rows(`SELECT id,request_key,state,created FROM workflows WHERE tenant=? ${workflow ? "AND id=?" : ""} ORDER BY created DESC LIMIT 100`, ...([tenant, ...(workflow ? [workflow] : [])]));
    const tasks = workflows.flatMap(w => this.rows("SELECT id,workflow,kind,state,due,attempt,worker,token,reason,output,external_id,created FROM tasks WHERE workflow=?", w.id));
    return { workflows, tasks, now: this.now() };
  }
  events(tenant: string, after: number): Row {
    integer(after, 0, Number.MAX_SAFE_INTEGER);
    return { events: this.rows("SELECT * FROM events WHERE tenant=? AND seq>? ORDER BY seq LIMIT 200", tenant, after).map(r => ({ ...r, body: JSON.parse(r.body) })) };
  }
  nextAlarm(): number | null {
    const row = this.one(`SELECT MIN(at) AS at FROM (
      SELECT expires AS at FROM tasks WHERE state='running' AND expires IS NOT NULL
      UNION ALL SELECT due AS at FROM tasks t JOIN workflows w ON w.id=t.workflow
      WHERE t.state='ready' AND w.state='active' AND due>?)`, this.now());
    return row?.at == null ? null : Number(row.at);
  }
}
