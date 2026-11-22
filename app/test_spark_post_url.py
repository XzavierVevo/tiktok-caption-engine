"""spark_post_url_param must not stuff https URLs into aweme/detail/${post_url}."""

from __future__ import annotations

import os
import unittest
from unittest import mock


class SparkPostUrlParamTests(unittest.TestCase):
    def test_default_uses_numeric_id_for_photo(self) -> None:
        from app.duoplus import spark_post_url_param

        with mock.patch.dict(os.environ, {"DUOPLUS_SPARK_POST_URL_MODE": ""}, clear=False):
            # Ensure empty override (not inherited full_url from env).
            os.environ.pop("DUOPLUS_SPARK_POST_URL_MODE", None)
            out = spark_post_url_param(
                "https://www.tiktok.com/@giby724/photo/7681525977565924638"
            )
        self.assertEqual(out, "7681525977565924638")

    def test_full_url_mode_opt_in(self) -> None:
        from app.duoplus import spark_post_url_param

        url = "https://www.tiktok.com/@giby724/photo/7681525977565924638"
        with mock.patch.dict(os.environ, {"DUOPLUS_SPARK_POST_URL_MODE": "full_url"}):
            self.assertEqual(spark_post_url_param(url), url)

    def test_video_default_is_id(self) -> None:
        from app.duoplus import spark_post_url_param

        os.environ.pop("DUOPLUS_SPARK_POST_URL_MODE", None)
        out = spark_post_url_param(
            "https://www.tiktok.com/@giby724/video/7681412889957158175"
        )
        self.assertEqual(out, "7681412889957158175")

    def test_stale_failed_job_does_not_erase_retry_spark_code(self) -> None:
        from app import spark_jobs

        code = "#lerpesCp50GgCkqaG9+b3o17wryNawtgBOUuwQnE8iTHSiaxtPjCAJaJEHBPB1M="
        post_url = "https://www.tiktok.com/@giby724/video/7687814412543085855"
        item = spark_jobs.SparkItem(
            source_id="source-1",
            caption="caption",
            output_rel="batch/video.mp4",
            post_url=post_url,
            status="failed",
            error="older job failed",
        )
        job = spark_jobs.SparkJob(id="job-1", owner_uid="user-1", thread_id="thread-1")
        job.items = [item]
        current = {"output_rel": item.output_rel, "tiktok_spark_code": code}

        with (
            mock.patch.object(spark_jobs.catalog, "find_render_by_pair", return_value=current),
            mock.patch.object(spark_jobs.catalog, "find_by_spark_code", return_value=current),
            mock.patch.object(spark_jobs.catalog, "update_publish") as update,
            mock.patch.object(spark_jobs.chat_store, "patch_plan_pair_spark"),
        ):
            spark_jobs._persist_item(job, item)

        self.assertEqual(item.status, "done")
        self.assertEqual(item.spark_code, code)
        self.assertEqual(update.call_args.args[2]["tiktok_spark_code"], code)


if __name__ == "__main__":
    unittest.main()
