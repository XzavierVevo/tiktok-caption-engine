"""A new caption must not be painted through one already burned into the clip."""

from __future__ import annotations

import unittest

import cv2
import numpy as np
from PIL import Image

from app.existing_caption import clear_outline_caption, detect_outline_mask
from app.layout import render_overlay


def _scene(w: int, h: int) -> np.ndarray:
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = (28, 36, 22)
    # Corner light that is not a caption.
    frame[8 : h // 10, w // 3 : 2 * w // 3] = (180, 220, 240)
    return frame


def _with_caption(bgr: np.ndarray, text: str) -> np.ndarray:
    h, w = bgr.shape[:2]
    overlay = render_overlay(text, w, h, {})
    base = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")
    merged = Image.alpha_composite(base, overlay).convert("RGB")
    return cv2.cvtColor(np.array(merged), cv2.COLOR_RGB2BGR)


def _white_count(bgr: np.ndarray) -> int:
    return int(np.count_nonzero(np.all(bgr >= 200, axis=2)))


class ExistingCaptionTests(unittest.TestCase):
    def test_outline_caption_is_removed_and_the_room_stays(self) -> None:
        src = _with_caption(
            _scene(1280, 720),
            "how fast mfs run to turn on the fyp\nsetting after they dropped ts update",
        )
        mask = detect_outline_mask(src)
        self.assertIsNotNone(mask)
        before = _white_count(src)
        cleared = clear_outline_caption(src, mask)
        self.assertLess(_white_count(cleared), before * 0.1)
        # The ceiling light is outside the caption mask and stays bright.
        self.assertGreater(int(cleared[20, 640, 2]), 180)

    def test_plain_frame_is_not_treated_as_a_caption(self) -> None:
        self.assertIsNone(detect_outline_mask(_scene(1280, 720)))

    def test_person_covering_the_line_is_kept(self) -> None:
        src = _with_caption(
            _scene(1280, 720),
            "I flipped the screen in the lobby and nobody queued",
        )
        mask = detect_outline_mask(src)
        self.assertIsNotNone(mask)
        covered = src.copy()
        covered[300:460, 180:420] = (8, 8, 8)
        cleared = clear_outline_caption(covered, mask)
        self.assertLess(int(cleared[380, 300].max()), 30)

    def test_new_caption_is_the_only_line(self) -> None:
        old = _with_caption(
            _scene(1280, 720),
            "how fast mfs run to turn on the fyp\nsetting after they dropped ts update",
        )
        mask = detect_outline_mask(old)
        cleared = clear_outline_caption(old, mask)
        fresh = _with_caption(cleared, "I flipped the screen in the lobby and nobody queued")
        # The old two-line caption is gone, so the frame's white pixels are the new line.
        new_only = _with_caption(
            _scene(1280, 720),
            "I flipped the screen in the lobby and nobody queued",
        )
        self.assertLess(abs(_white_count(fresh) - _white_count(new_only)), 800)


if __name__ == "__main__":
    unittest.main()
