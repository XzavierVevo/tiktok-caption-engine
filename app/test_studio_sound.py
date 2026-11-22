from __future__ import annotations

import unittest

from app.studio_sound import classify_studio_review, parse_studio_content_rows, pick_sound_row


SAMPLE = """
Posts 748
Drafts 0
00:05
You should try
Nov 18, 9:41 AM
Everyone
0
0
0
00:20
if i can earn, my fyp pays
Content under review
Nov 18, 9:23 AM
Only me
0
0
0
"""


class StudioSoundTests(unittest.TestCase):
    def test_pending_under_review(self) -> None:
        self.assertEqual(
            classify_studio_review("if i can earn\nContent under review\nOnly me"),
            "pending",
        )

    def test_approved_everyone(self) -> None:
        self.assertEqual(
            classify_studio_review(
                "00:20\nif i can earn, my fyp pays\nNov 18, 9:23 AM\nEveryone\n5"
            ),
            "approved",
        )

    def test_everyone_still_pending_if_review_badge(self) -> None:
        self.assertEqual(
            classify_studio_review("Everyone\nContent under review"),
            "pending",
        )

    def test_rejected_muted(self) -> None:
        self.assertEqual(classify_studio_review("This sound was muted"), "rejected")

    def test_parse_picks_seed(self) -> None:
        rows = parse_studio_content_rows(SAMPLE)
        self.assertGreaterEqual(len(rows), 2)
        hit = pick_sound_row(rows, query="if i can earn", duration="00:20")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["review"], "pending")
        self.assertEqual(hit["privacy"], "Only me")


if __name__ == "__main__":
    unittest.main()
