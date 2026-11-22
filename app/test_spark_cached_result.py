import unittest
from unittest import mock

from app import duoplus as dp, spark_jobs as sj

URL = "https://www.tiktok.com/@test/video/1234567890"
CODE = "#" + "A" * 64
PHONE = {"id": "p", "name": "phone"}


class CachedResultTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("_spark_completed_ids", set()),
                            ("_spark_inflight_ids", set()), ("_spark_code_owners", {})):
            patch = mock.patch.object(dp, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_completed_marker_without_code_does_not_block_fetch(self):
        dp._spark_completed_ids.add("1234567890")
        self.assertFalse(dp.spark_already_succeeded(URL))
        self.assertEqual(dp._claim_spark_run(URL), "1234567890")
        with self.assertRaisesRegex(dp.DuoPlusError, "already running"):
            dp._claim_spark_run(URL)

    def test_cached_group_result_is_returned_and_persisted_instead_of_skipped(self):
        dp.register_spark_code(CODE, URL)
        dp._release_spark_run("1234567890", completed=True)
        item = sj.SparkItem(source_id="s", caption="c", output_rel="test.mp4", post_url=URL)
        job = sj.SparkJob(id="j", owner_uid="u", thread_id="t", items=[item])

        def persist_result(result):
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "done")
            item.spark_code = result["sparkCode"]
            item.status = result["status"]
            sj._persist_item(job, item)

        with mock.patch.object(dp, "find_phone", return_value=PHONE), \
             mock.patch.object(dp, "phone_lock"), \
             mock.patch.object(dp, "ensure_powered_on", return_value=PHONE), \
             mock.patch.object(dp, "_fetch_spark_code_unlocked") as fetch, \
             mock.patch.object(dp, "_fetch_verified_spark") as verified, \
             mock.patch.object(sj.catalog, "find_render_by_pair", return_value={}), \
             mock.patch.object(sj.catalog, "find_by_spark_code", return_value=None), \
             mock.patch.object(sj.catalog, "update_publish") as update, \
             mock.patch.object(sj.chat_store, "patch_plan_pair_spark") as plan:
            result = dp._run_spark_phone_group_unlocked(
                [{"postUrl": URL}], phone=PHONE, power_off_after=False, on_result=persist_result)
            fetch.assert_not_called()
            verified.assert_not_called()
            self.assertEqual(result[0]["sparkCode"], CODE)
            self.assertEqual(update.call_args.args[2]["tiktok_spark_code"], CODE)
            self.assertEqual(plan.call_args.kwargs["spark_code"], CODE)

    def test_missing_or_wrong_post_code_runs_phone_fetch(self):
        dp._spark_completed_ids.add("1234567890")
        dp.register_spark_code(CODE, "https://www.tiktok.com/@test/video/9999999999")
        with mock.patch.object(dp, "find_phone", return_value=PHONE), \
             mock.patch.object(dp, "phone_lock"), \
             mock.patch.dict(dp.os.environ, {"DUOPLUS_SPARK_ENGINE": "legacy"}), \
             mock.patch.object(dp, "_fetch_spark_code_unlocked", return_value={"sparkCode": "fresh"}) as fetch:
            self.assertEqual(dp.fetch_spark_code(URL)["sparkCode"], "fresh")
            fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
