"""Tests for batch source restore before encode/ffprobe."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import jobs
from app.encode import EncodeError


class EnsureSourceLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.uid = "test-batch-uid"
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _item(self, path: Path, **kwargs) -> jobs.JobItem:
        defaults = dict(
            video_path=str(path),
            video_stem=path.stem,
            video_hash="abc123",
            video_index=0,
            caption="asdf",
            caption_index=0,
            source_id="src1",
            source_name=path.name,
            source_url="",
        )
        defaults.update(kwargs)
        return jobs.JobItem(**defaults)

    def test_returns_existing_local_file(self) -> None:
        path = self.downloads / "clip.mp4"
        path.write_bytes(b"not-empty")
        got = jobs._ensure_source_local(self.uid, self._item(path))
        self.assertEqual(got, str(path))

    def test_restores_from_r2_when_missing(self) -> None:
        path = self.downloads / "missing.mp4"
        restored = self.downloads / "restored.mp4"
        restored.write_bytes(b"from-r2")
        with mock.patch.object(jobs.storage, "rel_of", return_value="downloads/missing.mp4"), mock.patch.object(
            jobs.storage, "ensure_local", return_value=restored
        ):
            got = jobs._ensure_source_local(self.uid, self._item(path))
        self.assertEqual(got, str(restored))

    def test_redownloads_when_r2_miss_and_url_present(self) -> None:
        path = self.downloads / "gone.mp4"
        fresh = self.downloads / "fresh.mp4"
        fresh.write_bytes(b"fresh-bytes")
        item = self._item(path, source_url="https://example.com/v.mp4")
        with mock.patch.object(jobs.storage, "rel_of", return_value="downloads/gone.mp4"), mock.patch.object(
            jobs.storage, "ensure_local", return_value=None
        ), mock.patch.object(jobs.storage, "ENABLED", True), mock.patch(
            "app.ingest.download_url", return_value=fresh
        ), mock.patch.object(jobs.storage, "mirror_up", return_value=True), mock.patch(
            "app.library.state", return_value={"sources": {}}
        ):
            got = jobs._ensure_source_local(self.uid, item)
        self.assertEqual(got, str(fresh))

    def test_raises_clear_error_when_unrecoverable(self) -> None:
        path = self.downloads / "gone.mp4"
        item = self._item(path, source_url="")
        with mock.patch.object(jobs.storage, "rel_of", return_value="downloads/gone.mp4"), mock.patch.object(
            jobs.storage, "ensure_local", return_value=None
        ), mock.patch.object(jobs.storage, "ENABLED", True), mock.patch(
            "app.library.state", return_value={"sources": {}}
        ):
            with self.assertRaises(EncodeError) as ctx:
                jobs._ensure_source_local(self.uid, item)
        self.assertIn("could not be restored", str(ctx.exception))
        self.assertIn("gone.mp4", str(ctx.exception))

    def test_rejects_zero_byte_local_and_restores(self) -> None:
        path = self.downloads / "empty.mp4"
        path.write_bytes(b"")
        restored = self.downloads / "ok.mp4"
        restored.write_bytes(b"ok")
        with mock.patch.object(jobs.storage, "rel_of", return_value="downloads/empty.mp4"), mock.patch.object(
            jobs.storage, "ensure_local", return_value=restored
        ):
            got = jobs._ensure_source_local(self.uid, self._item(path))
        self.assertEqual(got, str(restored))


class ProbeMissingFileTests(unittest.TestCase):
    def test_probe_missing_file_message(self) -> None:
        from app import encode

        missing = Path(tempfile.gettempdir()) / "caption-tool-missing-probe.mp4"
        missing.unlink(missing_ok=True)
        with mock.patch.object(encode, "FFPROBE_PATH", encode.FFPROBE_PATH or "ffprobe"):
            if not encode.FFPROBE_PATH:
                self.skipTest("ffprobe not available")
            with self.assertRaises(EncodeError) as ctx:
                encode.probe(missing)
        self.assertIn("not found", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
