"""Parse and recover TikTok post URLs after upload.

HTTP publish often returns ``status_code: 0`` with no ``item_id`` / ``share_url``.
When that happens we look up the account's latest public posts and pick the new
video so the render catalog can store a durable URL (and later a Spark code).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

_log = logging.getLogger(__name__)

_ITEM_ID_RE = re.compile(r"^\d{15,20}$")
_VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?tiktok\.com/@([\w.\-]+)/(?:video|photo)/(\d{15,20})",
    re.I,
)
_BARE_VIDEO_RE = re.compile(
    r"https?://(?:www\.|m\.)?tiktok\.com/(?:video|photo)/(\d{15,20})",
    re.I,
)
_PHOTO_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?tiktok\.com/@([\w.\-]+)/photo/(\d{15,20})",
    re.I,
)
_ITEM_KEYS = frozenset(
    {"item_id", "aweme_id", "awemeId", "itemId", "awemeid"}
)
_URL_KEYS = frozenset(
    {"share_url", "shareUrl", "share_link", "shareLink", "video_url", "videoUrl"}
)
_SKIP_WALK_KEYS = frozenset(
    {"log_pb", "logPb", "impr_id", "imprId", "upload_param"}
)
_JSON_MARKERS = (
    "__UNIVERSAL_DATA_FOR_REHYDRATION__",
    "SIGI_STATE",
    "__NEXT_DATA__",
)


def video_id_of(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    match = _VIDEO_URL_RE.search(raw)
    if match:
        return match.group(2)
    match = _BARE_VIDEO_RE.search(raw)
    if match:
        return match.group(1)
    if _ITEM_ID_RE.match(raw):
        return raw
    try:
        from . import duoplus as dp

        return dp.tiktok_video_id(raw)
    except Exception:
        return ""


def post_kind_of(url: str) -> str:
    raw = (url or "").strip()
    if _PHOTO_URL_RE.search(raw) or "/photo/" in raw.lower():
        return "photo"
    return "video"


def canonical_post_url(url: str, account_name: str = "", *, force_kind: str = "") -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        from . import duoplus as dp

        built = dp.canonical_post_url_for_duoplus(raw, account_name or "")
        want = (force_kind or "").strip().lower()
        if want in ("photo", "video"):
            vid = video_id_of(built)
            handle = (account_name or "").strip().lstrip("@")
            m = re.search(r"tiktok\.com/@([^/]+)/", built, re.I)
            if m:
                handle = m.group(1)
            if vid and handle:
                return f"https://www.tiktok.com/@{handle}/{want}/{vid}"
        return built
    except Exception:
        vid = video_id_of(raw)
        handle = (account_name or "").strip().lstrip("@")
        kind = (force_kind or "").strip().lower()
        if kind not in ("photo", "video"):
            kind = "photo" if post_kind_of(raw) == "photo" else "video"
        if vid and handle:
            return f"https://www.tiktok.com/@{handle}/{kind}/{vid}"
        if vid:
            return f"https://www.tiktok.com/{kind}/{vid}"
        return raw.split("?")[0].split("#")[0].rstrip("/")


def extract_ids_from_publish_json(data: Any) -> tuple[str, str]:
    """Return ``(item_id, post_url)`` from a TikTok web publish response."""
    found_id = ""
    found_url = ""

    def consider_url(value: str) -> None:
        nonlocal found_id, found_url
        text = (value or "").strip()
        if "tiktok.com" not in text.lower():
            return
        found_url = found_url or text
        vid = video_id_of(text)
        if vid:
            found_id = found_id or vid

    def consider_id(value: Any) -> None:
        nonlocal found_id, found_url
        text = str(value or "").strip()
        if text.startswith("http"):
            consider_url(text)
            return
        if _ITEM_ID_RE.match(text):
            found_id = found_id or text

    def walk(node: Any, depth: int = 0) -> None:
        if found_id and found_url:
            return
        if depth > 8 or node is None:
            return
        if isinstance(node, dict):
            for key, val in node.items():
                k = str(key)
                if k in _SKIP_WALK_KEYS:
                    continue
                if k in _URL_KEYS and isinstance(val, str):
                    consider_url(val)
                elif k in _ITEM_KEYS:
                    consider_id(val)
                elif isinstance(val, (dict, list)):
                    walk(val, depth + 1)
                elif isinstance(val, str) and "tiktok.com" in val.lower() and (
                    "/video/" in val.lower() or "/photo/" in val.lower()
                ):
                    consider_url(val)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, str) and "tiktok.com" in node.lower() and (
            "/video/" in node.lower() or "/photo/" in node.lower()
        ):
            consider_url(node)

    walk(data)
    if found_id and not found_url:
        kind = post_kind_of(found_url) if found_url else "video"
        found_url = f"https://www.tiktok.com/{kind}/{found_id}"
    return found_id, found_url


def _unique_posts(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        vid = str(row.get("id") or "")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append(row)
    return out


def posts_from_json(node: Any, *, handle: str = "") -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def walk(cur: Any, depth: int = 0) -> None:
        if depth > 10 or cur is None:
            return
        if isinstance(cur, dict):
            vid = str(
                cur.get("aweme_id")
                or cur.get("itemId")
                or cur.get("item_id")
                or cur.get("id")
                or ""
            )
            video_hint = any(
                key in cur
                for key in (
                    "desc",
                    "share_url",
                    "shareUrl",
                    "video",
                    "bitrateInfo",
                    "videoMeta",
                    "stats",
                    "statistics",
                )
            )
            if _ITEM_ID_RE.match(vid) and video_hint:
                desc = str(
                    cur.get("desc")
                    or cur.get("description")
                    or cur.get("title")
                    or cur.get("text")
                    or ""
                )
                author = handle
                user = cur.get("author") or cur.get("authorInfo") or {}
                if isinstance(user, dict):
                    author = str(
                        user.get("uniqueId") or user.get("unique_id") or author
                    )
                url = str(cur.get("share_url") or cur.get("shareUrl") or "")
                kind = "photo" if "/photo/" in url.lower() else ""
                found.append(
                    {
                        "id": vid,
                        "desc": desc,
                        "author": author.lstrip("@"),
                        "url": url,
                        "kind": kind,
                    }
                )
            for val in cur.values():
                if isinstance(val, (dict, list)):
                    walk(val, depth + 1)
        elif isinstance(cur, list):
            for item in cur:
                walk(item, depth + 1)

    walk(node)
    return _unique_posts(found)


def _extract_script_json(html: str, marker: str) -> Any | None:
    idx = html.find(marker)
    if idx < 0:
        return None
    # Include chars before the marker so id="MARKER" still matches.
    slice_ = html[max(0, idx - 96) : idx + 2_000_000]
    # <script id="MARKER" type="application/json">...</script>
    tagged = re.search(
        rf'id=["\']{re.escape(marker)}["\'][^>]*>\s*(\{{)',
        slice_,
        re.I,
    )
    start = -1
    if tagged:
        start = tagged.start(1)
    else:
        assigned = re.search(
            rf"{re.escape(marker)}\s*=\s*(\{{)",
            slice_,
        )
        if assigned:
            start = assigned.start(1)
    if start < 0:
        return None
    blob = slice_[start:]
    decoder = json.JSONDecoder()
    try:
        return decoder.raw_decode(blob)[0]
    except json.JSONDecodeError:
        return None


def posts_from_profile_html(html: str, *, handle: str = "") -> list[dict[str, str]]:
    handle = (handle or "").strip().lstrip("@")
    found: list[dict[str, str]] = []
    text = html or ""
    for marker in _JSON_MARKERS:
        blob = _extract_script_json(text, marker)
        if blob is not None:
            found.extend(posts_from_json(blob, handle=handle))
    if handle:
        pattern = re.compile(
            rf"/@{re.escape(handle)}/(video|photo)/(\d{{15,20}})",
            re.I,
        )
    else:
        pattern = re.compile(r"/@[\w.\-]+/(video|photo)/(\d{15,20})", re.I)
    by_id = {row["id"]: row for row in found}
    for match in pattern.finditer(text):
        kind = match.group(1).lower()
        vid = match.group(2)
        if vid in by_id:
            if kind == "photo" and "/photo/" not in (by_id[vid].get("url") or "").lower():
                by_id[vid]["kind"] = "photo"
            continue
        author = handle
        if not author:
            full = match.group(0)
            am = re.search(r"/@([\w.\-]+)/(?:video|photo)/", full)
            author = am.group(1) if am else ""
        found.append(
            {"id": vid, "desc": "", "author": author, "url": "", "kind": kind}
        )
        by_id[vid] = found[-1]
    for row in found:
        author = (row.get("author") or handle).lstrip("@")
        existing = str(row.get("url") or "")
        kind = str(row.get("kind") or "").lower()
        if "/photo/" in existing.lower() or kind == "photo":
            kind = "photo"
        else:
            kind = "video"
        if not existing:
            if author:
                row["url"] = f"https://www.tiktok.com/@{author}/{kind}/{row['id']}"
            else:
                row["url"] = f"https://www.tiktok.com/{kind}/{row['id']}"
        elif kind == "photo" and "/video/" in existing.lower():
            row["url"] = existing.replace("/video/", "/photo/")
    return _unique_posts(found)


def _session_from_cookies(
    cookies_path: str | Path | None,
    *,
    session: requests.Session | None = None,
) -> requests.Session | None:
    if session is not None:
        return session
    if not cookies_path:
        return None
    path = Path(cookies_path)
    if not path.is_file():
        return None
    try:
        from .browser_stealth import default_user_agent
        from .tiktok_engines.http_session import build_session

        return build_session(path, user_agent=default_user_agent())
    except Exception:
        _log.debug("could not build cookie session for post lookup", exc_info=True)
        return None


def list_recent_posts(
    handle: str,
    *,
    session: requests.Session | None = None,
    cookies_path: str | Path | None = None,
) -> list[dict[str, str]]:
    name = (handle or "").strip().lstrip("@")
    if not name:
        return []
    http = _session_from_cookies(cookies_path, session=session)
    url = f"https://www.tiktok.com/@{name}"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    try:
        if http is not None:
            resp = http.get(url, headers=headers, timeout=20, allow_redirects=True)
        else:
            from .browser_stealth import default_user_agent

            resp = requests.get(
                url,
                headers={**headers, "User-Agent": default_user_agent()},
                timeout=20,
                allow_redirects=True,
            )
    except requests.RequestException:
        _log.debug("profile fetch failed for @%s", name, exc_info=True)
        return []
    if not resp.ok:
        _log.debug("profile fetch HTTP %s for @%s", resp.status_code, name)
        return []
    return posts_from_profile_html(resp.text or "", handle=name)


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _desc_matches(desc: str, caption: str) -> bool:
    a = _norm_text(desc)
    b = _norm_text(caption)
    if not a or not b:
        return False
    if a == b:
        return True
    if b in a or a in b:
        return True
    prefix = b[:40]
    return bool(prefix) and a.startswith(prefix)


def pick_new_post(
    posts: list[dict[str, str]],
    *,
    caption: str = "",
    exclude_ids: set[str] | None = None,
    require_match: bool = False,
) -> str:
    skip = {str(x) for x in (exclude_ids or set()) if str(x).strip()}
    unknown = [p for p in posts if str(p.get("id") or "") not in skip]
    if not unknown:
        return ""
    cap = (caption or "").strip()
    if cap:
        matched = [p for p in unknown if _desc_matches(str(p.get("desc") or ""), cap)]
        if matched:
            return str(matched[0].get("url") or "")
    if require_match:
        return ""
    return str(unknown[0].get("url") or "")


def recover_post_url(
    handle: str,
    *,
    caption: str = "",
    exclude_ids: set[str] | None = None,
    session: requests.Session | None = None,
    cookies_path: str | Path | None = None,
    require_match: bool = False,
    attempts: int = 5,
    delay_s: float = 2.0,
) -> str:
    """Find a newly posted video URL for ``handle``. Returns canonical URL or ""."""
    name = (handle or "").strip().lstrip("@")
    if not name:
        return ""
    tries = max(1, int(attempts or 1))
    wait = max(0.0, float(delay_s or 0))
    last = ""
    for i in range(tries):
        if i and wait:
            time.sleep(wait)
        posts = list_recent_posts(
            name, session=session, cookies_path=cookies_path
        )
        raw = pick_new_post(
            posts,
            caption=caption,
            exclude_ids=exclude_ids,
            require_match=require_match,
        )
        if raw:
            url = canonical_post_url(raw, name)
            if url:
                return url
        last = raw
    return canonical_post_url(last, name) if last else ""
