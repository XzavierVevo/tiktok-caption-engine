"""Clips that already have a caption burned in are flagged and never rendered."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app import jobs
from app.config import FFMPEG_PATH
from app.encode import BurnedCaptionError, burn_caption
from app.existing_caption import has_burned_caption
from app.layout import render_overlay

W, H = 720, 1280


def _scene() -> np.ndarray:
    frame = np.zeros((H, W, 3), np.uint8)
    frame[:] = (28, 36, 22)
    frame[8 : H // 10, W // 3 : 2 * W // 3] = (180, 220, 240)
    return frame


def _with_caption(bgr: np.ndarray, text: str) -> np.ndarray:
    overlay = render_overlay(text, W, H, {})
    base = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")
    merged = Image.alpha_composite(base, overlay).convert("RGB")
    return cv2.cvtColor(np.array(merged), cv2.COLOR_RGB2BGR)


def _write_clip(path: Path, frame: np.ndarray, seconds: float = 2.0) -> None:
    png = path.with_suffix(".png")
    cv2.imwrite(str(png), frame)
    subprocess.run(
        [
            FFMPEG_PATH, "-y", "-v", "error", "-loop", "1", "-i", str(png),
            "-t", str(seconds), "-r", "15", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )
    png.unlink()


@unittest.skipUnless(FFMPEG_PATH, "ffmpeg not available")
class BurnedCaptionBlockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="burned-"))
        cls.clean = cls.tmp / "clean.mp4"
        cls.captioned = cls.tmp / "captioned.mp4"
        _write_clip(cls.clean, _scene())
        _write_clip(
            cls.captioned,
            _with_caption(_scene(), "this clip already has a caption on it"),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_detector_flags_captioned_clip(self) -> None:
        self.assertTrue(has_burned_caption(self.captioned))

    def test_detector_passes_clean_clip(self) -> None:
        self.assertFalse(has_burned_caption(self.clean))

    def test_render_refuses_captioned_clip(self) -> None:
        out = self.tmp / "out_bad.mp4"
        with self.assertRaises(BurnedCaptionError):
            burn_caption(self.captioned, "new caption", out, {})
        self.assertFalse(out.exists())

    def test_render_works_on_clean_clip(self) -> None:
        out = self.tmp / "out_ok.mp4"
        burn_caption(self.clean, "new caption", out, {})
        self.assertTrue(out.is_file() and out.stat().st_size > 0)
        # The output now has one caption, so it must never be accepted as a base.
        self.assertTrue(has_burned_caption(out))


class FlaggedSourceBatchTests(unittest.TestCase):
    def test_start_batch_rejects_flagged_source(self) -> None:
        video = {
            "id": "abc", "name": "broll.mp4", "path": "x.mp4", "stem": "broll",
            "hash": "h", "duration": 5.0, "burnedCaption": True,
        }
        with self.assertRaises(ValueError) as ctx:
            jobs.start_batch("u", [video], ["caption"], "matrix", {}, None)
        self.assertIn("broll.mp4", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
