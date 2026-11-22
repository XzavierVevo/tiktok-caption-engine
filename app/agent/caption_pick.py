"""Parse /all caption lists and ask a model to pick favorites."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .. import chat_models
from .. import usage as usage_mod
from . import providers

_log = logging.getLogger(__name__)

PICK_SYSTEM = (
    "You pick the best TikTok on-screen captions. "
    "Only real burn-in lines count — skip angle names, themes, and concepts "
    "(e.g. 'Social proof angle'). "
    "Judge punch, distinctiveness, and scroll-stopping power. "
    "Skip lines too long to read on a phone or that would sit under TikTok UI. "
    "Skip near-duplicates and filler. Reply with JSON only."
)

_CAPTION_PROMPT_RE = re.compile(
    r"""
    \b(captions?|hooks?|on[- ]?screen(?:\s+text)?|tiktok\s+(?:text|lines?)|
       one[- ]liners?|punchy)\b
    |
    \b(write|generate|create|brainstorm|give\s+me|come\s+up\s+with|make)\b
    .{0,120}\b(lines?|ideas?|options?|versions?|variations?)\b
    """,
    re.I | re.S | re.X,
)

_SKIP_LINE_RE = re.compile(
    r"""^(
        here\s+(are|is)\b |
        sure[,!]?\b |
        (of\s+course|absolutely)[,!]?\b |
        hope\s+(this|these)\b |
        let\s+me\s+know\b |
        want\s+me\b |
        would\s+you\b |
        (i('ve| have)\s+)?(written|generated|created|listed)\b |
        below\s+(are|is)\b |
        (these|the)\s+following\b |
        option\s+\d+\s*:?\s*$ |
        \#{1,6}\s |
        \*{0,2}(claude|gpt|grok|gemini|openai|anthropic|xai|all models)|
        video\s*\d+\s*:?\s*$ |
        source(id)?\s*[:\-] |
        type\s+/ |
        ^clip\s*: |
        ^search\s*: |
        \/preview\b |
        \/render\b |
        \/all\b |
        \/multiple\b |
        or\s+say\b |
        open(ing)?\s+the\s+(spreadsheet|plan)\b |
        saved?\s+\d+\s+captions?\b
    )""",
    re.I | re.X,
)

# Brainstorm labels / frameworks — not burn-in text.
_META_CAPTION_RE = re.compile(
    r"""
    \bangles?\b |
    \b(concepts?|themes?|approaches?|frameworks?|categories|hook\s+types?|
       idea\s+types?|strategies|tropes?)\b |
    ^(proof|receipts|social\s+proof|lifestyle(\s+flex)?|casual\s+discovery|
      skeptic([- ]to[- ]believer)?|time[- ]?value(\s+reframe)?|
      fomo|curiosity\s+gap|pattern\s+interrupt)\b |
    \b(reframe|discovery|flex|journey)\s*$
    """,
    re.I | re.X,
)

_LIST_PREFIX_RE = re.compile(
    r"""^\s*(?:
        (?:\d{1,3}[\.\)\:]\s+) |
        (?:[-*•–—]\s+) |
        (?:\[\s*[xX\s]?\s*\]\s+)
    )""",
    re.X,
)

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def looks_like_caption_prompt(prompt: str) -> bool:
    return bool(_CAPTION_PROMPT_RE.search(prompt or ""))


def _strip_markup(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^[*_`~]+|[*_`~]+$", "", s).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    if s.startswith("“") and s.endswith("”"):
        s = s[1:-1].strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _line_is_caption(text: str) -> bool:
    if len(text) < 2 or len(text) > 160:
        return False
    if _SKIP_LINE_RE.search(text):
        return False
    if _META_CAPTION_RE.search(text):
        return False
    if re.search(
        r"/preview\b|/render\b|/all\b|/multiple\b|spreadsheet|automate both|"
        r"type\s+\*{0,2}/",
        text,
        re.I,
    ):
        return False
    # "Label: actual caption" — keep only if the part after the colon looks real.
    if ":" in text and len(text) < 80:
        left, _, right = text.partition(":")
        left_l = left.strip().lower()
        if left_l.endswith("angle") or left_l in (
            "proof",
            "receipts",
            "social proof",
            "lifestyle",
            "flex",
            "concept",
            "theme",
            "approach",
        ):
            return False
    if text.startswith("http://") or text.startswith("https://"):
        return False
    return True


def extract_captions_from_text(text: str) -> list[str]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"```[\s\S]*?```", "\n", raw)
    listed: list[str] = []
    unlabeled: list[str] = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        prefixed = bool(_LIST_PREFIX_RE.match(stripped))
        body = _LIST_PREFIX_RE.sub("", stripped) if prefixed else stripped
        body = _strip_markup(body)
        if not _line_is_caption(body):
            continue
        if prefixed:
            listed.append(body)
        else:
            unlabeled.append(body)
    if listed:
        return listed
    if len(unlabeled) >= 3 and all(len(x) <= 120 for x in unlabeled):
        return unlabeled
    return []


def collect_caption_items(bots: list[dict], prompt: str = "") -> list[dict[str, Any]]:
    """Unique caption-like lines from /all bot replies, tagged with source."""
    items: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for bot in bots or []:
        if bot.get("error") or not (bot.get("content") or "").strip():
            continue
        label = bot.get("label") or bot.get("model") or bot.get("provider") or "Model"
        provider = bot.get("provider") or ""
        for text in extract_captions_from_text(bot.get("content") or ""):
            key = _norm_key(text)
            if not key:
                continue
            if key in seen:
                idx = seen[key]
                srcs = items[idx].setdefault("sources", [])
                if label not in srcs:
                    srcs.append(label)
                continue
            seen[key] = len(items)
            items.append(
                {
                    "id": f"c{len(items) + 1}",
                    "text": text,
                    "provider": provider,
                    "sources": [label],
                }
            )
    if len(items) < 2:
        return []
    if looks_like_caption_prompt(prompt):
        return items
    short = [i for i in items if len(i["text"]) <= 100]
    if len(short) >= 3 and len(short) >= 0.65 * len(items):
        return items
    return []


def _parse_indexes(raw: str, n: int, count: int) -> list[int]:
    blob = (raw or "").strip()
    data: Any = None
    fence = _FENCE_RE.search(blob)
    candidates = [fence.group(1).strip()] if fence else []
    candidates.append(blob)
    start = blob.find("{")
    end = blob.rfind("}")
    if start >= 0 and end > start:
        candidates.append(blob[start : end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
            break
        except json.JSONDecodeError:
            continue
    nums: list[int] = []
    if isinstance(data, dict):
        seq = data.get("indexes")
        if seq is None:
            seq = data.get("index")
        if isinstance(seq, list):
            for v in seq:
                try:
                    nums.append(int(v))
                except (TypeError, ValueError):
                    continue
    if not nums:
        for m in re.finditer(r"\d+", blob):
            try:
                nums.append(int(m.group(0)))
            except ValueError:
                continue
    if not nums:
        return []
    one_based = any(v == n for v in nums) or (min(nums) >= 1 and max(nums) <= n)
    out: list[int] = []
    seen: set[int] = set()
    for v in nums:
        idx = v - 1 if one_based else v
        if idx < 0 or idx >= n or idx in seen:
            continue
        seen.add(idx)
        out.append(idx)
        if len(out) >= count:
            break
    return out


def pick_favorites(
    uid: str,
    *,
    captions: list[str],
    count: int,
    model_id: str | None = None,
    prompt: str = "",
    thread_id: str = "",
) -> dict[str, Any]:
    lines = [str(c).strip() for c in captions if str(c).strip()]
    if len(lines) < 2:
        raise ValueError("Need at least two captions to pick from")
    want = max(1, min(int(count or 1), len(lines), 80))
    model = chat_models.resolve_model(model_id)
    numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    user = (
        f"Pick exactly {want} of your favorite TikTok on-screen captions "
        "from this numbered list. Prefer distinct, punchy, scroll-stopping hooks. "
        "Skip near-duplicates.\n"
    )
    if (prompt or "").strip():
        user += f"\nOriginal request:\n{(prompt or '').strip()[:800]}\n"
    user += f"\nCaptions:\n{numbered}\n\n"
    user += f'Reply with JSON only: {{"indexes": [1, 2, ...]}} using those list numbers. Pick {want}.'
    result = providers.chat_completion(
        provider=model["provider"],
        model=model["id"],
        messages=[{"role": "user", "content": user}],
        allow_tools=False,
        system=PICK_SYSTEM,
    )
    try:
        usage_mod.record_llm(
            uid,
            thread_id=thread_id,
            provider=model["provider"],
            model=model["id"],
            usage=(result or {}).get("usage"),
            kind="pick",
        )
    except Exception:
        _log.debug("usage record failed", exc_info=True)
    indexes = _parse_indexes(result.get("content") or "", len(lines), want)
    if not indexes:
        raise ValueError("The model did not return a usable list of favorites")
    picked = [lines[i] for i in indexes]
    return {
        "ok": True,
        "indexes": indexes,
        "captions": picked,
        "model": model["id"],
        "provider": model["provider"],
        "label": model.get("label") or model["id"],
        "count": len(picked),
    }


_SECTION_RE = re.compile(
    r"""^(?:\#{1,6}\s*)?(?:frame\s*)?(?P<num>\d+)\s*[:.\-)–—]?\s*
        |^(?:\#{1,6}\s*)?frame\s*(?P<fnum>\d+)\b
        |sourceid\s*[=:]\s*(?P<sid>[\w\-]+)
        """,
    re.I | re.X,
)

_SOURCE_ID_IN_LINE = re.compile(r"sourceid\s*[=:]\s*([^\s\|)\]\}]+)", re.I)


def _videos_from_attachments(attachments: list[dict] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("kind") or "source").lower()
        if kind not in ("source", "video", ""):
            continue
        sid = str(a.get("sourceId") or a.get("id") or "").strip()
        if not sid:
            continue
        name = str(a.get("name") or a.get("title") or sid).strip()
        out.append({"sourceId": sid, "name": name})
    return out


def parse_per_video_captions(
    text: str,
    videos: list[dict[str, str]],
) -> dict[str, list[str]]:
    """Map sourceId → caption lines from a model reply grouped by Frame/video."""
    if not videos:
        return {}
    by_id = {v["sourceId"]: v for v in videos}
    by_name = {v["name"].lower(): v["sourceId"] for v in videos if v.get("name")}
    order = [v["sourceId"] for v in videos]

    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    sections: list[tuple[str | None, list[str]]] = []
    current_sid: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_lines
        if current_lines:
            sections.append((current_sid, current_lines))
            current_lines = []

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        sid_m = _SOURCE_ID_IN_LINE.search(stripped)
        frame_m = re.match(
            r"^(?:#{1,6}\s*)?(?:frame|video)\s*(\d+)\b", stripped, re.I
        )
        is_heading = bool(
            re.match(r"^#{1,6}\s+", stripped)
            or (sid_m and len(stripped) < 120)
            or (frame_m and len(stripped) < 120 and not _LIST_PREFIX_RE.match(stripped))
        )
        if is_heading:
            _flush()
            current_sid = None
            if sid_m:
                cand = sid_m.group(1).strip().rstrip(".,;:")
                if cand in by_id:
                    current_sid = cand
            if current_sid is None and frame_m:
                idx = int(frame_m.group(1)) - 1
                if 0 <= idx < len(order):
                    current_sid = order[idx]
            if current_sid is None:
                low = stripped.lower()
                for name, sid in by_name.items():
                    if name and name in low:
                        current_sid = sid
                        break
            continue
        prefixed = bool(_LIST_PREFIX_RE.match(stripped))
        body = _LIST_PREFIX_RE.sub("", stripped) if prefixed else stripped
        body = _strip_markup(body)
        if not _line_is_caption(body):
            continue
        if prefixed or current_sid is not None:
            current_lines.append(body)
    _flush()

    # If no headings matched, treat all listed captions as a flat list for round-robin.
    if not sections:
        flat = extract_captions_from_text(text)
        if not flat:
            return {}
        out: dict[str, list[str]] = {sid: [] for sid in order}
        if len(order) == 1:
            out[order[0]] = flat
            return out
        # Round-robin across videos when the model dumped one big list.
        for i, cap in enumerate(flat):
            out[order[i % len(order)]].append(cap)
        return out

    out = {sid: [] for sid in order}
    orphan: list[str] = []
    untitled_idx = 0
    for sid, lines in sections:
        if sid and sid in out:
            out[sid].extend(lines)
        else:
            # Assign untitled sections in order.
            while untitled_idx < len(order) and out[order[untitled_idx]]:
                untitled_idx += 1
            if untitled_idx < len(order):
                out[order[untitled_idx]].extend(lines)
                untitled_idx += 1
            else:
                orphan.extend(lines)
    if orphan and order:
        out[order[0]].extend(orphan)
    return {k: v for k, v in out.items() if v}


def merge_bot_plans(
    bots: list[dict],
    videos: list[dict[str, str]],
    *,
    origin: str = "/all",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge multi-model per-video captions → (videos[{sourceId,captions}], captionDetails)."""
    if not videos:
        return [], []
    buckets: dict[str, list[tuple[str, str]]] = {v["sourceId"]: [] for v in videos}
    seen: dict[str, set[str]] = {v["sourceId"]: set() for v in videos}

    for bot in bots or []:
        if bot.get("error") or not (bot.get("content") or "").strip():
            continue
        label = str(bot.get("label") or bot.get("model") or bot.get("provider") or "Model")
        parsed = parse_per_video_captions(bot.get("content") or "", videos)
        if not parsed:
            # Fallback: flat extract assigned round-robin.
            flat = extract_captions_from_text(bot.get("content") or "")
            if not flat:
                continue
            parsed = parse_per_video_captions(
                "\n".join(f"{i+1}. {c}" for i, c in enumerate(flat)),
                videos,
            )
        for sid, caps in parsed.items():
            if sid not in buckets:
                continue
            for cap in caps:
                key = _norm_key(cap)
                if not key or key in seen[sid]:
                    continue
                seen[sid].add(key)
                buckets[sid].append((cap, label))

    videos_out: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    detail_seen: set[str] = set()
    for v in videos:
        sid = v["sourceId"]
        pairs = buckets.get(sid) or []
        if not pairs:
            continue
        videos_out.append(
            {
                "sourceId": sid,
                "captions": [
                    {
                        "text": cap,
                        "model": label,
                        "origin": origin,
                        "sources": [label],
                    }
                    for cap, label in pairs
                ],
            }
        )
        for cap, label in pairs:
            key = _norm_key(cap)
            if key in detail_seen:
                continue
            detail_seen.add(key)
            details.append(
                {
                    "text": cap,
                    "model": label,
                    "origin": origin,
                    "sources": [label],
                }
            )
    return videos_out, details
