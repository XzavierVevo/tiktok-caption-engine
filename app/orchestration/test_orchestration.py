from __future__ import annotations

import copy
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from app import provision_jobs as jobs
from app.provision import inventory as inv
from app.orchestration import provision_bridge as bridge
from app.orchestration.worker import Context, Registry
from app.orchestration.client import LeaseLost


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.store = {"byId": {}}
        self.lock = threading.Lock()
        def mutate(uid, key, fn):
            with self.lock:
                self.store = fn(copy.deepcopy(self.store))
                return copy.deepcopy(self.store)
        self.cas = patch.object(inv.userstore, "patch_kv", side_effect=mutate)
        self.invalidate = patch.object(inv, "_invalidate_joined")
        self.cas.start(); self.invalidate.start()
        self.addCleanup(self.cas.stop); self.addCleanup(self.invalidate.stop)

    def test_parallel_profile_creation_binds_one_row_per_item(self):
        def create(index):
            return inv.create_profile_record("u", geo="US", login_mode="auto", job_id="j", item_index=index)
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(create, [0,1,2,0,1,2]))
        self.assertEqual(len(self.store["byId"]), 3)
        self.assertEqual([r["id"] for r in rows[:3]], [r["id"] for r in rows[3:]])

    def test_conflicting_hoot_allocation_rejected_inside_atomic_update(self):
        a = inv.create_profile_record("u", geo="US", login_mode="auto")
        b = inv.create_profile_record("u", geo="US", login_mode="auto")
        with patch("app.provision.hoot.sacrifice_hoot_mode", return_value=False):
            inv.attach_hoot("u", a["id"], {"id": "hoot-1"})
            with self.assertRaises(inv.InventoryError):
                inv.attach_hoot("u", b["id"], {"id": "hoot-1"})

    def test_concurrent_profile_updates_preserve_sibling_and_disapproval(self):
        a = inv.create_profile_record("u", geo="US", login_mode="auto")
        b = inv.create_profile_record("u", geo="US", login_mode="auto")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda r: inv.update_profile("u", r["id"], status="ready"), [a,b]))
        self.assertTrue(all(r["status"] == "ready" for r in self.store["byId"].values()))
        inv.update_profile("u", a["id"], disapprovalStatus="disapproved")
        inv.update_profile("u", a["id"], status="ready")
        self.assertEqual(self.store["byId"][a["id"]]["status"], "disapproved")


class ParallelJobTests(unittest.TestCase):
    def test_completed_launch_does_not_wait_for_review_monitor(self):
        item = jobs.ProvisionItem(index=0, profile_id="p", status="done")
        job = jobs.ProvisionJob(id="j", owner_uid="u", geo="US", items=[item])
        with (patch.object(jobs, "_persist"),
              patch.object(jobs.job_runtime.provisions, "promote"),
              patch.object(jobs, "_release_unclaimed_sparks"),
              patch.object(jobs, "_run_job_item") as run):
            jobs._run_job(job)
        self.assertEqual(job.status, "done")
        run.assert_called_once()

    def test_separate_existing_profiles_resume_in_parallel(self):
        barrier = threading.Barrier(2, timeout=3)
        entered = []

        def resume(_job, item, _profile):
            entered.append(item.profile_id)
            barrier.wait()

        def one(pid):
            job = jobs.ProvisionJob(id=f"job-{pid}", owner_uid="u", geo="US")
            jobs._run_item(job, jobs.ProvisionItem(index=0, profile_id=pid))

        with (patch.object(jobs, "_persist"),
              patch.object(inv, "get_profile", side_effect=lambda _uid, pid: {"id": pid}),
              patch.object(jobs, "_resume_item", side_effect=resume)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(one, ("disapproval-profile", "whitehat-profile")))
        self.assertEqual(set(entered), {"disapproval-profile", "whitehat-profile"})

    def test_continue_rejects_another_live_job_on_same_profile(self):
        with (patch.object(jobs, "_require_browser_host"),
              patch.object(inv, "get_profile", return_value={"id": "p", "status": "failed"}),
              patch.object(jobs, "list_jobs", return_value=[
                  {"id": "active", "status": "running", "items": [{"profileId": "p"}]}
              ])):
            with self.assertRaisesRegex(inv.InventoryError, "already has active job"):
                jobs.continue_profile("u", "p")

    def test_three_profile_bodies_really_overlap_and_sibling_failure_isolated(self):
        job = jobs.ProvisionJob(id="j", owner_uid="u", geo="US", items=[jobs.ProvisionItem(index=i) for i in range(3)])
        barrier = threading.Barrier(3, timeout=3)
        entered = []
        def run(job, item):
            entered.append(item.index)
            barrier.wait()
            if item.index == 1:
                raise RuntimeError("fixture failure")
        with (patch.dict(os.environ, {"ORCHESTRATION_ENABLED": "1"}),
              patch.object(jobs, "_persist"), patch.object(jobs, "_run_item", side_effect=run),
              patch.object(jobs, "_release_unclaimed_sparks"), patch.object(jobs.job_runtime.provisions, "promote")):
            jobs._run_job(job)
        self.assertEqual(set(entered), {0,1,2})
        self.assertEqual([i.status for i in job.items], ["done", "failed", "done"])

    def test_disabled_rollout_keeps_one_profile(self):
        with patch.dict(os.environ, {"ORCHESTRATION_ENABLED": "0", "ORCHESTRATION_PROFILES": "3"}):
            self.assertEqual(bridge.parallelism(), 1)

    def test_worker_reduces_browser_capacity_when_memory_is_low(self):
        from app import provision_worker as worker
        with (patch.dict(os.environ, {"ORCHESTRATION_PROFILES": "3"}),
              patch.object(worker, "max_workers", return_value=4),
              patch("psutil.virtual_memory", return_value=type("Mem", (), {"available": 3 * 1024**3})())):
            self.assertEqual(worker.browser_capacity(), 1)
        with (patch.dict(os.environ, {"ORCHESTRATION_PROFILES": "3"}),
              patch.object(worker, "max_workers", return_value=4),
              patch("psutil.virtual_memory", return_value=type("Mem", (), {"available": 7 * 1024**3})())):
            self.assertEqual(worker.browser_capacity(), 3)

    def test_stopped_item_does_not_execute(self):
        job = jobs.ProvisionJob(id="j", owner_uid="u", geo="US", cancel_flag=True)
        item = jobs.ProvisionItem(index=0)
        with patch.object(jobs, "_run_item") as run:
            jobs._run_job_item(job, item)
        run.assert_not_called(); self.assertEqual(item.status, "cancelled")


class ProtocolTests(unittest.TestCase):
    def test_whitehat_step_is_admitted_before_dispatch(self):
        from app.routers import provision as route
        candidate = {"id": "s", "kind": "white_hat", "profileId": "p"}
        with (patch.object(route, "_owner_uid", return_value="u"),
              patch("app.orchestration.client.enabled", return_value=True),
              patch.object(route.inv, "claim_next_step", return_value=candidate),
              patch.object(route.inv, "get_profile", return_value={"id": "p"}),
              patch.object(route.inv, "set_claim_admission", return_value=True),
              patch.object(route.inv, "reclaim_stale_claims"),
              patch.object(route.inv, "heal_stuck_live_profiles"),
              patch.object(bridge, "acquire", return_value={"token": "t", "worker": "w"}) as acquire,
              patch("app.edge_cache.publish_activity")):
            result = route.worker_next(
                _="token", ownerUid="u", waitSeconds=0, skipKinds=None,
                schedulerProtocol=1, workerInstance="w", hostCapacity=2,
            )
        self.assertEqual(result["step"]["orchestration"]["token"], "t")
        self.assertEqual(acquire.call_args.kwargs["host_capacity"], 2)

    def test_renewal_does_not_overwrite_checkpoint(self):
        client = MagicMock(); client.call.return_value = {"ok": True}
        ctx = Context(client, "u", "w", {"token": "t"})
        ctx.renew()
        self.assertNotIn("checkpoint", client.call.call_args.kwargs)
        ctx.checkpoint({"postId": "1"})
        self.assertEqual(client.call.call_args.kwargs["checkpoint"], {"postId": "1"})

    def test_cancelled_grant_stops_execution(self):
        client = MagicMock(); client.call.return_value = {"ok": False}
        ctx = Context(client, "u", "w", {"token": "t"})
        with self.assertRaises(LeaseLost): ctx.renew()
        with self.assertRaises(LeaseLost): ctx.check()

    def test_registry_rejects_duplicate_handler_version(self):
        registry = Registry(); registry.register("test.v1", lambda ctx, payload: {})
        with self.assertRaises(ValueError): registry.register("test.v1", lambda ctx, payload: {})

    def test_bridge_stores_references_not_profile_secrets(self):
        client = MagicMock(); client.submit.return_value = "flow"
        client.call.side_effect = lambda op, **kw: {"task": {"token":"t"}} if op == "claim" else {"ok":True}
        with patch.object(bridge, "Client", return_value=client):
            grant = bridge.acquire("u", {"id":"s","kind":"setup_bc","profileId":"p"},
                                   {"adspowerProfileId":"ap","password":"secret"}, "worker")
        spec = client.submit.call_args.args[2]
        self.assertNotIn("secret", str(spec)); self.assertEqual(grant["token"], "t")
        self.assertIn("finance:u:p", [d["key"] for d in spec[0]["resources"]])

    def test_browser_grants_include_host_and_exact_profile(self):
        client = MagicMock(); client.submit.return_value = "flow"
        client.call.side_effect = lambda op, **kw: {"task": {"token": "t"}} if op == "claim" else {"ok": True}
        with patch.object(bridge, "Client", return_value=client), patch.dict(
            os.environ, {"ORCHESTRATION_ENABLED": "1", "ORCHESTRATION_PROFILES": "3"}
        ):
            bridge.acquire("u", {"id": "a", "kind": "deactivate_business_center", "profileId": "p-a"},
                           {"adspowerProfileId": "ap-a"}, "worker", host_capacity=2)
        spec = client.submit.call_args.args[2][0]["resources"]
        keys = {d["key"] for d in spec}
        self.assertIn("profile:u:p-a", keys)
        self.assertIn("adspower:profile:ap-a", keys)
        self.assertNotIn("finance:u", keys)
        resource = next(c for c in client.call.call_args_list if c.args[0] == "resource")
        self.assertEqual(resource.kwargs["capacity"], 2)


class BridgeAdmissionRecoveryTests(unittest.TestCase):
    def test_terminal_workflow_without_grant_requests_new_attempt(self):
        client = MagicMock()
        client.submit.return_value = "workflow-1"
        client.call.side_effect = lambda operation, **_kwargs: {"task": None} if operation == "claim" else {"ok": True}
        client.snapshot.return_value = {"tasks": [{"id": "step-1", "state": "cancelled"}]}
        with patch.object(bridge, "Client", return_value=client):
            with self.assertRaises(bridge.AdmissionTerminal):
                bridge.acquire("u", {"id": "step-1", "kind": "white_hat", "profileId": "p"}, {}, "worker")

    def test_terminal_grant_uses_next_admission_key(self):
        client = MagicMock()
        client.submit.return_value = "workflow-1"

        def call(operation, **_kwargs):
            if operation == "claim":
                return {"task": {"token": "expired"}}
            if operation == "heartbeat":
                raise LeaseLost("expired")
            return {"ok": True}

        client.call.side_effect = call
        client.snapshot.return_value = {"tasks": [{"id": "step-1", "state": "cancelled"}]}
        with patch.object(bridge, "Client", return_value=client):
            with self.assertRaises(bridge.AdmissionTerminal):
                bridge.acquire(
                    "u", {"id": "step-1", "kind": "white_hat", "profileId": "p", "admissionAttempt": 1},
                    {"adspowerProfileId": "ap"}, "worker",
                )
        self.assertEqual(client.submit.call_args.args[1], "provision-step:step-1:retry:1")
        claim = [c for c in client.call.call_args_list if c.args[0] == "claim"][0]
        self.assertEqual(claim.kwargs["requestId"], "step:step-1:admission:1")


if __name__ == "__main__": unittest.main()
