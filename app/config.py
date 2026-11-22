"""Shared configuration: locked caption style defaults, per-user paths,
ffmpeg discovery, and environment-driven deployment settings."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FONTS_DIR = BASE_DIR / "fonts"
STATIC_DIR = BASE_DIR / "static"

# In the container DATA_DIR points at ephemeral scratch (e.g. /tmp/data);
# durable copies live in R2 via app.storage. Locally it is ./data as before.
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
USERS_DIR = DATA_DIR / "users"

# Shared (not per-user) caches.
EMOJI_CACHE_DIR = DATA_DIR / "emoji_cache"
FONT_CACHE_DIR = DATA_DIR / "font_cache"

for _d in (DATA_DIR, USERS_DIR, EMOJI_CACHE_DIR, FONT_CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_UID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_uid(uid: str) -> str:
    cleaned = _UID_SAFE.sub("_", (uid or "").strip())[:128]
    if not cleaned:
        raise ValueError("Invalid user id")
    return cleaned


def user_root(uid: str) -> Path:
    root = USERS_DIR / _safe_uid(uid)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_dir(uid: str, name: str) -> Path:
    d = user_root(uid) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_uploads_dir(uid: str) -> Path:
    return _user_dir(uid, "uploads")


def user_downloads_dir(uid: str) -> Path:
    return _user_dir(uid, "downloads")


def user_output_dir(uid: str) -> Path:
    return _user_dir(uid, "output")


def user_previews_dir(uid: str) -> Path:
    return _user_dir(uid, "previews")


def user_chat_images_dir(uid: str) -> Path:
    return _user_dir(uid, "chat_images")


def user_images_dir(uid: str) -> Path:
    return _user_dir(uid, "images")


def user_cookies_dir(uid: str) -> Path:
    return _user_dir(uid, "cookies")


def user_provision_cookies_dir(uid: str) -> Path:
    return _user_dir(uid, "provision_cookies")


def user_provision_shots_dir(uid: str) -> Path:
    return _user_dir(uid, "provision_shots")


def user_session_path(uid: str) -> Path:
    return user_root(uid) / "session.json"


def user_accounts_path(uid: str) -> Path:
    return user_root(uid) / "tiktok_accounts.json"


def user_settings_path(uid: str) -> Path:
    return user_root(uid) / "tiktok_settings.json"


def user_manifest_path(uid: str) -> Path:
    return user_root(uid) / "rendered.json"


def user_hooks_offers_path(uid: str) -> Path:
    return user_root(uid) / "hooks_offers.json"


def user_hooks_history_path(uid: str) -> Path:
    return user_root(uid) / "hooks_history.json"


def user_ytdlp_cookies_path(uid: str) -> Path:
    return user_root(uid) / "ytdlp_cookies.txt"


def user_workspace_path(uid: str) -> Path:
    return user_root(uid) / "workspace.json"


def user_usage_path(uid: str) -> Path:
    return user_root(uid) / "usage.json"


def user_chats_dir(uid: str) -> Path:
    return _user_dir(uid, "chats")


def user_chat_index_path(uid: str) -> Path:
    return user_chats_dir(uid) / "index.json"


def user_chat_thread_path(uid: str, thread_id: str) -> Path:
    safe = _safe_uid(thread_id)
    return user_chats_dir(uid) / f"{safe}.json"


def user_facebook_path(uid: str) -> Path:
    return user_root(uid) / "facebook_accounts.json"


def user_catalog_path(uid: str) -> Path:
    return user_root(uid) / "renders.csv"


# ---------------------------------------------------------------------------
# LOCKED CAPTION DEFAULTS (approved in sample/caption-preview.html)
# Keep in sync with static/app.js DEFAULT_STYLE.
# ---------------------------------------------------------------------------
CAPTION_STYLE = {
    "fontFamily": "TikTok Sans",
    "fontFile": "TikTokSans-Variable.ttf",
    "fontWeight": 500,
    "fill": "#FFFFFF",
    "stroke": "#000000",
    "strokeRatio": 0.135,     # outline thickness = em * ratio (CSS centered stroke)
    "fontSizeRatio": 0.038,   # em px = video height * ratio
    "maxWidthRatio": 0.68,    # wrap width; never wider than the phone safe zone
    "yRatio": 0.46,           # block center = video height * ratio
    "lineHeight": 1.15,
    "letterSpacing": -0.01,   # em fraction
    "textAlign": "center",
    # "outline" = white text + black stroke (default)
    # "boxed" = same font/layout, black text on per-line white wrapped
    #   background (box height ~1.8em, pad_x ~0.38em, corner radius ~0.2em,
    #   line boxes overlap at lineHeight 1.15em — measured from TikTok)
    "captionMode": "outline",
    "boxPadRatio": 0.38,      # horizontal pad around glyphs = em * ratio
    "boxCornerRatio": 0.20,   # corner radius = em * ratio
    "ass": {
        "alignment": 5,
        "primaryColour": "&H00FFFFFF",
        "outlineColour": "&H00000000",
    },
}

CAPTION_MODES = {
    "outline": {"label": "Outline (stroke)"},
    "boxed": {"label": "Wrapped box"},
}

STYLE_PRESETS = {
    "thick": {"label": "Thick outline (default)", "strokeRatio": 0.135},
    "thin": {"label": "Thin outline", "strokeRatio": 0.08},
}

POSITION_PRESETS = {
    "center": 0.46,
    "lower": 0.58,
    "upper": 0.28,
}

# Hard phone safe zone for 9:16 FYP / Spark ads. Burn-in must stay inside this
# box so every line is readable on a phone and clear of TikTok chrome.
# Fractions of frame size. Right/bottom are larger because ads add Learn more.
TIKTOK_PHONE_SAFE = {
    "top": 0.11,     # status + Following / For You
    "bottom": 0.34,  # username, post caption, Ad, Learn more, tab bar
    "left": 0.06,
    "right": 0.16,   # like / comment / bookmark / share
}

MAX_LINES_DEFAULT = 6
MIN_FONT_SIZE_RATIO = 0.028
DEFAULT_NAME_TEMPLATE = "{video}_{caption-name}"
SOFT_JOB_CAP = 100
# Typical Playwright / TikTok Studio post, excluding the configured delay between videos.
TIKTOK_UPLOAD_SECONDS = int(os.environ.get("TIKTOK_UPLOAD_SECONDS") or 55)
# Typical HTTP engine post (CDN + publish), excluding delay.
TIKTOK_HTTP_UPLOAD_SECONDS = int(os.environ.get("TIKTOK_HTTP_UPLOAD_SECONDS") or 15)

# Per-user limits (Phase 6 hardening).
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB") or 500)
MAX_IMAGE_MB = int(os.environ.get("MAX_IMAGE_MB") or 25)
CHAT_ATTACH_MAX = int(os.environ.get("CHAT_ATTACH_MAX") or 40)
CHAT_VISION_MAX = int(os.environ.get("CHAT_VISION_MAX") or 8)
MAX_CAROUSEL_SLIDES = 5
MAX_BATCH_JOBS = int(os.environ.get("MAX_BATCH_JOBS") or 200)
MAX_CONCURRENT_BATCHES_PER_USER = int(
    os.environ.get("MAX_CONCURRENT_BATCHES_PER_USER") or 2
)
MAX_CONCURRENT_UPLOADS_PER_USER = int(
    os.environ.get("MAX_CONCURRENT_UPLOADS_PER_USER") or 1
)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS") or 14)

# Rough encode-speed factors (seconds of encode per second of video)
ENCODE_FACTOR_NVENC = 0.15
ENCODE_FACTOR_X264 = 0.5

ALLOWED_URL_HOSTS = (
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "youtube.com",
    "youtu.be",
    "m.youtube.com",
    "youtube-nocookie.com",
    "pinterest.com",
    "pin.it",
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# ---------------------------------------------------------------------------
# FFmpeg discovery: FFMPEG_DIR env, then PATH, then vendor/ + known installs
# (Windows local-dev convenience).
# ---------------------------------------------------------------------------
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def _ffmpeg_candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()

    def add(raw: Path | str | None) -> None:
        if not raw:
            return
        path = Path(raw)
        key = str(path).lower()
        if key in seen:
            return
        seen.add(key)
        dirs.append(path)

    add(os.environ.get("FFMPEG_DIR"))
    add(BASE_DIR / "vendor" / "ffmpeg" / "bin")
    add(BASE_DIR / "bin")
    if sys.platform == "win32":
        localapp = Path(os.environ.get("LOCALAPPDATA") or "")
        program_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        add(Path(r"C:\ffmpeg\bin"))
        add(program_files / "ffmpeg" / "bin")
        add(program_files / "FFmpeg" / "bin")
        add(Path(r"C:\ProgramData\chocolatey\bin"))
        add(Path.home() / "scoop" / "shims")
        add(localapp / "Microsoft" / "WinGet" / "Links")
        for root in (
            localapp / "Microsoft" / "WinGet" / "Packages",
            BASE_DIR / "vendor" / "ffmpeg",
        ):
            if not root.is_dir():
                continue
            for exe in root.glob("**/ffmpeg.exe"):
                add(exe.parent)
    return dirs


def _find_binary(name: str) -> str | None:
    on_path = shutil.which(name)
    if on_path:
        return on_path
    exe = f"{name}{_EXE_SUFFIX}"
    for d in _ffmpeg_candidate_dirs():
        p = d / exe
        if p.is_file():
            return str(p)
    return None


if not _find_binary("ffmpeg") or not _find_binary("ffprobe"):
    try:
        from .ffmpeg_bootstrap import ensure_vendor_ffmpeg

        ensure_vendor_ffmpeg()
    except Exception:
        pass

FFMPEG_PATH = _find_binary("ffmpeg")
FFPROBE_PATH = _find_binary("ffprobe")
