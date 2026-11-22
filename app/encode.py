"""FFmpeg / ffprobe helpers: probe, burn-in via overlay compositing,
still-frame extraction, NVENC detection, preview encode."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from .config import (
    ENCODE_FACTOR_NVENC,
    ENCODE_FACTOR_X264,
    FFMPEG_PATH,
    FFPROBE_PATH,
)
from .layout import render_end_pill, render_overlay

# Hide console windows on Windows only; flag is invalid on Linux.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class EncodeError(RuntimeError):
    pass


class BurnedCaptionError(EncodeError):
    """The source clip already has a caption burned into its pixels."""


def ensure_binaries() -> None:
    if not FFMPEG_PATH or not FFPROBE_PATH:
        raise EncodeError(
            "ffmpeg/ffprobe not found. Install FFmpeg and add it to PATH, "
            "or set the FFMPEG_DIR environment variable to its bin folder."
        )


def _run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def probe(path: str | Path) -> dict:
    """Return {width, height, duration, has_audio} for a video file."""
    ensure_binaries()
    p = Path(path)
    try:
        size = p.stat().st_size if p.is_file() else -1
    except OSError as exc:
        raise EncodeError(f"Video file not readable: {p.name}") from exc
    if size < 0:
        raise EncodeError(f"Video file not found: {p.name}")
    if size == 0:
        raise EncodeError(f"Video file is empty: {p.name}")
    proc = _run(
        [
            FFPROBE_PATH,
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(p),
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        raise EncodeError(f"ffprobe failed for {p.name}: {proc.stderr.strip()[:400]}")
    info = json.loads(proc.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise EncodeError(f"No video stream in {p.name}")
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    width, height = int(video["width"]), int(video["height"])
    if rotation % 180 != 0:
        width, height = height, width
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return {
        "width": width, "height": height, "duration": duration,
        "has_audio": has_audio,
        "pix_fmt": video.get("pix_fmt") or "",
        "color_space": video.get("color_space") or "",
        "color_range": video.get("color_range") or "",
    }


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """True if h264_nvenc exists AND actually initializes on this GPU.

    Fast paths:
    - ``NVENC=0/1`` or ``CAPTION_NVENC=0/1`` env skips the probe entirely
    - result is also cached on disk under DATA_DIR so cold boots stay cheap
    """
    import os

    env = (os.environ.get("NVENC") or os.environ.get("CAPTION_NVENC") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True

    if not FFMPEG_PATH:
        return False

    cache_path: Path | None = None
    try:
        from .config import DATA_DIR

        cache_path = Path(DATA_DIR) / ".nvenc_available"
        if cache_path.is_file():
            raw = cache_path.read_text(encoding="utf-8").strip()
            if raw in ("0", "1"):
                return raw == "1"
    except Exception:
        cache_path = None

    try:
        proc = _run(
            [
                FFMPEG_PATH,
                "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i", "color=black:size=256x256:duration=0.1",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            timeout=8,
        )
        ok = proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        ok = False

    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text("1" if ok else "0", encoding="utf-8")
        except Exception:
            pass
    return ok


def encode_factor() -> float:
    return ENCODE_FACTOR_NVENC if nvenc_available() else ENCODE_FACTOR_X264


def _video_codec_args(*, force_x264: bool = False) -> list[str]:
    if not force_x264 and nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]


def _nvenc_dimension_error(stderr: str) -> bool:
    """True when NVENC rejected the frame size (too small / bad params)."""
    s = (stderr or "").lower()
    return "h264_nvenc" in s and (
        "frame dimension" in s
        or "invalid param" in s
        or "minimum supported" in s
    )


def extract_frame(video_path: str | Path, out_path: str | Path, at_seconds: float = 0.5) -> Path:
    """Extract a still frame as JPEG for the live preview."""
    ensure_binaries()
    out_path = Path(out_path)
    proc = _run(
        [
            FFMPEG_PATH,
            "-y", "-v", "error",
            "-ss", str(max(0.0, at_seconds)),
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "3",
            str(out_path),
        ],
        timeout=120,
    )
    if proc.returncode != 0 or not out_path.is_file():
        # retry at t=0 for very short clips
        proc = _run(
            [FFMPEG_PATH, "-y", "-v", "error", "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", str(out_path)],
            timeout=120,
        )
        if proc.returncode != 0:
            raise EncodeError(f"Frame extract failed: {proc.stderr.strip()[:400]}")
    return out_path


def encode_hover_preview(
    video_path: str | Path,
    out_path: str | Path,
    *,
    seconds: float = 2.5,
    width: int = 240,
) -> Path:
    """Create a tiny, silent MP4 intended for library-card hover playback.

    This is deliberately separate from the full source video.  The result is
    small enough to keep in the browser's persistent Cache Storage and cheap
    enough to restore from R2 after a container cold start.
    """
    ensure_binaries()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            FFMPEG_PATH,
            "-y", "-v", "error",
            "-ss", "0",
            "-i", str(video_path),
            "-t", str(max(1.0, min(float(seconds), 4.0))),
            "-an",
            "-vf", f"fps=12,scale={max(120, min(int(width), 360))}:-2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "31",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ],
        timeout=120,
    )
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise EncodeError(f"Hover preview encode failed: {proc.stderr.strip()[:400]}")
    return out_path


def _normalize_end_caption(end_caption: dict | None) -> dict | None:
    """Return {text, durationSeconds, xOffset, yOffset} or None when unused."""
    if not end_caption:
        return None
    text = (end_caption.get("text") or "").strip()
    if not text:
        return None
    try:
        dur = float(end_caption.get("durationSeconds") or 3.0)
    except (TypeError, ValueError):
        dur = 3.0
    dur = max(2.0, min(5.0, dur))
    try:
        x_off = float(end_caption.get("xOffset") or 0.0)
    except (TypeError, ValueError):
        x_off = 0.0
    try:
        y_off = float(end_caption.get("yOffset") or 0.0)
    except (TypeError, ValueError):
        y_off = 0.0
    return {
        "text": text,
        "durationSeconds": dur,
        "xOffset": max(-40.0, min(40.0, x_off)),
        "yOffset": max(-40.0, min(40.0, y_off)),
    }


def normalize_crop(crop: dict | None, width: int, height: int) -> dict | None:
    """Return pixel crop {x,y,w,h} (even dims, locked 9:16) from fractional crop.

    Input crop uses normalized fractions of the source frame:
      {enabled, x, y, w, h} with x/y/w/h in 0..1.
    Output is always a 9:16 rectangle (pixel width/height), snapped from the
    requested region.
    """
    if not crop:
        return None
    if crop.get("enabled") is False:
        return None
    try:
        fx = float(crop.get("x") or 0.0)
        fy = float(crop.get("y") or 0.0)
        fw = float(crop.get("w") or 1.0)
        fh = float(crop.get("h") or 1.0)
    except (TypeError, ValueError):
        return None
    fx = max(0.0, min(1.0, fx))
    fy = max(0.0, min(1.0, fy))
    fw = max(0.0, min(1.0 - fx, fw))
    fh = max(0.0, min(1.0 - fy, fh))
    if fw < 0.01 or fh < 0.01:
        return None

    # Center of the requested region → snap to largest 9:16 box of that size.
    cx = fx + fw / 2.0
    cy = fy + fh / 2.0
    # Requested size in pixels (use the limiting dimension for 9:16).
    req_w = fw * width
    req_h = fh * height
    # Fit 9:16 inside the requested rect, then allow growing to max fit if near-full.
    target_ar = 9.0 / 16.0
    if req_w / max(1.0, req_h) > target_ar:
        box_h = req_h
        box_w = box_h * target_ar
    else:
        box_w = req_w
        box_h = box_w / target_ar

    # Max 9:16 that fits the full frame.
    if width / max(1, height) > target_ar:
        max_h = float(height)
        max_w = max_h * target_ar
    else:
        max_w = float(width)
        max_h = max_w / target_ar

    box_w = min(box_w, max_w)
    box_h = min(box_h, max_h)

    x = cx * width - box_w / 2.0
    y = cy * height - box_h / 2.0
    x = max(0.0, min(width - box_w, x))
    y = max(0.0, min(height - box_h, y))

    # Even dims for yuv420; keep ~9:16 by deriving h from w (or vice versa).
    w = int(round(box_w))
    w -= w % 2
    h = int(round(w / target_ar))
    h -= h % 2
    if h < 16 or w < 16:
        return None
    # If height overshoots, rebuild from height.
    if y + h > height or h > max_h + 1:
        h = int(round(min(box_h, max_h, height - y)))
        h -= h % 2
        w = int(round(h * target_ar))
        w -= w % 2
    if w < 16 or h < 16:
        return None
    x = int(round(x))
    y = int(round(y))
    x -= x % 2
    y -= y % 2
    x = max(0, min(width - w, x))
    y = max(0, min(height - h, y))

    if w >= width - 2 and h >= height - 2 and x <= 2 and y <= 2:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def resolve_output_size(width: int, height: int) -> tuple[int, int]:
    """Ensure at least 720p output; keep higher native / cropped resolution.

    Vertical frames (h >= w) → min 720×1280. Landscape → min 1280×720.
    Upscales with aspect preserved and even dims for yuv420.
    """
    w = int(width or 0)
    h = int(height or 0)
    if w < 2 or h < 2:
        return 720, 1280
    if h >= w:
        min_w, min_h = 720, 1280
    else:
        min_w, min_h = 1280, 720
    scale = 1.0
    if w < min_w:
        scale = max(scale, min_w / w)
    if h < min_h:
        scale = max(scale, min_h / h)
    out_w = int(round(w * scale))
    out_h = int(round(h * scale))
    out_w -= out_w % 2
    out_h -= out_h % 2
    if out_w < min_w:
        out_w = min_w
    if out_h < min_h:
        out_h = min_h
    out_w -= out_w % 2
    out_h -= out_h % 2
    return out_w, out_h


def centered_max_916_crop(width: int, height: int) -> dict | None:
    """Largest centered 9:16 crop as a fractional payload, or None if not needed.

    Landscape (wider than 9:16, including 16:9) gets a full-height center crop.
    Portrait and already-9:16 frames are left alone.
    """
    try:
        w = int(width or 0)
        h = int(height or 0)
    except (TypeError, ValueError):
        return None
    if w < 2 or h < 2:
        return None
    target_ar = 9.0 / 16.0
    if w / h <= target_ar + 0.02:
        return None
    box_h = float(h)
    box_w = box_h * target_ar
    if box_w > w:
        box_w = float(w)
        box_h = box_w / target_ar
    nw = box_w / w
    nh = box_h / h
    return crop_payload(
        {
            "enabled": True,
            "x": (1.0 - nw) / 2.0,
            "y": (1.0 - nh) / 2.0,
            "w": nw,
            "h": nh,
        }
    )


def crop_payload(crop: dict | None) -> dict | None:
    """Stable fractional crop dict for job keys / session (or None)."""
    if not crop:
        return None
    # Treat a valid rect as on even if enabled was omitted by the client.
    if crop.get("enabled") is False:
        return None
    try:
        fx = float(crop.get("x") or 0.0)
        fy = float(crop.get("y") or 0.0)
        fw = float(crop.get("w") or 1.0)
        fh = float(crop.get("h") or 1.0)
    except (TypeError, ValueError):
        return None
    fx = round(max(0.0, min(1.0, fx)), 6)
    fy = round(max(0.0, min(1.0, fy)), 6)
    fw = round(max(0.0, min(1.0 - fx, fw)), 6)
    fh = round(max(0.0, min(1.0 - fy, fh)), 6)
    if fw < 0.01 or fh < 0.01:
        return None
    if fw >= 0.995 and fh >= 0.995 and fx <= 0.002 and fy <= 0.002:
        return None
    return {"enabled": True, "x": fx, "y": fy, "w": fw, "h": fh}


def trim_payload(trim: dict | None, duration: float | None = None) -> dict | None:
    """Return {start, end} in seconds, or None when effectively full-length."""
    if not trim:
        return None
    try:
        start = float(trim.get("start") or 0.0)
        end = float(trim.get("end") if trim.get("end") is not None else (duration or 0.0))
    except (TypeError, ValueError):
        return None
    start = max(0.0, start)
    if duration and duration > 0:
        end = min(duration, end)
        start = min(start, max(0.0, duration - 0.1))
    end = max(start + 0.1, end)
    if duration and duration > 0 and start <= 0.05 and end >= duration - 0.05:
        return None
    return {"start": round(start, 3), "end": round(end, 3)}


def burn_caption(
    video_path: str | Path,
    caption: str,
    out_path: str | Path,
    settings: dict,
    *,
    duration_limit: float | None = None,
    progress_cb=None,
    cancel_check=None,
    end_caption: dict | None = None,
    seek_start: float | None = None,
    end_always_on: bool = False,
    crop: dict | None = None,
    check_existing_caption: bool = True,
) -> Path:
    """Composite the caption overlay onto the video and encode.

    end_caption: optional {text, durationSeconds} shown only in the last N
    seconds of the *output* timeline (after seek/trim). When end_always_on is
    True (short end-of-clip previews), the pill stays visible for the whole
    encode segment.

    crop: optional fractional {enabled, x, y, w, h} applied before overlay so
    captions are laid out for the cropped frame.

    seek_start / duration_limit: optional trim window on the source.

    progress_cb(seconds_done) is called as encode progresses.
    cancel_check() returning True kills the encode.
    """
    ensure_binaries()
    meta = probe(video_path)
    end = _normalize_end_caption(end_caption)
    crop_px = normalize_crop(crop, meta["width"], meta["height"])
    src_w = crop_px["w"] if crop_px else meta["width"]
    src_h = crop_px["h"] if crop_px else meta["height"]
    # Caption PNGs and final encode share this size (upscale to ≥720p if needed).
    frame_w, frame_h = resolve_output_size(src_w, src_h)

    ss = float(seek_start or 0.0)
    if ss < 0:
        ss = 0.0
    if duration_limit is not None:
        out_dur = max(0.1, float(duration_limit))
    else:
        out_dur = max(0.1, meta["duration"] - ss)

    # Only ever render onto a clean base clip. A source that already has a
    # caption burned in would come out caption-on-caption, so refuse it.
    encode_input = Path(video_path)
    if check_existing_caption:
        from .existing_caption import has_burned_caption

        if has_burned_caption(encode_input, cancel_check=cancel_check):
            raise BurnedCaptionError(
                f"{encode_input.name} already has a caption burned in. "
                "It is blocked from rendering; use a clean base clip."
            )

    tmp_paths: list[Path] = []
    try:
        main_img = render_overlay(caption, frame_w, frame_h, settings)
        pill_img = None
        if end:
            pill_img = render_end_pill(
                end["text"],
                frame_w,
                frame_h,
                settings,
                main_caption=caption,
                x_offset=end.get("xOffset") or 0.0,
                y_offset=end.get("yOffset") or 0.0,
            )

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            main_path = Path(tmp.name)
        tmp_paths.append(main_path)
        main_img.save(main_path, "PNG")

        pill_path = None
        if pill_img is not None:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                pill_path = Path(tmp.name)
            tmp_paths.append(pill_path)
            pill_img.save(pill_path, "PNG")

        cmd = [FFMPEG_PATH, "-y", "-hide_banner", "-v", "error", "-progress", "pipe:1"]
        if ss > 0:
            cmd += ["-ss", str(ss)]
        if duration_limit:
            cmd += ["-t", str(duration_limit)]
        cmd += ["-i", str(encode_input)]
        if duration_limit:
            # also limit overlay streams when reusing full-length PNGs
            cmd += ["-t", str(duration_limit)]
        cmd += ["-i", str(main_path)]
        if pill_path is not None:
            if duration_limit:
                cmd += ["-t", str(duration_limit)]
            cmd += ["-i", str(pill_path)]

        vf_parts: list[str] = []
        if crop_px:
            vf_parts.append(
                f"crop={crop_px['w']}:{crop_px['h']}:{crop_px['x']}:{crop_px['y']}"
            )
        # Always scale to resolved size (≥720p). Avoids NVENC rejecting tiny crops
        # (min ~146px) and keeps caption overlays pixel-aligned with the video.
        vf_parts.append(f"scale={frame_w}:{frame_h}:flags=lanczos")
        base = f"[0:v]{','.join(vf_parts)}[base];[base]"
        output_format = ",format=yuv420p"
        # Some uploads incorrectly tag YUV frames as RGB (gbr). Once the PNG
        # overlay is composited in YUV, replace that invalid tag with the HD
        # video matrix so downstream decoders interpret the frames correctly.
        if meta["pix_fmt"].startswith("yuv") and meta["color_space"] == "gbr":
            range_name = "full" if meta["color_range"] == "pc" else "limited"
            output_format += (
                ",setparams=colorspace=bt709:color_primaries=bt709:"
                f"color_trc=bt709:range={range_name}"
            )

        if pill_path is None:
            fc = f"{base}[1:v]overlay=0:0:format=yuv420{output_format}[v]"
        elif end_always_on:
            # Forced (end-of-clip preview) — pill for whole segment.
            fc = (
                f"{base}[1:v]overlay=0:0:format=yuv420[tmp];"
                f"[tmp][2:v]overlay=0:0:format=yuv420{output_format}[v]"
            )
        else:
            # Output timeline t=0 at seek_start; pill covers last N seconds of output.
            start_t = max(0.0, out_dur - end["durationSeconds"])
            fc = (
                f"{base}[1:v]overlay=0:0:format=yuv420[tmp];"
                f"[tmp][2:v]overlay=0:0:format=yuv420:enable='gte(t\\,{start_t:.3f})'{output_format}[v]"
            )

        def _run_encode(*, force_x264: bool = False) -> None:
            cmd_local = list(cmd)
            cmd_local += [
                "-filter_complex", fc,
                "-map", "[v]",
            ]
            if meta["has_audio"]:
                cmd_local += ["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"]
            cmd_local += _video_codec_args(force_x264=force_x264)
            cmd_local += ["-movflags", "+faststart", str(out_path)]

            proc = subprocess.Popen(
                cmd_local,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW,
            )
            try:
                for line in proc.stdout:
                    if cancel_check and cancel_check():
                        proc.kill()
                        proc.wait()
                        raise EncodeError("cancelled")
                    line = line.strip()
                    if line.startswith("out_time_us=") and progress_cb:
                        try:
                            progress_cb(int(line.split("=", 1)[1]) / 1_000_000)
                        except ValueError:
                            pass
            finally:
                if proc.stdout:
                    proc.stdout.close()
            proc.wait()
            if proc.returncode != 0:
                Path(out_path).unlink(missing_ok=True)
                raise EncodeError("ffmpeg failed")

        try:
            _run_encode(force_x264=False)
        except EncodeError as exc:
            # Tiny crops used to trip NVENC before upscale; keep a software fallback.
            if _nvenc_dimension_error(str(exc)):
                _run_encode(force_x264=True)
            else:
                raise
        return Path(out_path)
    finally:
        for p in tmp_paths:
            p.unlink(missing_ok=True)
