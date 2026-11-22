"""Source ingestion: uploads, yt-dlp URL downloads, validation, hash dedupe.

All ingestion is per-user: files land under the user's uploads/downloads
scratch dirs and are mirrored to R2 by the caller (main.py) after probing.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .config import (
    ALLOWED_URL_HOSTS,
    FFMPEG_PATH,
    IMAGE_EXTENSIONS,
    MAX_IMAGE_MB,
    MAX_UPLOAD_MB,
    VIDEO_EXTENSIONS,
    user_chat_images_dir,
    user_downloads_dir,
    user_images_dir,
    user_uploads_dir,
    user_ytdlp_cookies_path,
)
from . import storage

_SAFE_STEM = re.compile(r"[^\w\s()'\-\.]", re.UNICODE)


class IngestError(RuntimeError):
    pass


def file_hash(path: str | Path, chunk: int = 1 << 20) -> str:
    """Fast content hash: size + first/last 4 MB (plenty for dedupe)."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(p, "rb") as f:
        h.update(f.read(4 * chunk))
        if size > 8 * chunk:
            f.seek(-4 * chunk, 2)
            h.update(f.read(4 * chunk))
    return h.hexdigest()[:16]


def safe_stem(name: str) -> str:
    stem = Path(name).stem
    stem = _SAFE_STEM.sub("_", stem).strip(" ._")
    return stem[:60] or "video"


def save_upload(uid: str, filename: str, data: bytes) -> Path:
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        raise IngestError(f"Unsupported file type: {ext or '(none)'}")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise IngestError(f"File exceeds the {MAX_UPLOAD_MB} MB upload limit")
    dest = user_uploads_dir(uid) / f"{safe_stem(filename)}_{uuid.uuid4().hex[:8]}{ext}"
    dest.write_bytes(data)
    return dest


def _image_dest(uid: str, filename: str, data: bytes, folder: Path) -> Path:
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise IngestError(f"Unsupported image type: {ext or '(none)'}")
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise IngestError(f"Image exceeds the {MAX_IMAGE_MB} MB upload limit")
    dest = folder / f"{safe_stem(filename)}_{uuid.uuid4().hex[:10]}{ext}"
    dest.write_bytes(data)
    return dest


def save_image(uid: str, filename: str, data: bytes) -> Path:
    return _image_dest(uid, filename, data, user_chat_images_dir(uid))


def save_library_image(uid: str, filename: str, data: bytes) -> Path:
    return _image_dest(uid, filename, data, user_images_dir(uid))


def validate_url(url: str) -> str:
    """Return the recognized platform for a URL, or raise IngestError."""
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise IngestError(f"Not a valid URL: {url}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise IngestError(f"Not a valid URL: {url}")
    host = parsed.netloc.lower().removeprefix("www.")
    for allowed in ALLOWED_URL_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            if "tiktok" in allowed:
                return "tiktok"
            if "youtu" in allowed:
                return "youtube"
            return "pinterest"
    raise IngestError(f"Unsupported site: {host} (TikTok / YouTube / Pinterest only)")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_ydl_error(exc: BaseException) -> str:
    """Strip ANSI color codes yt-dlp may embed in exception text."""
    return _ANSI_RE.sub("", str(exc)).strip()[:300]


def download_url(uid: str, url: str) -> Path:
    """Download a video with yt-dlp. Prefers H.264 mp4 at <=1080p.

    If the user uploaded a cookies.txt for downloads (Settings page), it is
    passed to yt-dlp — useful when a site blocks anonymous/datacenter access.
    """
    import yt_dlp

    validate_url(url)
    token = uuid.uuid4().hex[:8]
    downloads = user_downloads_dir(uid)
    outtmpl = str(downloads / f"%(title).50B_{token}.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        # Prefer H.264 ≤1080 on the long edge. Use width OR height so vertical
        # clips (e.g. 720x1280) are not excluded by a height-only filter.
        # Drop ext=m4a — Pinterest HLS audio is mp4, not m4a.
        "format": (
            "bestvideo[vcodec^=avc1][width<=1080]+bestaudio/"
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
            "bestvideo[width<=1080]+bestaudio/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "color": "no_color",
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    cookies_path = user_ytdlp_cookies_path(uid)
    if not cookies_path.is_file():
        storage.ensure_local(uid, "ytdlp_cookies.txt")
    if cookies_path.is_file():
        opts["cookiefile"] = str(cookies_path)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except Exception as exc:  # yt-dlp raises many exception types
        raise IngestError(f"Download failed: {_clean_ydl_error(exc)}") from exc

    if not path.is_file():
        mp4 = path.with_suffix(".mp4")
        if mp4.is_file():
            path = mp4
        else:
            matches = list(downloads.glob(f"*_{token}.*"))
            if not matches:
                raise IngestError("Download finished but output file not found")
            path = matches[0]
    return path
