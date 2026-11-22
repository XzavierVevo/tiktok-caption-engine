// Test the production scheduling implementation against SQLite, not a mock SQL parser.
import { DatabaseSync } from 'node:sqlite';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import test from 'node:test';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const ts = require('../node_modules/typescript');
const code = ts.transpileModule(readFileSync(new URL('../src/orchestration/store.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const { SchedulerStore } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

function setup(path = ':memory:') {
  const db = new DatabaseSync(path);
  let now = 100000, seq = 0;
  const sql = { exec(q, ...args) { const s = db.prepare(q); return s.columns().length ? s.all(...args) : (s.run(...args), []); } };
  const store = new SchedulerStore(sql, () => now, () => `id-${++seq}`);
  store.init();
  const tx = fn => { db.exec('BEGIN IMMEDIATE'); try { const r = fn(); db.exec('COMMIT'); return r; } catch(e) { db.exec('ROLLBACK'); throw e; } };
  return { store, db, tx, advance: ms => now += ms };
}
const task = (id, resources = [], extra = {}) => ({ id, kind: 'test.v1', input: { id }, resources, ...extra });
const claim = (s, tenant = 'u', specific) => s.claim(tenant, 'host:boot', ['test.v1'], specific).task;
const finish = (s, t, outcome = { type: 'success' }, tenant = 'u') => s.complete(tenant, t.token, 'host:boot', outcome);

test('three profiles overlap, same profile exclusive, independent work progresses', () => {
  const { store: s } = setup();
  s.resource('host', 3);
  for (const p of ['a','b','c']) s.resource(p, 1);
  const tasks = ['a','b','c'].flatMap(p => [task(`${p}-login`, [{key:'host',units:1},{key:p,units:1}]), task(`${p}-setup`, [{key:'host',units:1},{key:p,units:1}], {after:[`${p}-login`]})]);
  s.submit('u','launch',tasks);
  const active = [claim(s),claim(s),claim(s)];
  assert.equal(active.filter(Boolean).length,3); assert.equal(claim(s),null);
  finish(s, active[0]); const next = claim(s); assert.match(next.id, /setup$/);
  assert.equal(next.id[0],active[0].id[0]);
});
test('atomic admission does not hold a partial resource set', () => {
  const {store:s}=setup(); s.resource('a',1); s.resource('b',0);
  s.submit('u','blocked',[task('a',[{key:'a',units:1},{key:'b',units:1}])]);
  s.submit('u','other',[task('b',[{key:'a',units:1}])]);
  assert.equal(claim(s).id,'b'); assert.equal(s.rows('SELECT * FROM grants').length,1);
});
test('persisted cooldown allows another account and wakes at due time', () => {
  const {store:s,advance}=setup(); s.resource('a',1);s.resource('b',1);s.cooldown('a',150000);
  s.submit('u','uploads',[task('a',[{key:'a',units:1}]),task('b',[{key:'b',units:1}])]);
  assert.equal(claim(s).id,'b');assert.equal(claim(s),null);advance(50000);assert.equal(claim(s).id,'a');
});
test('defer releases capacity and does not consume a retry', () => {
  const {store:s,advance}=setup();s.resource('phone',1);
  s.submit('u','spark',[task('a',[{key:'phone',units:1}]),task('b',[{key:'phone',units:1}])]);
  const a=claim(s); finish(s,a,{type:'defer',until:200000,checkpoint:{post:'p'}});
  assert.equal(claim(s).id,'b');advance(100000);
  assert.equal(s.one("SELECT attempt FROM tasks WHERE id='a'").attempt,0);
});
test('external tasks retain phone ownership after releasing execution', () => {
  const {store:s,advance}=setup();s.resource('phone',1);
  const w=s.submit('u','spark',[task('a',[{key:'phone',units:1}]),task('b',[{key:'phone',units:1}])]);
  const a=claim(s);finish(s,a,{type:'external',externalId:'rpa-1'});advance(1000000);s.tick();
  assert.equal(claim(s),null);
  assert.throws(()=>s.reconcile('u',w.id,'a',a.token,'other',{type:'success',evidence:'result'}));
  s.reconcile('u',w.id,'a',a.token,'rpa-1',{type:'success',evidence:'verified'});
  assert.equal(claim(s).id,'b');
});
test('expired mutation is quarantined, stale completion rejected', () => {
  const {store:s,advance}=setup();s.resource('account',1);
  s.submit('u','post',[task('a',[{key:'account',units:1}]),task('b',[{key:'account',units:1}])]);
  const a=claim(s);advance(90001);s.tick();assert.equal(claim(s),null);
  assert.throws(()=>finish(s,a),/Lease lost/);
  assert.equal(s.one("SELECT state FROM tasks WHERE id='a'").state,'reconciling');
});
test('expired read can retry with a new fenced token', () => {
  const {store:s,advance}=setup();s.submit('u','read',[task('a',[],{effect:'read'})]);
  const a=claim(s);advance(90001);const b=claim(s);assert.notEqual(a.token,b.token);
  assert.throws(()=>finish(s,a),/Lease lost/);finish(s,b);
});
test('submission and completion replay do not duplicate dependent work', () => {
  const {store:s}=setup();const tasks=[task('a'),task('b',[],{after:['a']})];
  const a=s.submit('u','key',tasks),b=s.submit('u','key',tasks);assert.equal(a.id,b.id);
  assert.throws(()=>s.submit('u','key',[task('different')]));
  const grant=claim(s);finish(s,grant);finish(s,grant);
  assert.equal(s.rows("SELECT * FROM events WHERE kind='result'").length,1);
  assert.equal(claim(s).id,'b');
});
test('cancel race preserves completed publish and suppresses descendants', () => {
  const {store:s}=setup();const w=s.submit('u','cancel',[task('post'),task('spark',[],{after:['post']})]);
  const a=claim(s);s.control('u',w.id,'cancel');finish(s,a,{type:'success',output:{postId:'published'}});
  assert.equal(s.workflow('u',w.id).state,'cancelled');assert.equal(claim(s),null);
  assert.equal(s.one("SELECT state FROM tasks WHERE id='post'").state,'succeeded');
});
test('tenant boundaries apply to grants, status and controls', () => {
  const {store:s}=setup();const w=s.submit('u','private',[task('a')]);const a=claim(s);
  assert.throws(()=>finish(s,a,{type:'success'},'attacker'));
  assert.throws(()=>s.control('attacker',w.id,'cancel'));assert.throws(()=>s.snapshot('attacker',w.id));
  assert.equal(s.snapshot('attacker').tasks.length,0);
});
test('pause prevents new claims and completion does not undo pause', () => {
  const {store:s}=setup();const w=s.submit('u','pause',[task('a'),task('b',[],{after:['a']})]);
  const a=claim(s);s.control('u',w.id,'pause');finish(s,a);assert.equal(claim(s),null);
  s.control('u',w.id,'resume');assert.equal(claim(s).id,'b');
});
test('dependency failure blocks only its descendants', () => {
  const {store:s}=setup();s.submit('u','failure',[task('a'),task('b',[],{after:['a']}),task('c')]);
  finish(s,claim(s),{type:'failed'});assert.equal(claim(s).id,'c');
  assert.equal(s.one("SELECT reason FROM tasks WHERE id='b'").reason,'dependency_failed');
});
test('malformed workflow rolls back; cycles rejected', () => {
  const {store:s,tx}=setup();
  assert.throws(()=>tx(()=>s.submit('u','bad',[task('a',[],{after:['b']}),task('b',[],{after:['a']})])));
  assert.equal(s.rows('SELECT * FROM workflows').length,0);
});
test('SQLite restart retains grants and checkpoints without a live Python process', () => {
  const dir=mkdtempSync(join(tmpdir(),'orchestration-'));
  try {
    const path=join(dir,'state.db');const first=setup(path);first.store.resource('phone',1);
    first.store.submit('u','restart',[task('a',[{key:'phone',units:1}]),task('b',[{key:'phone',units:1}])]);
    const a=claim(first.store);first.store.heartbeat('u',a.token,'host:boot',{externalId:'known'});first.db.close();
    const second=setup(path);assert.equal(claim(second.store),null);
    assert.equal(second.store.one("SELECT checkpoint FROM tasks WHERE id='a'").checkpoint,'{"externalId":"known"}');second.db.close();
  } finally {rmSync(dir,{recursive:true,force:true});}
});
