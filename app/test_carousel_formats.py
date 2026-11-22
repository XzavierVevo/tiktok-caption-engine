from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent import tools as agent_tools
from app.carousel_formats import (
    CarouselFormatError,
    likely_screen_record,
    normalize_post_url,
)


class CarouselFormatsTests(unittest.TestCase):
    def test_normalize_instagram_and_tiktok(self) -> None:
        ig = normalize_post_url("https://www.instagram.com/reel/DeS-G2VvsvL/?igsh=abc")
        self.assertEqual(ig, "https://www.instagram.com/reel/DeS-G2VvsvL")
        tt = normalize_post_url("https://www.tiktok.com/@x/video/1")
        self.assertIn("tiktok.com", tt)

    def test_reject_other_hosts(self) -> None:
        with self.assertRaises(CarouselFormatError):
            normalize_post_url("https://example.com/post")

    def test_likely_reaction_length(self) -> None:
        self.assertTrue(likely_screen_record(42))
        self.assertFalse(likely_screen_record(8))
        self.assertTrue(likely_screen_record(None))

    def test_tool_is_registered(self) -> None:
        names = {d["name"] for d in agent_tools.TOOL_DEFS}
        self.assertIn("find_carousel_formats", names)

    def test_chat_exposes_tool_for_format_hunt(self) -> None:
        names = agent_tools.chat_tool_names(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "find more carousel formats like wrd2jon",
                    }
                ]
            }
        )
        self.assertIn("find_carousel_formats", names)

    def test_execute_requires_url(self) -> None:
        class Ctx:
            thread_id = ""

        out = agent_tools.execute("test-uid", "find_carousel_formats", {}, Ctx())
        self.assertFalse(out.get("ok", True))
        self.assertIn("profileUrl", str(out.get("error") or ""))

    def test_list_posts_parses_ytdlp_print(self) -> None:
        printed = (
            "id1\thttps://www.instagram.com/reel/AAA\tGym carousel\t41.2\n"
            "id2\thttps://www.instagram.com/reel/BBB\tTalking\tNA\n"
        )

        class Proc:
            returncode = 0
            stdout = printed
            stderr = ""

        with patch("app.carousel_formats.subprocess.run", return_value=Proc()):
            from app.carousel_formats import list_post_urls

            rows = list_post_urls("https://www.instagram.com/wrd2jon/", limit=8)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["duration"], 41.2)
        self.assertIsNone(rows[1]["duration"])


if __name__ == "__main__":
    unittest.main()
