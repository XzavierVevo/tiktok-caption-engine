"""Random-word constraint: a common word as a spark, not a second writing brief.

The assigned word can be any common word through about 10th-grade vocabulary.
The on-screen line still follows the hook brief in caption_markers: say paid
fyp and the money, and tag income. The line has to make sense on the clip.
"""

from __future__ import annotations

import random
import re
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_GRADE10 = _DIR / "grade10_words.txt"
_GRADE3 = _DIR / "grade3_words.txt"
_WORD_RE = re.compile(r"^[a-z]{3,12}$")

_STOP = frozenset(
    """
    a an the of to and in is it for on with as by at from or if but not you
    your we they this that those these be been being was were are am i me my
    he she his her its our their what which who how when where why can could
    should would will just about into over after before than then also only
    other some any more most such own same so too very get got make made like
    use used using go going went come came take took give gave see saw know
    knew think thought look looked want wanted have has had do does did
    the their them then there these they those though
    money cash dollar dollars paid pay paycheck bank rich gold
    """.split()
)

_FALLBACK = tuple(
    """
    piggy cookie rocket pillow sandwich dragon bicycle rainbow pancake
    puppy truck balloon kitchen pocket umbrella thunder magnet
    """.split()
)


def _clean(word: str) -> str | None:
    w = (word or "").strip().lower()
    if not _WORD_RE.match(w) or w in _STOP:
        return None
    return w


def _load_file(path: Path, seen: set[str], words: list[str]) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        w = _clean(line)
        if w and w not in seen:
            seen.add(w)
            words.append(w)


@lru_cache(maxsize=1)
def english_words() -> tuple[str, ...]:
    """Common English content words through ~10th-grade reading."""
    words: list[str] = []
    seen: set[str] = set()
    _load_file(_GRADE10, seen, words)
    _load_file(_GRADE3, seen, words)
    if len(words) < 50:
        for raw in _FALLBACK:
            c = _clean(raw)
            if c and c not in seen:
                seen.add(c)
                words.append(c)
    return tuple(words)


def pick_random_words(n: int, *, rng: random.Random | None = None) -> list[str]:
    """n unique words from the 10th-grade-or-below lexicon."""
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return []
    pool = list(english_words())
    if not pool:
        return []
    r = rng or random.Random()
    if count <= len(pool):
        return r.sample(pool, count)
    out = list(pool)
    r.shuffle(out)
    while len(out) < count:
        out.append(r.choice(pool))
    return out[:count]


def random_word_brief(words: list[str], clip_notes: list[str] | None = None) -> str:
    """Short spark list. The word is inspiration; the hook brief still rules."""
    lines = [
        "Word = inspiration only. Do not force it into the line.",
        "Use it as a spark for a visible action, like running or a dance.",
        "If it does not become an action, drop it. Never make it a person.",
        "Follow the hook brief. Say paid fyp and the money. Tag income [[uncompliant:income]]. Never tiktok.",
        "Words:",
    ]
    notes = list(clip_notes or [])
    for i, w in enumerate(words, 1):
        extra = ""
        if i - 1 < len(notes) and notes[i - 1].strip():
            extra = f"  | CLIP: {notes[i - 1].strip()}"
        lines.append(f"  {i}. {w}{extra}")
    return "\n".join(lines)
