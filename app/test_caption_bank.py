"""Per-offer caption bank: winners + history, newest unique first."""

from __future__ import annotations

import unittest

import json

from app.hooks import HooksError, _build_prompt, _dedupe_caption_bank, _parse_hooks_json


class CaptionBankTests(unittest.TestCase):
    def test_newest_unique(self) -> None:
        bank = _dedupe_caption_bank(
            [
                (1.0, "wait tiktok pays me now??"),
                (3.0, "i been on fyp unpaid this whole time"),
                (2.0, "Wait tiktok pays me now??"),
                (4.0, "the starter offer is literally homework"),
            ],
            limit=10,
        )
        self.assertEqual(
            bank,
            [
                "the starter offer is literally homework",
                "i been on fyp unpaid this whole time",
                "Wait tiktok pays me now??",
            ],
        )

    def test_prompt_includes_past_captions(self) -> None:
        _system, user = _build_prompt(
            {"name": "Reco", "url": "", "notes": "", "winners": [], "liked": []},
            5,
            "new_angles",
            "",
            "",
            past_captions=["i been on fyp unpaid this whole time"],
        )
        self.assertIn("PAST CAPTIONS", user)
        self.assertIn("i been on fyp unpaid this whole time", user)
        self.assertIn("Claude Opus", _system)

    def test_reco_requires_paid_fyp(self) -> None:
        _system, user = _build_prompt(
            {
                "name": "Reco",
                "url": "",
                "notes": "Get paid per minute on TikTok",
                "winners": [],
                "liked": [],
            },
            5,
            "new_angles",
            "",
            "",
            past_captions=[],
        )
        self.assertIn("paid fyp", user)

    def test_random_word_brief_in_prompt(self) -> None:
        _system, user = _build_prompt(
            {"name": "Reco", "url": "", "notes": "", "winners": [], "liked": []},
            2,
            "new_angles",
            "",
            "",
            past_captions=[],
            random_words=["piggy", "cookie"],
        )
        self.assertIn("piggy", user)
        self.assertIn("cookie", user)
        self.assertIn("paid fyp", user)
        self.assertIn("inspiration only", user)
        self.assertIn("DECISION GATE", user)
        self.assertIn("connect:", user)
        self.assertIn("decide:", user)
        self.assertIn("not the button", user)
        self.assertIn("showed them paid fyp", user)
        self.assertIn("[[uncompliant:income]]", user)
        self.assertIn("do not rewrite it to be compliant", user)
        self.assertIn("just to turn on paid fyp", user)
        self.assertIn("nagger", user)
        self.assertIn("Do not use a slur", user)
        self.assertIn("ONE PUNCH", user)
        self.assertIn("Do not narrate the action", user)
        self.assertIn("Do not name the emotion", user)

    def test_parse_keeps_only_lines_that_decide(self) -> None:
        raw = json.dumps(
            {
                "hooks": [
                    "my phone fits in one hand but paid fyp was off that whole hand",
                    {
                        "text": "paid fyp",
                        "connect": "yes",
                        "decide": "maybe",
                    },
                    {
                        "text": "i sat in the car after work and scrolled. paid fyp was off that whole time",
                        "connect": "The car scroll is the exact time paid fyp was off, so that drive counted for nothing.",
                        "decide": "They turn paid fyp on before the next scroll so the drive counts.",
                    },
                    {
                        "text": "paid fyp is what makes those minutes pay. mine was off",
                        "connect": "The night scroll is the thing that pays and theirs was off so the hours paid nothing.",
                        "decide": "They check paid fyp because the line says the minutes pay and theirs was off.",
                    },
                    {
                        "text": "nah my sister got paid for that same scroll and i had paid fyp off",
                        "connect": "Her getting paid for the scroll they both did, crossed with paid fyp being the reason he didn't.",
                        "decide": "He feels behind his sister and checks paid fyp before he scrolls again.",
                    },
                    {
                        "text": "she got paid to scroll. i didn't. paid fyp",
                        "connect": "She got paid to scroll and he didn't, so paid fyp is the thing he'd check.",
                        "decide": "He checks paid fyp because she got paid and he didn't.",
                    },
                    {
                        "text": "cab driver had paid fyp on gettin paid",
                        "connect": "A cab driver had the setting and got paid, which is just a person.",
                        "decide": "Nobody pictures an action, so nobody checks paid fyp.",
                    },
                    {
                        "text": "all my homies running to turn on paid fyp but me",
                        "connect": "The run shows paid fyp but the but-me ending does nothing.",
                        "decide": "The left-out ending is not why anyone checks paid fyp.",
                    },
                    {
                        "text": "all my homies running to turn on paid fyp",
                        "connect": "The run is the mechanism that shows they found paid fyp.",
                        "decide": "You see them sprint and you want to turn paid fyp on too.",
                    },
                ]
            }
        )
        hooks = _parse_hooks_json(raw, 5, reco=True)
        self.assertIn("all my homies running to turn on paid fyp", hooks)
        self.assertNotIn("all my homies running to turn on paid fyp but me", hooks)
        self.assertNotIn("cab driver had paid fyp on gettin paid", hooks)
        self.assertEqual(sum("running" in h for h in hooks), 1)

    def test_parse_rejects_a_draft_with_no_decision(self) -> None:
        raw = json.dumps(
            {"hooks": ["i was the last one following along with paid fyp still off"]}
        )
        with self.assertRaises(HooksError):
            _parse_hooks_json(raw, 5)


if __name__ == "__main__":
    unittest.main()
