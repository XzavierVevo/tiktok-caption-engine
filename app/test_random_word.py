from __future__ import annotations

import random
import unittest

from app.random_word import english_words, pick_random_words, random_word_brief


class RandomWordTests(unittest.TestCase):
    def test_lexicon_is_content_words(self) -> None:
        words = english_words()
        self.assertGreaterEqual(len(words), 20)
        self.assertTrue(all(w.isalpha() and w == w.lower() for w in words))
        self.assertNotIn("the", words)

    def test_pick_is_deterministic_with_seed(self) -> None:
        a = pick_random_words(10, rng=random.Random(7))
        b = pick_random_words(10, rng=random.Random(7))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 10)
        self.assertEqual(len(set(a)), 10)

    def test_brief_is_short_and_inspiration_only(self) -> None:
        text = random_word_brief(["piggy", "cookie"])
        self.assertLess(len(text.splitlines()), 12)
        self.assertIn("1. piggy", text)
        self.assertIn("2. cookie", text)
        self.assertIn("inspiration only", text)
        self.assertIn("paid fyp", text)
        self.assertNotIn("gobblin", text)

    def test_lexicon_is_grade10_not_dictionary_junk(self) -> None:
        words = set(english_words())
        self.assertIn("piggy", words)
        self.assertNotIn("schizothymic", words)
        self.assertNotIn("chromatium", words)
        self.assertTrue(all(3 <= len(w) <= 12 for w in words))

    def test_brief_can_attach_clip_notes(self) -> None:
        text = random_word_brief(["thunder"], clip_notes=["people sprinting"])
        self.assertIn("people sprinting", text)


if __name__ == "__main__":
    unittest.main()
