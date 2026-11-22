"""Compliance tags stay in stored copy; burn/upload peel tags only."""

from __future__ import annotations

import unittest

from app.agent.providers import CAPTION_WRITING_RULES, COMPACT_SYSTEM_PROMPT
from app.caption_markers import HOOK_CREATIVE_RULES, strip_markers
from app.layout import fit_caption
from app.tiktok_upload import render_description


class CaptionMarkerTests(unittest.TestCase):
    def test_keeps_inner_words(self) -> None:
        raw = "This app [[uncompliant]]guarantees $400 a day[[/uncompliant]] from your phone"
        self.assertEqual(
            strip_markers(raw),
            "This app guarantees $400 a day from your phone",
        )

    def test_html_and_xml_wraps(self) -> None:
        self.assertEqual(
            strip_markers("Get <uncompliant>rich overnight</uncompliant> with this"),
            "Get rich overnight with this",
        )
        self.assertEqual(
            strip_markers("Try <!-- uncompliant -->this phrase<!-- /uncompliant --> now"),
            "Try this phrase now",
        )

    def test_typo_uncomplaint_and_reason(self) -> None:
        raw = "Earn [[uncomplaint:income]]passive cash[[/uncomplaint]] this week"
        self.assertEqual(strip_markers(raw), "E*rn passive c*sh this week")

    def test_leftover_tags_and_comments(self) -> None:
        self.assertEqual(
            strip_markers("Hook here [[uncompliant]] [[/uncompliant]]"),
            "Hook here",
        )
        self.assertEqual(
            strip_markers("this is uncompliant: still the hook"),
            "still the hook",
        )
        self.assertEqual(
            strip_markers("still the hook (uncompliant)"),
            "still the hook",
        )

    def test_swear_is_blurred_a_little(self) -> None:
        self.assertEqual(
            strip_markers("put the fucking phone down"),
            "put the f*cking phone down",
        )
        self.assertEqual(strip_markers("what the SHIT"), "what the S*IT")
        self.assertEqual(strip_markers("class photo"), "class photo")
        self.assertEqual(
            strip_markers("[[uncompliant]]fucking[[/uncompliant]] paid"),
            "f*cking p*id",
        )

    def test_money_tokens_hide_one_letter(self) -> None:
        self.assertEqual(strip_markers("count the money"), "count the m*ney")
        self.assertEqual(strip_markers("a fan of CASH"), "a fan of C*SH")
        self.assertEqual(
            strip_markers("all I did was scroll"),
            "all I did was s*roll",
        )
        self.assertEqual(
            strip_markers(
                "grandma out-earned the whole house. paid to play video games"
            ),
            "grandma out-e*rned the whole house. p*id to play video games",
        )

    def test_plain_copy_unchanged(self) -> None:
        line = "I made $400 from my phone today"
        self.assertEqual(strip_markers(line), line)

    def test_fit_caption_does_not_burn_tags(self) -> None:
        lines, _size, _fitted = fit_caption(
            "Save [[uncompliant]]time[[/uncompliant]] tonight",
            1080,
            1920,
            {},
        )
        joined = " ".join(lines)
        self.assertIn("Save time tonight", joined)
        self.assertNotIn("uncompliant", joined.lower())
        self.assertNotIn("[[", joined)

    def test_creative_brief_leads_caption_prompt(self) -> None:
        self.assertTrue(CAPTION_WRITING_RULES.startswith("BIGGEST JOB"))
        self.assertIn("CLIP IDEA", CAPTION_WRITING_RULES)
        self.assertIn("unfiltered", CAPTION_WRITING_RULES.lower())
        self.assertIn("TikTok-ey", CAPTION_WRITING_RULES)
        self.assertIn("past captions", CAPTION_WRITING_RULES.lower())
        self.assertIn(HOOK_CREATIVE_RULES, COMPACT_SYSTEM_PROMPT)

    def test_tiktok_description_strips_tags(self) -> None:
        out = render_description(
            "{caption}",
            caption="Install [[uncompliant]]now[[/uncompliant]]",
        )
        self.assertEqual(out, "Install now")


if __name__ == "__main__":
    unittest.main()
