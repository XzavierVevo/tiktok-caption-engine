from __future__ import annotations

import json
import os
import tempfile
import threading
import sqlite3
import re
import unittest
from pathlib import Path
from unittest import mock

from app import duoplus as dp, spark_ingress as ingress, spark_phone_lock as locks
from app.spark_ui import SparkUI, SparkUIError

CODE = "#" + "A" * 64
URL = "https://www.tiktok.com/@test/video/1234567890"


def row(action, result=True, **data):
    return {"result_info": {"action": action, "result": result, "extra_data": {"data": data}}}


class LogTests(unittest.TestCase):
    def test_pending_blocks_phone(self):
        with mock.patch.object(dp, "list_tasks_recent", return_value=[{"id": "queued", "status": 0}]):
            self.assertEqual(dp.executing_spark_tasks()[0]["id"], "queued")

    def test_finish_without_code_still_retries_after_click_failure(self):
        info = dp.inspect_spark_task("x", logs=[row("CLICK_ELEMENT", False), row("FINISH")])
        self.assertTrue(info["shouldRetry"])

    def test_one_log_download_and_clipboard_beats_fail(self):
        logs = [row("GET_CLIPBOARD", text_content=CODE),
                row("OUTPUT_LOG", desc="FINALSPARK", output_log_content="fail"), row("FINISH")]
        with mock.patch.object(dp, "fetch_all_task_logs", return_value=logs) as fetch:
            info = dp.inspect_spark_task("x")
        self.assertTrue(info["success"])
        self.assertEqual(info["clipboardCode"], CODE)
        fetch.assert_called_once_with("x")

    def test_empty_clipboard_retains_task_id_and_retries(self):
        with mock.patch.object(ingress, "read_spark", return_value=""), \
             mock.patch.object(dp, "find_scheduled_task", return_value={"id": "failed-task", "name": "run", "status": 3}), \
             mock.patch.object(dp, "fetch_all_task_logs", return_value=[row("GET_CLIPBOARD", text_content=""), row("FINISH")]), \
             mock.patch.dict(os.environ, {"DUOPLUS_SPARK_LOG_GRACE_S": "0", "DUOPLUS_SPARK_SUCCESS_GRACE_S": "0"}):
            with self.assertRaises(dp.DuoPlusError) as caught:
                dp.wait_for_spark_code("phone", task_name="run", timeout_s=1)
        self.assertEqual(caught.exception.details["taskId"], "failed-task")
        self.assertTrue(caught.exception.details["shouldRetry"])
        self.assertIn("slider", str(caught.exception))

    def test_poll_discovers_task_once_and_reuses_logs(self):
        logs = [[row("START")], [row("START")], [row("GET_CLIPBOARD", text_content=CODE)]]
        with mock.patch.object(ingress, "read_spark", return_value=""), \
             mock.patch.object(dp, "find_scheduled_task", return_value={"id": "x", "name": "run", "status": 1}) as find, \
             mock.patch.object(dp, "fetch_all_task_logs", side_effect=logs) as fetch, \
             mock.patch.object(dp.time, "sleep"):
            code, _ = dp.wait_for_spark_code("phone", task_name="run", poll_s=0)
        self.assertEqual(code, CODE)
        self.assertEqual(find.call_count, 1)
        self.assertEqual(fetch.call_count, 3)


class LockTests(unittest.TestCase):
    def test_nested_owner_and_competing_thread(self):
        from app import cloudflare_store as cf
        failures = []
        def competing():
            try:
                with locks.phone_lock("phone", timeout_s=0):
                    failures.append("incorrectly acquired")
            except locks.PhoneBusy:
                failures.append("busy")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(locks, "DATA_DIR", Path(tmp)), \
             mock.patch.object(cf, "enabled", return_value=False):
            with locks.phone_lock("phone"):
                with locks.phone_lock("phone"):
                    locks.assert_owned()
                t = threading.Thread(target=competing)
                t.start(); t.join(3)
                self.assertFalse(t.is_alive())
                self.assertEqual(failures, ["busy"])
            with locks.phone_lock("phone", timeout_s=0):
                pass

    def test_expired_owner_cannot_send_commands(self):
        locks._local.lease = {"phone": "p", "lost": threading.Event(), "deadline": [0]}
        try:
            with self.assertRaises(locks.PhoneBusy):
                locks.assert_owned()
        finally:
            locks._local.lease = None

    def test_cloud_failure_does_not_fall_back_to_local(self):
        from app import cloudflare_store as cf
        with mock.patch.object(cf, "enabled", return_value=True), \
             mock.patch.object(cf, "_request", side_effect=cf.StateUnavailable("offline")):
            with self.assertRaises(cf.StateUnavailable):
                with locks.phone_lock("phone", timeout_s=0):
                    self.fail("must not acquire")


class CallbackTests(unittest.TestCase):
    def test_rejects_local_url_before_phone_use(self):
        for base in ("https://localhost:8765", "https://127.0.0.1", "http://example.com", "https://10.0.0.1"):
            with self.subTest(base=base), mock.patch.dict(os.environ, {
                "DUOPLUS_SPARK_CALLBACK_ENABLED": "1", "SPARK_CALLBACK_BASE_URL": base,
                "SPARK_CALLBACK_SECRET": "a&b", "PUBLIC_UI_ORIGIN": "",
            }):
                with self.assertRaises(ValueError):
                    ingress.delivery_url(task_name="run")

    def test_secret_encoding_and_redaction(self):
        with mock.patch.dict(os.environ, {"SPARK_CALLBACK_BASE_URL": "https://example.com",
                                         "SPARK_CALLBACK_SECRET": "a&b"}):
            url = ingress.callback_url(task_name="run")
        self.assertIn("token=a%26b", url)
        self.assertNotIn("a%26b", ingress.redact_callback_url(url))

    def test_delivery_failure_is_metadata_not_generation_retry(self):
        import requests
        with mock.patch.object(requests, "post", side_effect=requests.Timeout), mock.patch.object(ingress.time, "sleep"):
            out = ingress.deliver_spark("https://example.com", task_name="run", post_url=URL, spark_code=CODE)
        self.assertEqual(out["callbackDelivery"], "failed")


class ReadinessTests(unittest.TestCase):
    phone = {"id": "p", "name": "test", "status": 2}

    def test_starting_phone_is_not_powered_on_again(self):
        with mock.patch.object(dp, "phone_status", side_effect=[{"status": 10}, {"status": 11}, {"status": 1}, {"status": 1}]), \
             mock.patch.object(dp, "adb_command", return_value="1") as adb, \
             mock.patch.object(dp, "power_on") as power, mock.patch.object(dp.time, "sleep"):
            self.assertEqual(dp.ensure_powered_on(self.phone)["status"], 1)
        power.assert_not_called()
        self.assertEqual(adb.call_count, 2)

    def test_temporary_proxy_rejection_can_finish_startup(self):
        rejection = dp.DuoPlusError("Connection check failed", details={"failureKind": "power_on_rejected",
                                                                      "rawReason": "proxy connection failed"})
        with mock.patch.object(dp, "phone_status", side_effect=[{"status": 2}, {"status": 11}, {"status": 1}, {"status": 1}]), \
             mock.patch.object(dp, "adb_command", return_value="1"), \
             mock.patch.object(dp, "power_on", side_effect=rejection) as power, mock.patch.object(dp.time, "sleep"):
            self.assertEqual(dp.ensure_powered_on(self.phone)["status"], 1)
        power.assert_called_once_with(["p"])

    def test_on_status_alone_is_not_ready(self):
        with mock.patch.object(dp, "phone_status", return_value={"status": 1}), \
             mock.patch.object(dp, "adb_command", side_effect=["0", "1", "1"]) as adb, \
             mock.patch.object(dp, "power_on") as power, mock.patch.object(dp.time, "sleep"):
            dp.ensure_powered_on(self.phone)
        self.assertEqual(adb.call_count, 3)
        power.assert_not_called()

    def test_readiness_timeout_is_bounded_and_diagnostic(self):
        now = [0.0]
        def sleep(seconds): now[0] += seconds
        with mock.patch.object(dp.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(dp.time, "sleep", side_effect=sleep), \
             mock.patch.object(dp, "phone_status", return_value={"status": 2}), \
             mock.patch.object(dp, "power_on") as power:
            with self.assertRaises(dp.DuoPlusError) as caught:
                dp.ensure_powered_on(self.phone, timeout_s=10)
        power.assert_called_once()
        self.assertEqual(now[0], 10)
        self.assertEqual(caught.exception.details["failureKind"], "phone_not_ready")

    def test_balance_failure_is_not_hidden_by_startup_wait(self):
        error = dp.DuoPlusError("Insufficient balance", details={"failureKind": "power_on_rejected"})
        with mock.patch.object(dp, "phone_status", return_value={"status": 2}), \
             mock.patch.object(dp, "power_on", side_effect=error), \
             mock.patch.object(dp.time, "sleep") as sleep:
            with self.assertRaises(dp.DuoPlusError):
                dp.ensure_powered_on(self.phone)
        sleep.assert_not_called()

    def test_host_proxy_error_does_not_blame_phone_proxy(self):
        message = dp.format_duoplus_user_error("DuoPlus request failed: ProxyError")
        self.assertIn("API connection", message)
        self.assertNotIn("replace", message)
        message = dp.format_duoplus_user_error("Phone being configured after proxy change")
        self.assertIn("phone not ready", message)


def node(text="", *, checked=None, bounds="[0,0][100,100]"):
    state = ' checkable="true" checked="' + checked + '"' if checked is not None else ''
    return f'<node package="com.zhiliaoapp.musically" text="{text}" enabled="true" bounds="{bounds}"{state}/>'


def screen(*nodes):
    return '<hierarchy>' + ''.join(nodes) + '</hierarchy>'


class UITests(unittest.TestCase):
    def driver(self, screens, clips):
        snapshots = iter(screens)
        commands = []
        def command(cmd):
            commands.append(cmd)
            return next(snapshots) if cmd.startswith("uiautomator") else ""
        ui = SparkUI(command, mock.Mock(side_effect=clips), dp.looks_like_spark_code,
                     package=dp.TIKTOK_PACKAGE, wait_s=0)
        return ui, commands

    def test_enabled_authorization_is_never_toggled_and_copy_is_retried(self):
        settings = screen(node("Ad settings"))
        auth = screen('<node>' + node("Ad authorization") + node(checked="true", bounds="[200,0][300,100]") + '</node>')
        copy = screen(node("Copy code", bounds="[400,0][500,100]"))
        ui, commands = self.driver([settings, auth, copy, copy, copy], ["", "", CODE])
        with mock.patch("app.spark_ui.time.sleep"):
            self.assertEqual(ui.run("1234567890"), CODE)
        self.assertNotIn("input tap 250 50", commands)
        self.assertEqual(commands.count("input tap 450 50"), 2)

    def test_ambiguous_control_sends_no_tap(self):
        ui, commands = self.driver([screen(node("Ad settings"), node("Ad settings"))], [""])
        with self.assertRaises(SparkUIError):
            ui.run("1234567890")
        self.assertFalse(any(c.startswith("input tap") for c in commands))

    def test_stale_clipboard_is_not_accepted(self):
        settings = screen(node("Ad settings"))
        auth = screen('<node>' + node("Ad authorization") + node(checked="true") + '</node>')
        copy = screen(node("Copy code"))
        ui, _ = self.driver([settings, auth, copy, copy, copy, copy], [CODE] * 4)
        with mock.patch("app.spark_ui.time.sleep"), self.assertRaises(SparkUIError):
            ui.run("1234567890")

    def test_disabled_authorization_is_enabled_once_then_verified(self):
        settings = screen(node("Ad settings"))
        def auth(checked):
            return screen('<node>' + node("Ad authorization") +
                          node(checked=checked, bounds="[200,0][300,100]") + '</node>')
        generate = screen(node("Generate code", bounds="[300,0][400,100]"))
        copy = screen(node("Copy code", bounds="[400,0][500,100]"))
        ui, commands = self.driver([settings, auth("false"), auth("true"), generate, copy, copy], ["", CODE])
        with mock.patch("app.spark_ui.time.sleep"):
            self.assertEqual(ui.run("1234567890"), CODE)
        self.assertEqual(commands.count("input tap 250 50"), 1)
        self.assertEqual(commands.count("input tap 350 50"), 1)


class DurableDiagnosticsTests(unittest.TestCase):
    def test_retry_floor_applies_to_configured_attempts(self):
        with mock.patch.dict(os.environ, {"DUOPLUS_SPARK_MAX_ATTEMPTS": "1"}):
            self.assertEqual(dp.default_spark_max_attempts(), 3)

    def test_finished_without_code_gets_two_retries(self):
        error = dp.DuoPlusError("No code captured", details={"taskId": "wm1UJl", "shouldRetry": False})
        phone = {"id": "p", "name": "phone", "status": 1}
        with mock.patch.object(dp, "find_phone", return_value=phone), \
             mock.patch.object(dp, "find_spark_template", return_value={"id": "template"}), \
             mock.patch.object(dp, "ensure_powered_on"), \
             mock.patch.object(dp, "wait_until_phone_idle"), \
             mock.patch.object(dp, "reset_phone_for_spark"), \
             mock.patch.object(dp, "_claim_spark_run", return_value="video"), \
             mock.patch.object(dp, "_release_spark_run"), \
             mock.patch.object(ingress, "delivery_url", return_value=""), \
             mock.patch.object(dp, "schedule_spark_task", return_value="run") as schedule, \
             mock.patch.object(dp, "wait_for_spark_code", side_effect=error):
            with self.assertRaises(dp.DuoPlusError) as caught:
                dp._fetch_spark_code_unlocked(URL, max_attempts=2, retry_delay_s=0)
        self.assertEqual(schedule.call_count, 3)
        self.assertEqual(caught.exception.details["taskId"], "wm1UJl")
        self.assertEqual(len(caught.exception.details["attempts"]), 3)
        self.assertTrue(caught.exception.details["attempts"][0]["shouldRetry"])
        self.assertFalse(caught.exception.details["attempts"][-1]["shouldRetry"])

    def test_valid_code_stops_retries(self):
        phone = {"id": "p", "name": "phone", "status": 1}
        error = dp.DuoPlusError("No code captured", details={"shouldRetry": False})
        with mock.patch.object(dp, "find_phone", return_value=phone), \
             mock.patch.object(dp, "find_spark_template", return_value={"id": "template"}), \
             mock.patch.object(dp, "ensure_powered_on"), \
             mock.patch.object(dp, "wait_until_phone_idle"), \
             mock.patch.object(dp, "reset_phone_for_spark"), \
             mock.patch.object(dp, "_claim_spark_run", return_value="video"), \
             mock.patch.object(dp, "_release_spark_run"), \
             mock.patch.object(ingress, "delivery_url", return_value=""), \
             mock.patch.object(ingress, "deliver_spark", return_value={}), \
             mock.patch.object(dp, "schedule_spark_task", return_value="run") as schedule, \
             mock.patch.object(dp, "wait_for_spark_code", side_effect=[error, (CODE, {"taskId": "done"})]):
            result = dp._fetch_spark_code_unlocked(URL, max_attempts=1, retry_delay_s=0)
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(result["sparkCode"], CODE)
        self.assertEqual(result["attemptCount"], 2)

    def test_verified_ui_fetch_gets_two_retries(self):
        phone = {"id": "p", "name": "phone", "status": 1}
        ui = mock.Mock()
        ui.stage = "authorization"
        ui.run.side_effect = [SparkUIError("slider missed", stage="authorization"),
                              SparkUIError("slider missed", stage="authorization"), CODE]
        with mock.patch("app.spark_ui.SparkUI", return_value=ui), \
             mock.patch.object(dp, "ensure_powered_on"), \
             mock.patch.object(dp, "wait_until_phone_idle"), \
             mock.patch.object(dp, "_claim_spark_run", return_value="video"), \
             mock.patch.object(dp, "_release_spark_run"), \
             mock.patch.object(dp, "spark_code_owned_elsewhere", return_value=False), \
             mock.patch.object(ingress, "delivery_url", return_value=""), \
             mock.patch.object(ingress, "deliver_spark", return_value={}):
            result = dp._fetch_verified_spark(URL, phone=phone, max_attempts=1, retry_delay_s=0)
        self.assertEqual(ui.run.call_count, 3)
        self.assertEqual(result["sparkCode"], CODE)

    def test_failed_group_result_keeps_id_and_next_item_runs(self):
        phone = {"id": "p", "name": "phone", "status": 1}
        failure = dp.DuoPlusError("No code", details={"taskId": "wm1UJl"})
        with mock.patch.object(dp, "ensure_powered_on", return_value=phone), \
             mock.patch.object(dp, "spark_already_succeeded", return_value=False), \
             mock.patch.object(dp, "fetch_spark_code", side_effect=[failure, {"sparkCode": CODE}]):
            results = dp._run_spark_phone_group_unlocked([{"postUrl": URL}, {"postUrl": URL}],
                                                        phone=phone, power_off_after=False)
        self.assertEqual(results[0]["taskId"], "wm1UJl")
        self.assertTrue(results[1]["ok"])

    def test_failure_survives_catalog_and_job_snapshot(self):
        from app import spark_jobs as sj
        item = sj.SparkItem(source_id="s", caption="c", output_rel="test.mp4", post_url=URL,
                            status="failed", task_id="wm1UJl", error="empty clipboard",
                            diagnostics={"failureKind": "code_not_captured"})
        job = sj.SparkJob(id="job", owner_uid="test", thread_id="thread", items=[item])
        with mock.patch.object(sj.catalog, "find_render_by_pair", return_value={}), \
             mock.patch.object(sj.catalog, "update_publish") as update, \
             mock.patch.object(sj.chat_store, "patch_plan_pair_spark"), \
             mock.patch.object(sj.job_runtime.sparks, "persist"):
            sj._persist_item(job, item)
            state = sj.job_state(job)
        self.assertEqual(update.call_args.args[2]["tiktok_spark_task_id"], "wm1UJl")
        self.assertEqual(state["items"][0]["diagnostics"], item.diagnostics)
        self.assertIn("tiktok_spark_diagnostics", sj.catalog.COLUMNS)


class D1LeaseSQLTests(unittest.TestCase):
    def test_atomic_acquire_renew_expire_and_stale_release(self):
        # Execute the actual Worker SQL against SQLite (D1's SQL engine).
        source = (Path(__file__).resolve().parents[1] / "worker/src/state.ts").read_text()
        source = source[source.index("export async function sparkPhoneLock"):]
        sql = []
        for expr in re.findall(r"\.prepare\((.*?)\)\.bind", source, re.S):
            sql.append("".join(json.loads(x) for x in re.findall(r'"(?:[^"\\]|\\.)*"', expr)))
        self.assertEqual(len(sql), 3)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE documents(uid TEXT, collection TEXT, doc_id TEXT, body_json TEXT, "
                   "body_sha256 TEXT, version INTEGER, updated_at INTEGER, deleted_at INTEGER, storage_rel TEXT, "
                   "PRIMARY KEY(uid,collection,doc_id))")
        body = lambda owner, expiry: json.dumps({"owner": owner, "expiresAt": expiry})
        acquire = lambda who, now: db.execute(sql[0], ("phone", body(who, now + 120), now, now, who)).rowcount
        self.assertEqual(acquire("A", 0), 1)
        self.assertEqual(acquire("B", 1), 0)
        self.assertEqual(acquire("A", 2), 1)  # response retry is idempotent
        self.assertEqual(db.execute(sql[1], (body("A", 180), 60, "phone", "A", 60)).rowcount, 1)
        self.assertEqual(acquire("B", 150), 0)
        self.assertEqual(acquire("B", 181), 1)
        self.assertEqual(db.execute(sql[2], (body("", 0), 182, "phone", "A")).rowcount, 0)
        self.assertEqual(db.execute(sql[1], (body("A", 400), 183, "phone", "A", 183)).rowcount, 0)
        self.assertEqual(db.execute(sql[2], (body("", 0), 184, "phone", "B")).rowcount, 1)
        self.assertEqual(acquire("C", 185), 1)


if __name__ == "__main__":
    unittest.main()
