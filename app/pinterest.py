"""Pinterest video search via Scrape Creators. Downloads still use yt-dlp."""

from __future__ import annotations

import base64
import logging
import os
import re
from io import BytesIO
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests

_log = logging.getLogger(__name__)

SCRAPE_BASE = "https://api.scrapecreators.com"
DEFAULT_LIMIT = 12
MAX_LIMIT = 20
MAX_PAGES = 3
REQUEST_TIMEOUT = 45


class PinterestError(RuntimeError):
    pass


def api_key() -> str:
    return (os.environ.get("SCRAPE_CREATORS_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise PinterestError(
            "Pinterest search is not configured. Set SCRAPE_CREATORS_API_KEY in .env."
        )
    return {"x-api-key": key}


def _raise_http(resp: requests.Response) -> None:
    if resp.status_code == 401:
        raise PinterestError("Scrape Creators API key is invalid.")
    if resp.status_code == 402:
        raise PinterestError("Scrape Creators is out of credits.")
    if resp.status_code == 403:
        raise PinterestError("Pinterest blocked this search.")
    if resp.status_code == 404:
        raise PinterestError("No Pinterest results found.")
    try:
        payload = resp.json()
        msg = payload.get("message") or payload.get("error") or resp.text
    except Exception:
        msg = resp.text or f"HTTP {resp.status_code}"
    raise PinterestError(f"Pinterest search failed ({resp.status_code}): {str(msg)[:240]}")


def _video_lists(pin: dict) -> list[dict]:
    lists: list[dict] = []
    videos = pin.get("videos")
    if isinstance(videos, dict) and videos:
        vl = videos.get("video_list")
        if isinstance(vl, dict) and vl:
            lists.append(vl)
        elif any(isinstance(v, dict) and v.get("url") for v in videos.values()):
            lists.append(videos)
    story = pin.get("story_pin_data")
    if isinstance(story, dict):
        for page in story.get("pages") or []:
            if not isinstance(page, dict):
                continue
            for block in page.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                vid = block.get("video")
                if isinstance(vid, dict) and isinstance(vid.get("video_list"), dict):
                    lists.append(vid["video_list"])
    return lists


def _video_formats(pin: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for vl in _video_lists(pin):
        for key, fmt in vl.items():
            if not isinstance(fmt, dict):
                continue
            url = str(fmt.get("url") or "").strip()
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            path = urlparse(url).path.lower()
            if path.endswith(".mp4"):
                kind = "mp4"
            elif path.endswith(".m3u8"):
                kind = "hls"
            else:
                kind = "other"
            out.append(
                {
                    "url": url,
                    "kind": kind,
                    "key": str(key),
                    "width": fmt.get("width"),
                    "height": fmt.get("height"),
                    "duration": fmt.get("duration") or fmt.get("duration_ms"),
                }
            )
    return out


def _is_video_pin(pin: dict) -> bool:
    return bool(_video_formats(pin))


def _as_seconds(raw: Any) -> float | None:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val > 1000:
        val = val / 1000.0
    return val if val > 0 else None


def _duration_seconds(pin: dict, formats: list[dict[str, Any]] | None = None) -> float | None:
    best = None
    for fmt in formats or _video_formats(pin):
        val = _as_seconds(fmt.get("duration"))
        if val is not None:
            best = val if best is None else max(best, val)
    story = pin.get("story_pin_data")
    if isinstance(story, dict):
        val = _as_seconds(story.get("total_video_duration"))
        if val is not None:
            best = val if best is None else max(best, val)
    return best


def _preview_choice(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    def score(fmt: dict[str, Any]) -> tuple[int, int]:
        path = urlparse(fmt["url"]).path.lower()
        # Prefer a real clip over expMp4 trailers (_t1, _t2, …).
        trailer = 1 if "_t" in path.split("/")[-1] else 0
        try:
            width = int(fmt.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        # Hover: around 360–540 wide is enough.
        closeness = abs(width - 540) if width else 9000
        return (trailer, closeness)

    mp4s = [f for f in formats if f.get("kind") == "mp4"]
    if mp4s:
        return min(mp4s, key=score)
    hls = [f for f in formats if f.get("kind") == "hls"]
    if hls:
        return min(hls, key=score)
    return None


def _thumbnail(pin: dict) -> str:
    images = pin.get("images")
    if not isinstance(images, dict):
        return ""
    for key in ("474x", "236x", "564x", "736x", "orig", "origs"):
        row = images.get(key)
        if isinstance(row, dict) and row.get("url"):
            return str(row["url"])
    for row in images.values():
        if isinstance(row, dict) and row.get("url"):
            return str(row["url"])
    return ""


def _pin_url(pin: dict) -> str:
    url = (pin.get("url") or "").strip()
    if url:
        return url
    pin_id = str(pin.get("id") or "").strip()
    if pin_id:
        return f"https://www.pinterest.com/pin/{pin_id}/"
    return ""


def _orig_image(pin: dict) -> dict[str, Any]:
    images = pin.get("images")
    if not isinstance(images, dict):
        return {}
    for key in ("orig", "origs", "736x", "564x", "474x"):
        row = images.get(key)
        if isinstance(row, dict) and row.get("url"):
            return {
                "url": str(row.get("url") or ""),
                "width": row.get("width"),
                "height": row.get("height"),
            }
    for row in images.values():
        if isinstance(row, dict) and row.get("url"):
            return {
                "url": str(row.get("url") or ""),
                "width": row.get("width"),
                "height": row.get("height"),
            }
    return {}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    # Nested API blobs occasionally appear as dict/list — ignore.
    return ""


def _normalize_pin(pin: dict, *, media: str = "video") -> dict[str, Any] | None:
    if not isinstance(pin, dict):
        return None
    url = _pin_url(pin)
    if not url:
        return None
    formats = _video_formats(pin)
    want = (media or "video").strip().lower()
    if want == "video" and not formats:
        return None
    if want == "image" and formats:
        return None
    title = _as_text(
        pin.get("grid_title") or pin.get("title") or pin.get("seo_title")
    )
    description = _as_text(
        pin.get("description") or pin.get("closeup_unified_description")
    )
    alt = _as_text(
        pin.get("seo_alt_text") or pin.get("auto_alt_text") or pin.get("autoAltText")
    )
    orig = _orig_image(pin)
    preview = _preview_choice(formats) if formats else None
    if want == "image" and not orig.get("url"):
        return None
    if want == "any" and not formats and not orig.get("url"):
        return None
    kind = "video" if formats else "image"
    width = (preview or {}).get("width") or orig.get("width")
    height = (preview or {}).get("height") or orig.get("height")
    image_url = str(orig.get("url") or "")
    return {
        "pinId": str(pin.get("id") or ""),
        "url": url,
        "title": title or alt or ("Pinterest video" if kind == "video" else "Pinterest image"),
        "description": description,
        "alt": alt,
        "thumbnail": _thumbnail(pin),
        "previewUrl": (preview or {}).get("url") or image_url,
        "previewKind": (preview or {}).get("kind") or ("image" if kind == "image" else ""),
        "imageUrl": image_url,
        "kind": kind,
        "duration": _duration_seconds(pin, formats) if formats else 0,
        "width": width,
        "height": height,
    }


def _norm_url(url: str) -> str:
    try:
        parsed = urlparse((url or "").strip())
        host = parsed.netloc.lower().removeprefix("www.")
        path = parsed.path.rstrip("/")
        return f"{host}{path}"
    except Exception:
        return (url or "").strip().lower()


def search_videos(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    exclude_urls: list[str] | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    return search_pins(
        query,
        media="video",
        limit=limit,
        cursor=cursor,
        exclude_urls=exclude_urls,
        max_pages=max_pages,
    )


def search_images(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    exclude_urls: list[str] | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    return search_pins(
        query,
        media="image",
        limit=limit,
        cursor=cursor,
        exclude_urls=exclude_urls,
        max_pages=max_pages,
    )


def search_pins(
    query: str,
    *,
    media: str = "video",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    exclude_urls: list[str] | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise PinterestError("Search query is empty")
    want = (media or "video").strip().lower()
    if want not in ("video", "image", "any"):
        want = "video"
    limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    page_cap = MAX_PAGES if max_pages is None else max(1, min(MAX_PAGES, int(max_pages)))
    exclude = {_norm_url(u) for u in (exclude_urls or []) if u}

    pins: list[dict[str, Any]] = []
    next_cursor = (cursor or "").strip() or None
    pages = 0
    data: dict[str, Any] = {}

    while len(pins) < limit and pages < page_cap:
        params: dict[str, Any] = {"query": q, "trim": "false"}
        if next_cursor:
            params["cursor"] = next_cursor
        try:
            resp = requests.get(
                f"{SCRAPE_BASE}/v1/pinterest/search",
                headers=_headers(),
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise PinterestError(f"Pinterest search request failed: {exc}") from exc
        if resp.status_code != 200:
            _raise_http(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise PinterestError("Pinterest search returned invalid JSON") from exc
        pages += 1
        raw_pins = data.get("pins") if isinstance(data, dict) else None
        if not isinstance(raw_pins, list):
            raw_pins = []
        for pin in raw_pins:
            mapped = _normalize_pin(pin, media=want)
            if not mapped:
                continue
            if _norm_url(mapped["url"]) in exclude:
                continue
            if any(_norm_url(p["url"]) == _norm_url(mapped["url"]) for p in pins):
                continue
            pins.append(mapped)
            if len(pins) >= limit:
                break
        raw_cursor = data.get("cursor") if isinstance(data, dict) else None
        if isinstance(raw_cursor, str):
            next_cursor = raw_cursor.strip() or None
        elif raw_cursor is None:
            next_cursor = None
        else:
            # Some responses return a cursor object; stringify bookmark-like values.
            next_cursor = str(raw_cursor).strip() or None
            if next_cursor in ("{}", "None", "null"):
                next_cursor = None
        if not next_cursor:
            break

    return {
        "ok": True,
        "query": q,
        "media": want,
        "pins": pins[:limit],
        "cursor": next_cursor,
        "pages": pages,
        "creditsRemaining": data.get("credits_remaining") if isinstance(data, dict) else None,
    }


def fetch_thumbnail_jpegs(
    urls: list[str],
    *,
    max_n: int = 8,
    max_edge: int = 400,
) -> list[dict[str, str]]:
    """Download and shrink thumbnails for vision. Returns [{media_type, data, url}]."""
    from PIL import Image

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in urls:
        if len(out) >= max_n:
            break
        url = (raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200 or not resp.content:
                continue
            img = Image.open(BytesIO(resp.content))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_edge, max_edge))
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=75)
            out.append(
                {
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                    "url": url,
                }
            )
        except Exception:
            _log.debug("Thumbnail fetch failed: %s", url, exc_info=True)
            continue
    return out


_PIN_MEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.pinterest.com/",
}


def is_allowed_media_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.lower().removeprefix("www.")
    return host == "pinimg.com" or host.endswith(".pinimg.com")


def media_proxy_path(target: str, auth: str | None = None) -> str:
    q = "u=" + quote(target, safe="")
    if auth:
        q += "&auth=" + quote(auth, safe="")
    return "/api/pinterest/media?" + q


def _rewrite_m3u8(text: str, playlist_url: str, auth: str | None) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            abs_url = urljoin(playlist_url, stripped)
            if is_allowed_media_url(abs_url):
                lines.append(media_proxy_path(abs_url, auth))
            else:
                lines.append(stripped)
            continue
        if 'URI="' in line:

            def _repl(match: re.Match[str]) -> str:
                raw = match.group(1)
                abs_url = urljoin(playlist_url, raw)
                if is_allowed_media_url(abs_url):
                    return f'URI="{media_proxy_path(abs_url, auth)}"'
                return match.group(0)

            lines.append(re.sub(r'URI="([^"]+)"', _repl, line))
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def _media_content_type(path: str, fallback: str) -> str:
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".cmfa"):
        return fallback or "audio/mp4"
    if path.endswith((".cmfv", ".m4s", ".mp4", ".m4a")):
        return fallback or "video/mp4"
    if path.endswith(".ts"):
        return fallback or "video/mp2t"
    return fallback or "application/octet-stream"


def fetch_media(
    url: str, *, auth: str | None = None, range_header: str | None = None
) -> dict[str, Any]:
    """Fetch a pinimg playlist or CMAF/MP4 segment.

    Pinterest HLS is CMAF (`.cmfv` / `.cmfa`) with EXT-X-BYTERANGE, so Range
    requests must be forwarded or hls.js cannot append fragments.
    """
    target = (url or "").strip()
    if not is_allowed_media_url(target):
        raise PinterestError("Invalid Pinterest media URL")
    path = urlparse(target).path.lower()
    is_playlist = path.endswith(".m3u8")
    headers = dict(_PIN_MEDIA_HEADERS)
    if range_header and not is_playlist:
        headers["Range"] = range_header
    try:
        resp = requests.get(target, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise PinterestError(f"Preview request failed: {exc}") from exc
    if resp.status_code not in (200, 206) or not resp.content:
        raise PinterestError(f"Pinterest media failed ({resp.status_code})")
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if is_playlist or "mpegurl" in ctype or "m3u8" in ctype:
        return {
            "body": _rewrite_m3u8(resp.text, target, auth).encode("utf-8"),
            "content_type": "application/vnd.apple.mpegurl",
            "status": 200,
            "headers": {"Cache-Control": "private, max-age=30"},
        }
    out_headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=120"}
    content_range = resp.headers.get("Content-Range")
    if content_range:
        out_headers["Content-Range"] = content_range
    return {
        "body": resp.content,
        "content_type": _media_content_type(path, ctype),
        "status": 206 if resp.status_code == 206 else 200,
        "headers": out_headers,
    }
