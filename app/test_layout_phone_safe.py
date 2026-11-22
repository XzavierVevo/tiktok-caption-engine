"""Burn-in stays on a 9:16 phone and clear of TikTok chrome."""

from __future__ import annotations

import unittest

from app.config import TIKTOK_PHONE_SAFE
from app.layout import caption_block_rect, fit_caption, max_safe_width_ratio


FRAME_W, FRAME_H = 1080, 1920
LONG = (
    "how fast mfs run to turn on the fyp setting after they dropped ts update "
    "and everyone is still talking about it on the for you page tonight"
)


class PhoneSafeCaptionTests(unittest.TestCase):
    def test_wide_setting_is_clamped_off_the_right_rail(self) -> None:
        _lines, _size, fitted = fit_caption(
            LONG, FRAME_W, FRAME_H, {"maxWidthRatio": 0.94, "yRatio": 0.50}
        )
        self.assertLessEqual(fitted["maxWidthRatio"], max_safe_width_ratio() + 1e-9)
        self.assertLessEqual(fitted["maxWidthRatio"], 0.70)

    def test_block_stays_inside_phone_safe_zone(self) -> None:
        left, top, right, bottom = caption_block_rect(
            LONG,
            FRAME_W,
            FRAME_H,
            {"maxWidthRatio": 0.94, "yRatio": 0.78, "fontSizeRatio": 0.06},
        )
        safe_l = FRAME_W * TIKTOK_PHONE_SAFE["left"]
        safe_t = FRAME_H * TIKTOK_PHONE_SAFE["top"]
        safe_r = FRAME_W * (1 - TIKTOK_PHONE_SAFE["right"])
        safe_b = FRAME_H * (1 - TIKTOK_PHONE_SAFE["bottom"])
        self.assertGreaterEqual(left, safe_l - 1)
        self.assertGreaterEqual(top, safe_t - 1)
        self.assertLessEqual(right, safe_r + 1)
        self.assertLessEqual(bottom, safe_b + 1)

    def test_lower_preset_does_not_cover_learn_more(self) -> None:
        _left, _top, _right, bottom = caption_block_rect(
            "how fast mfs run to turn on the fyp\nsetting after they dropped ts update",
            FRAME_W,
            FRAME_H,
            {"yRatio": 0.72, "maxWidthRatio": 0.86},
        )
        cta_top = FRAME_H * (1 - TIKTOK_PHONE_SAFE["bottom"])
        self.assertLessEqual(bottom, cta_top + 1)


if __name__ == "__main__":
    unittest.main()
