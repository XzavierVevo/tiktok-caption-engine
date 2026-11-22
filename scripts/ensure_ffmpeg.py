"""CLI: download portable FFmpeg into vendor/ffmpeg if missing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ffmpeg_bootstrap import VENDOR_BIN, ensure_vendor_ffmpeg, vendor_ready  # noqa: E402


def main() -> int:
    if vendor_ready():
        print(f"FFmpeg already at {VENDOR_BIN}")
        return 0
    if ensure_vendor_ffmpeg() and vendor_ready():
        return 0
    print(
        "Could not bootstrap FFmpeg. Install it and add it to PATH, "
        "or set FFMPEG_DIR to its bin folder.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
