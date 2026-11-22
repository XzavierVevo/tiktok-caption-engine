"""Analyze a public TikTok post/ad URL for creative reference.

Uses Scrape Creators (same key as Pinterest) for description / cover / transcript.
The agent then invents Pinterest search queries and caption variations from that.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests

_log = logging.getLogger(__name__)

SCRAPE_BASE = "https://api.scrapecreators.com"
REQUEST_TIMEOUT = 60
_HASHTAG_RE = re.compile(r"#([\w]+)", re.UNICODE)
_TIKTOK_HOSTS = ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")


class TikTokAnalyzeError(RuntimeError):
    pass


def api_key() -> str:
    return (os.environ.get("SCRAPE_CREATORS_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise TikTokAnalyzeError(
            "TikTok ad analysis is not configured. Set SCRAPE_CREATORS_API_KEY in .env."
        )
    return {"x-api-key": key}


def is_tiktok_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return False
    try:
        host = urlparse(raw).netloc.lower().removeprefix("www.")
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _TIKTOK_HOSTS)


def _first_url(node: Any) -> str:
    if isinstance(node, dict):
        urls = node.get("url_list") or node.get("urlList") or []
        if isinstance(urls, list):
            for u in urls:
                s = str(u or "").strip()
                if s.startswith("http"):
                    return s
        for key in ("url", "uri"):
            s = str(node.get(key) or "").strip()
            if s.startswith("http"):
                return s
    elif isinstance(node, list):
        for item in node:
            hit = _first_url(item)
            if hit:
                return hit
    elif isinstance(node, str) and node.startswith("http"):
        return node
    return ""


def _cover_urls(aweme: dict) -> list[str]:
    video = aweme.get("video") if isinstance(aweme.get("video"), dict) else {}
    out: list[str] = []
    for key in (
        "origin_cover",
        "cover",
        "dynamic_cover",
        "ai_dynamic_cover",
        "animated_cover",
    ):
        u = _first_url(video.get(key))
        if u and u not in out:
            out.append(u)
    return out


def _hashtags(aweme: dict, desc: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for m in _HASHTAG_RE.finditer(desc or ""):
        t = m.group(1).lower()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    extras = aweme.get("text_extra") or aweme.get("textExtra") or []
    if isinstance(extras, list):
        for row in extras:
            if not isinstance(row, dict):
                continue
            name = str(row.get("hashtag_name") or row.get("hashtagName") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                tags.append(name.lower())
    return tags[:20]


def _clean_transcript(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "WEBVTT" in text[:40].upper() or "-->" in text:
        lines: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.upper().startswith("WEBVTT") or "-->" in s or s.isdigit():
                continue
            if re.match(r"^\d{2}:\d{2}", s):
                continue
            lines.append(s)
        return " ".join(lines).strip()
    return " ".join(text.split())


def _suggest_queries(desc: str, hashtags: list[str], author: str) -> list[str]:
    """Lightweight visual keyword seeds — the model should refine after vision."""
    seeds: list[str] = []
    # Drop @handles and hashtags from desc for a short phrase.
    cleaned = _HASHTAG_RE.sub(" ", desc or "")
    cleaned = re.sub(r"@[\w.]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    words = [w for w in cleaned.split() if len(w) > 2][:8]
    if len(words) >= 3:
        seeds.append(" ".join(words[:5]))
    for tag in hashtags[:4]:
        if tag not in ("fyp", "foryou", "viral", "tiktok"):
            seeds.append(tag.replace("_", " "))
    # Dedup preserve order
    out: list[str] = []
    seen: set[str] = set()
    for q in seeds:
        key = q.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:5]


def fetch_video_info(url: str, *, get_transcript: bool = False) -> dict[str, Any]:
    """Return a slim creative brief for a public TikTok URL."""
    target = (url or "").strip()
    if not is_tiktok_url(target):
        raise TikTokAnalyzeError("Not a TikTok URL")
    params: dict[str, Any] = {"url": target}
    if get_transcript:
        params["get_transcript"] = "true"
    try:
        resp = requests.get(
            f"{SCRAPE_BASE}/v2/tiktok/video",
            headers=_headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TikTokAnalyzeError(f"TikTok lookup failed: {exc}") from exc
    if resp.status_code != 200:
        raise TikTokAnalyzeError(
            f"TikTok lookup failed (HTTP {resp.status_code}): {(resp.text or '')[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise TikTokAnalyzeError("TikTok lookup returned invalid JSON") from exc
    aweme = data.get("aweme_detail") if isinstance(data, dict) else None
    if not isinstance(aweme, dict):
        aweme = data.get("aweme") if isinstance(data, dict) else None
    if not isinstance(aweme, dict):
        raise TikTokAnalyzeError("Could not read TikTok video details for that URL")

    author_obj = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    author = str(
        author_obj.get("unique_id")
        or author_obj.get("uniqueId")
        or author_obj.get("nickname")
        or ""
    ).strip()
    desc = str(aweme.get("desc") or aweme.get("description") or "").strip()
    aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "").strip()
    duration_ms = aweme.get("duration") or 0
    try:
        duration_s = float(duration_ms) / 1000.0 if float(duration_ms) > 100 else float(duration_ms)
    except (TypeError, ValueError):
        duration_s = 0.0
    music = aweme.get("music") if isinstance(aweme.get("music"), dict) else {}
    music_title = str(music.get("title") or music.get("id_str") or "").strip()
    hashtags = _hashtags(aweme, desc)
    covers = _cover_urls(aweme)
    transcript = ""
    for key in ("transcript", "video_transcript", "asr"):
        if data.get(key):
            transcript = _clean_transcript(str(data.get(key) or ""))
            break
        if aweme.get(key):
            transcript = _clean_transcript(str(aweme.get(key) or ""))
            break
    share = f"https://www.tiktok.com/@{author}/video/{aweme_id}" if author and aweme_id else target

    return {
        "ok": True,
        "url": share,
        "inputUrl": target,
        "awemeId": aweme_id,
        "author": author,
        "description": desc,
        "hashtags": hashtags,
        "durationSeconds": round(duration_s, 2) if duration_s else 0,
        "musicTitle": music_title,
        "transcript": transcript,
        "coverUrls": covers,
        "suggestedQueries": _suggest_queries(desc, hashtags, author),
        "creditsCharged": data.get("credits_charged") if isinstance(data, dict) else None,
    }


def analyze_example_ad(
    url: str,
    *,
    get_transcript: bool = False,
) -> dict[str, Any]:
    brief = fetch_video_info(url, get_transcript=get_transcript)
    # Vision: prefer cover stills so the model can invent visual Pinterest queries.
    vision = list(brief.get("coverUrls") or [])[:3]
    brief["_visionUrls"] = vision
    brief["hint"] = (
        "Use this as a creative reference. Invent short visual Pinterest search "
        "queries from the cover + description (not the whole caption). Then "
        "search_pinterest → pinterest_set show for the requested image count, "
        "and write the requested number of finished caption variations."
    )
    return brief
