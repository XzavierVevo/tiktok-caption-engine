"""Download a portable FFmpeg build into vendor/ffmpeg for local Windows dev."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_BIN = ROOT / "vendor" / "ffmpeg" / "bin"
_ESSENTIALS_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
_FALLBACK_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/8.0/ffmpeg-8.0-essentials_build.zip"
)
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_NEEDED = ("ffmpeg", "ffprobe")


def vendor_ready() -> bool:
    return all((VENDOR_BIN / f"{name}{_EXE_SUFFIX}").is_file() for name in _NEEDED)


def _download(url: str, dest: Path) -> None:
    curl = shutil.which("curl")
    if curl:
        subprocess.run(
            [curl, "-fsSL", "--retry", "3", "-o", str(dest), url],
            check=True,
        )
        return
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "tiktok-caption-tool/ffmpeg-bootstrap"},
    )
    with urllib.request.urlopen(req, timeout=300) as src, dest.open("wb") as dst:
        shutil.copyfileobj(src, dst)


def _install_from_zip(zip_path: Path, extract_root: Path) -> bool:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_root)
    matches = list(extract_root.rglob("ffmpeg.exe"))
    if not matches:
        return False
    src_bin = matches[0].parent
    VENDOR_BIN.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
        src = src_bin / name
        if src.is_file():
            shutil.copy2(src, VENDOR_BIN / name)
    return vendor_ready()


def ensure_vendor_ffmpeg(*, force: bool = False) -> Path | None:
    """Install ffmpeg/ffprobe into vendor/ffmpeg/bin. Windows-only zip bootstrap.

    Returns the bin directory on success, or None if this platform should use
    a system install (or the download failed).
    """
    if vendor_ready() and not force:
        return VENDOR_BIN
    if sys.platform != "win32":
        return VENDOR_BIN if vendor_ready() else None

    print(f"Downloading FFmpeg essentials to {VENDOR_BIN} ...", flush=True)
    last_error: Exception | None = None
    for url in (_ESSENTIALS_URL, _FALLBACK_URL):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / "ffmpeg.zip"
                _download(url, zip_path)
                if _install_from_zip(zip_path, Path(tmp)):
                    print(f"FFmpeg installed at {VENDOR_BIN}", flush=True)
                    return VENDOR_BIN
                print(f"Downloaded zip from {url} did not contain ffmpeg.exe", flush=True)
        except Exception as exc:
            last_error = exc
            print(f"FFmpeg download from {url} failed: {exc}", flush=True)

    if last_error:
        print(f"FFmpeg download failed: {last_error}", flush=True)
    return None
