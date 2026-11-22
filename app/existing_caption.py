"""Detect (and, legacy only, remove) a caption already burned into a source.

The render path no longer strips captions. Sources that already carry one
are flagged in the library (``burnedCaption``) and refused by renders; see
``has_burned_caption``.

Downloaded TikToks often already have a white outline caption in the pixels.
Compositing another transparent caption paints the new line through the old
one. When a centered outline caption is present, this clears those glyphs
and leaves the rest of the frame alone.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path

from .config import FFMPEG_PATH, FFPROBE_PATH

_log = logging.getLogger(__name__)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# White fill of an outline caption. Yellow windows and ceiling lights stay out.
_WHITE_LO = (200, 200, 200)
_DARK_LUMA = 55


def _odd(n: int) -> int:
    n = max(3, int(n))
    return n if n % 2 else n + 1


def _dilate_px(height: int) -> int:
    """Stroke + halo, scaled to the frame, capped so faces beside the line stay."""
    return _odd(min(21, max(11, round(height * 0.024))))


def _white_stroke_core(bgr, cv2, np):
    """White glyph pixels that sit on a dark stroke, inside the center band."""
    h, w = bgr.shape[:2]
    white = cv2.inRange(bgr, _WHITE_LO, (255, 255, 255))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    dark = (gray < _DARK_LUMA).astype(np.uint8) * 255
    near = cv2.dilate(
        dark, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    )
    core = cv2.bitwise_and(white, near)
    band = np.zeros_like(core)
    band[int(h * 0.20) : int(h * 0.80), int(w * 0.04) : int(w * 0.96)] = 255
    return cv2.bitwise_and(core, band)


def detect_outline_mask(bgr):
    """Dilated mask of a centered outline caption, or None.

    The mask is the region the caption occupies. Callers should still only
    inpaint pixels that are caption ink on the current frame, so a person
    walking through the line is kept.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    core = _white_stroke_core(bgr, cv2, np)
    return _accept_caption_core(core, bgr.shape, cv2, np)


def _accept_caption_core(core, shape, cv2, np):
    h, w = shape[:2]
    count = int(cv2.countNonZero(core))
    if count < 0.0015 * h * w:
        return None
    ys, xs = np.where(core > 0)
    bw = int(xs.max() - xs.min() + 1)
    bh = int(ys.max() - ys.min() + 1)
    if bw < 0.35 * w or bh > 0.40 * h or bw / max(bh, 1) < 3.0:
        return None
    cx = (float(xs.min()) + float(xs.max())) / 2.0 / w
    if not 0.30 <= cx <= 0.70:
        return None
    k = _dilate_px(h)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    closed = cv2.morphologyEx(core, cv2.MORPH_CLOSE, kernel)
    return cv2.dilate(closed, kernel)


def clear_outline_caption(bgr, static_mask):
    """Inpaint caption ink inside ``static_mask``. Other pixels stay."""
    import cv2
    import numpy as np

    ink = _ink_in_mask(bgr, static_mask, cv2, np)
    if ink is None:
        return bgr
    return cv2.inpaint(bgr, ink, 5, cv2.INPAINT_TELEA)


def _ink_in_mask(bgr, static_mask, cv2, np):
    white = cv2.bitwise_and(
        cv2.inRange(bgr, _WHITE_LO, (255, 255, 255)), static_mask
    )
    if int(cv2.countNonZero(white)) < 40:
        return None
    near = cv2.dilate(white, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    dark = ((gray < _DARK_LUMA) & (near > 0)).astype(np.uint8) * 255
    ink = cv2.bitwise_or(white, dark)
    ink = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    return cv2.bitwise_and(ink, static_mask)


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def _video_meta(src: Path) -> dict | None:
    if not FFPROBE_PATH:
        return None
    proc = _run(
        [
            FFPROBE_PATH,
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(src),
        ]
    )
    if proc.returncode != 0:
        return None
    try:
        info = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return None
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    width, height = int(video["width"]), int(video["height"])
    if rotation % 180 != 0:
        width, height = height, width
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    try:
        num, den = str(rate).split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    if fps < 1 or fps > 120:
        fps = 30.0
    return {"width": width, "height": height, "duration": duration, "fps": fps}


def _sample_frame(src: Path, at: float, width: int, height: int):
    if not FFMPEG_PATH:
        return None
    proc = _run(
        [
            FFMPEG_PATH,
            "-v", "error",
            "-ss", f"{max(0.0, at):.3f}",
            "-i", str(src),
            "-frames:v", "1",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ],
        timeout=30,
    )
    need = width * height * 3
    if proc.returncode != 0 or len(proc.stdout) < need:
        return None
    import numpy as np

    return np.frombuffer(proc.stdout[:need], np.uint8).reshape((height, width, 3)).copy()


def _mask_from_samples(frames) -> object | None:
    import cv2
    import numpy as np

    cores = []
    for frame in frames:
        core = _white_stroke_core(frame, cv2, np)
        if int(cv2.countNonZero(core)) > 0:
            cores.append(core)
    if not cores:
        return None
    union = cores[0]
    for core in cores[1:]:
        union = cv2.bitwise_or(union, core)
    return _accept_caption_core(union, frames[0].shape, cv2, np)


def _read_exact(stream, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _transcode_clear(src: Path, dst: Path, mask, fps: float, cancel_check) -> None:
    import numpy as np

    h, w = mask.shape[:2]
    frame_bytes = w * h * 3
    dec = subprocess.Popen(
        [
            FFMPEG_PATH, "-v", "error", "-i", str(src),
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )
    enc = subprocess.Popen(
        [
            FFMPEG_PATH, "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}", "-r", f"{fps:.6f}",
            "-i", "-",
            "-i", str(src),
            "-map", "0:v", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(dst),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )
    err_box: list[bytes] = []

    def _drain() -> None:
        if enc.stderr:
            err_box.append(enc.stderr.read())

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    frames: queue.Queue = queue.Queue(maxsize=4)
    read_error: list[BaseException] = []

    def _read() -> None:
        try:
            assert dec.stdout is not None
            while True:
                raw = _read_exact(dec.stdout, frame_bytes)
                if raw is None:
                    break
                frames.put(raw)
        except BaseException as exc:  # surfaced on the writer side
            read_error.append(exc)
        finally:
            frames.put(None)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    cancelled = False
    try:
        assert enc.stdin is not None
        while True:
            if cancel_check and cancel_check():
                cancelled = True
                break
            raw = frames.get()
            if raw is None:
                break
            frame = np.frombuffer(raw, np.uint8).reshape((h, w, 3))
            cleaned = clear_outline_caption(frame, mask)
            enc.stdin.write(np.ascontiguousarray(cleaned).tobytes())
        enc.stdin.close()
    finally:
        if dec.poll() is None:
            dec.kill()
        dec.wait(timeout=30)
        if cancelled and enc.poll() is None:
            enc.kill()
        enc.wait(timeout=120)
        drain.join(timeout=5)
        reader.join(timeout=5)
    if read_error:
        raise read_error[0]
    if cancelled:
        from .encode import EncodeError

        raise EncodeError("cancelled")
    if enc.returncode != 0:
        detail = (err_box[0] if err_box else b"").decode("utf-8", "replace")[:300]
        raise RuntimeError(detail or "ffmpeg failed while clearing the old caption")


def has_burned_caption(src: str | Path, *, cancel_check=None) -> bool | None:
    """True when ``src`` already has a centered outline caption burned in.

    Samples four frames and runs the same detector the stripper used. Returns
    None when the check cannot run (no OpenCV / ffmpeg, unreadable file), so
    callers can tell "clean" apart from "unknown".
    """
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        _log.warning("opencv is not installed; burned-in caption check skipped")
        return None
    src = Path(src)
    if not FFMPEG_PATH or not FFPROBE_PATH or not src.is_file():
        return None
    meta = _video_meta(src)
    if not meta:
        return None
    width, height = int(meta["width"]), int(meta["height"])
    if width < 16 or height < 16:
        return None
    duration = float(meta["duration"] or 0)
    times = [0.0] if duration <= 0.4 else [duration * f for f in (0.15, 0.40, 0.65, 0.85)]
    frames = []
    for at in times:
        if cancel_check and cancel_check():
            from .encode import EncodeError

            raise EncodeError("cancelled")
        frame = _sample_frame(src, min(at, max(0.0, duration - 0.05)), width, height)
        if frame is not None and frame.shape[0] == height and frame.shape[1] == width:
            frames.append(frame)
    if not frames:
        return None
    return _mask_from_samples(frames) is not None


def strip_existing_caption(src: Path, dst: Path, *, cancel_check=None) -> bool:
    """Write ``dst`` with a burned-in outline caption removed.

    Returns False when the source has no such caption, or when clearing
    cannot run. The caller then burns onto the original file.
    """
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        _log.warning("opencv is not installed; existing burned-in captions stay")
        return False
    if not FFMPEG_PATH or not FFPROBE_PATH or not src.is_file():
        return False
    meta = _video_meta(src)
    if not meta:
        return False
    width, height = int(meta["width"]), int(meta["height"])
    if width < 16 or height < 16:
        return False
    duration = float(meta["duration"] or 0)
    times = [0.0] if duration <= 0.4 else [duration * f for f in (0.15, 0.40, 0.65, 0.85)]
    frames = []
    for at in times:
        if cancel_check and cancel_check():
            from .encode import EncodeError

            raise EncodeError("cancelled")
        frame = _sample_frame(src, min(at, max(0.0, duration - 0.05)), width, height)
        if frame is not None and frame.shape[0] == height and frame.shape[1] == width:
            frames.append(frame)
    if not frames:
        return False
    mask = _mask_from_samples(frames)
    if mask is None:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        _transcode_clear(src, dst, mask, float(meta["fps"]), cancel_check)
    except Exception as exc:
        from .encode import EncodeError

        dst.unlink(missing_ok=True)
        if isinstance(exc, EncodeError):
            raise
        _log.warning("could not clear existing caption on %s: %s", src.name, exc)
        return False
    if not dst.is_file() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        return False
    _log.info("cleared existing burned-in caption on %s", src.name)
    return True
