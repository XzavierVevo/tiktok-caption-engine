"""Find TikTok/Instagram reaction posts that screen-record photo carousels."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import FFMPEG_PATH, FFPROBE_PATH
from .encode import EncodeError, ensure_binaries

_CREATE = 0x08000000 if sys.platform == "win32" else 0


class CarouselFormatError(RuntimeError):
    pass


DEFAULT_LIMIT = 8
MAX_LIMIT = 16
FRAME_TIMES = (1.0, 4.0, 8.0, 12.0, 16.0)


def normalize_post_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise CarouselFormatError("url is required")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CarouselFormatError(f"Not a valid URL: {raw}")
    host = parsed.netloc.lower().removeprefix("www.")
    if "tiktok.com" in host or "instagram.com" in host:
        return raw.split("?")[0].rstrip("/")
    raise CarouselFormatError("Use a public TikTok or Instagram URL")


def _yt_dlp() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def list_post_urls(profile_url: str, *, limit: int) -> list[dict[str, Any]]:
    url = normalize_post_url(profile_url)
    cap = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    cmd = [
        *_yt_dlp(),
        "--flat-playlist",
        "--no-warnings",
        "--playlist-end",
        str(cap),
        "--print",
        "%(id)s\t%(webpage_url)s\t%(title)s\t%(duration)s",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=_CREATE,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CarouselFormatError(f"Could not list posts: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "yt-dlp failed").strip()[:400]
        raise CarouselFormatError(err)
    rows: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        pid, href = parts[0].strip(), parts[1].strip()
        title = parts[2].strip() if len(parts) > 2 else ""
        duration = parts[3].strip() if len(parts) > 3 else ""
        if not href:
            continue
        dur = None
        try:
            dur = float(duration) if duration not in ("", "NA", "None") else None
        except ValueError:
            dur = None
        rows.append({"id": pid, "url": href, "title": title, "duration": dur})
        if len(rows) >= cap:
            break
    return rows


def _duration_seconds(video: Path) -> float:
    if not FFPROBE_PATH:
        return 0.0
    proc = subprocess.run(
        [
            FFPROBE_PATH,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=_CREATE,
    )
    if proc.returncode != 0:
        return 0.0
    try:
        data = json.loads(proc.stdout or "{}")
        return float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_carousel_frames(video: Path, dest: Path) -> list[str]:
    ensure_binaries()
    dest.mkdir(parents=True, exist_ok=True)
    duration = _duration_seconds(video)
    times = [t for t in FRAME_TIMES if duration <= 0 or t < duration]
    if not times:
        times = [0.5]
    paths: list[str] = []
    for t in times:
        out = dest / f"t{int(t):02d}.jpg"
        proc = subprocess.run(
            [
                FFMPEG_PATH,
                "-y",
                "-ss",
                f"{t:.2f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                str(out),
            ],
            capture_output=True,
            timeout=40,
            creationflags=_CREATE,
        )
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 800:
            paths.append(str(out))
    return paths


def download_post(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest_dir / "%(id)s.%(ext)s")
    cmd = [
        *_yt_dlp(),
        "-q",
        "--no-playlist",
        "-o",
        outtmpl,
        normalize_post_url(url),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, creationflags=_CREATE)
    if proc.returncode != 0:
        raise CarouselFormatError((proc.stderr or "download failed").strip()[:400])
    files = sorted(dest_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        if path.is_file() and path.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}:
            return path
    raise CarouselFormatError("Download finished but no video file was written")


def likely_screen_record(duration: float | None) -> bool:
    """Reaction-to-carousel posts are usually a phone recording plus talking head."""
    if duration is None:
        return True
    return 18.0 <= float(duration) <= 180.0


def find_carousel_formats(
    *,
    profile_url: str = "",
    urls: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    work_dir: Path,
) -> dict[str, Any]:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    posts: list[dict[str, Any]] = []
    given = [normalize_post_url(u) for u in (urls or []) if str(u).strip()]
    if given:
        posts = [{"id": "", "url": u, "title": "", "duration": None} for u in given[:MAX_LIMIT]]
    elif (profile_url or "").strip():
        posts = list_post_urls(profile_url, limit=limit)
    else:
        raise CarouselFormatError("Pass profileUrl or urls[]")

    formats: list[dict[str, Any]] = []
    vision: list[str] = []
    errors: list[dict[str, str]] = []
    for i, post in enumerate(posts[: max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))], 1):
        href = post["url"]
        slot = work_dir / f"{i:02d}"
        try:
            video = download_post(href, slot)
            frames = extract_carousel_frames(video, slot / "frames")
        except (CarouselFormatError, EncodeError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append({"url": href, "error": str(exc)[:300]})
            continue
        duration = post.get("duration") or _duration_seconds(video)
        row = {
            "url": href,
            "title": post.get("title") or "",
            "duration": round(float(duration or 0), 1),
            "likelyReactionCarousel": likely_screen_record(duration),
            "frameCount": len(frames),
            "frames": frames,
            "hint": (
                "Read the early frames. If they show TikTok Photo mode "
                "(slide counter, white on-photo text), copy that on-screen "
                "text as the carousel format. Ignore the talking-head after."
            ),
        }
        formats.append(row)
        vision.extend(frames[:4])
    return {
        "ok": True,
        "count": len(formats),
        "formats": formats,
        "errors": errors,
        "_visionPaths": vision[:24],
    }
