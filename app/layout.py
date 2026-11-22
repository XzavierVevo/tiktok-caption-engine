"""Caption layout + rasterization.

Renders captions to transparent RGBA overlays with the locked style:
TikTok Sans (variable, weight from settings). Two looks:
  outline — white fill, thick black stroke
  boxed   — black text on a white per-line wrapped background

Color emoji: try Segoe UI Emoji first; fall back to Twemoji PNG tiles
(cached under data/emoji_cache) so modern glyphs like 🥷 always render
even when the system emoji font is outdated.
"""

from __future__ import annotations

import io
import re
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .caption_markers import strip_markers
from .config import (
    CAPTION_STYLE,
    EMOJI_CACHE_DIR,
    FONTS_DIR,
    MAX_LINES_DEFAULT,
    MIN_FONT_SIZE_RATIO,
    TIKTOK_PHONE_SAFE,
)


def _find_emoji_font() -> Path | None:
    """System color-emoji font: Segoe on Windows, Noto elsewhere.

    When none is usable, emoji fall back to Twemoji PNG tiles below.
    """
    candidates = []
    if sys.platform == "win32":
        candidates.append(Path(r"C:\Windows\Fonts\seguiemj.ttf"))
    candidates.append(FONTS_DIR / "NotoColorEmoji.ttf")
    candidates.append(Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"))
    for p in candidates:
        if p.is_file():
            return p
    return None


EMOJI_FONT_PATH = _find_emoji_font()
_TWEMOJI_CDN = (
    "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{code}.png"
)

# Common emoji ranges + joiners/selectors/flags/keycaps.
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\u2600-\u27bf"
    "\u2b00-\u2bff"
    "\u2190-\u21ff"
    "\u2300-\u23ff"
    "\u25a0-\u25ff"
    "\u2900-\u297f"
    "\U0001f1e6-\U0001f1ff"
    "\ufe0f"
    "\u200d"
    "\u20e3"
    "]+"
)


def normalize_settings(settings: dict | None) -> dict:
    """Merge user overrides onto the locked defaults."""
    merged = dict(CAPTION_STYLE)
    merged["maxLines"] = MAX_LINES_DEFAULT
    merged["forceLowercase"] = False
    merged["preserveLineBreaks"] = True
    if settings:
        for key, value in settings.items():
            if value is not None:
                merged[key] = value
    return merged


@lru_cache(maxsize=32)
def _text_font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONTS_DIR / CAPTION_STYLE["fontFile"]), size=size)
    try:
        # Axis order from fvar: opsz, wdth, wght, slnt
        font.set_variation_by_axes([36.0, 100.0, float(weight), 0.0])
    except OSError:
        pass  # static fallback font has no variations
    return font


@lru_cache(maxsize=16)
def _emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    if EMOJI_FONT_PATH is None or not EMOJI_FONT_PATH.is_file():
        return None
    try:
        return ImageFont.truetype(str(EMOJI_FONT_PATH), size=size)
    except OSError:
        # Bitmap emoji fonts (e.g. NotoColorEmoji CBDT) reject arbitrary
        # sizes — fall back to Twemoji PNG tiles.
        return None


def _twemoji_codes(emoji: str) -> list[str]:
    """Candidate Twemoji filename codes for a single emoji / ZWJ sequence."""
    cps = [ord(ch) for ch in emoji]
    if not cps:
        return []
    full = "-".join(f"{c:x}" for c in cps)
    no_vs = "-".join(f"{c:x}" for c in cps if c != 0xFE0F)
    codes = []
    for c in (full, no_vs):
        if c and c not in codes:
            codes.append(c)
    return codes


@lru_cache(maxsize=512)
def _twemoji_png_bytes(code: str) -> bytes | None:
    """Download (or load cached) a Twemoji 72px PNG for `code`."""
    cache_path = EMOJI_CACHE_DIR / f"{code}.png"
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        try:
            return cache_path.read_bytes()
        except OSError:
            pass
    url = _TWEMOJI_CDN.format(code=code)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TikTokCaptionTool/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            try:
                cache_path.write_bytes(data)
            except OSError:
                pass
            return data
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
    return None


def _load_twemoji(emoji: str, size: int) -> Image.Image | None:
    for code in _twemoji_codes(emoji):
        data = _twemoji_png_bytes(code)
        if not data:
            continue
        try:
            tile = Image.open(io.BytesIO(data)).convert("RGBA")
            if tile.size != (size, size):
                tile = tile.resize((size, size), Image.Resampling.LANCZOS)
            return tile
        except OSError:
            continue
    return None


def _emoji_tokens(run: str) -> list[str]:
    """Split a multi-emoji run into renderable tokens (ZWJ sequences stay together)."""
    if not run:
        return []
    # If the whole run is one Twemoji asset (incl. ZWJ), keep it.
    if _load_twemoji(run, 72) is not None:
        return [run]

    tokens: list[str] = []
    buf: list[str] = []
    for ch in run:
        o = ord(ch)
        if o == 0x200D or o == 0xFE0F or o == 0x20E3:
            buf.append(ch)
            continue
        if buf and buf[-1] not in ("\u200d",) and o >= 0x1F000:
            # start of a new emoji base after completed previous
            tok = "".join(buf)
            if tok:
                tokens.append(tok)
            buf = [ch]
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens or [run]


def _split_runs(line: str) -> list[tuple[str, bool]]:
    """Split a line into (text, is_emoji) runs."""
    runs: list[tuple[str, bool]] = []
    pos = 0
    for m in _EMOJI_RE.finditer(line):
        if m.start() > pos:
            runs.append((line[pos : m.start()], False))
        runs.append((m.group(), True))
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], False))
    return runs


def _emoji_run_width(run: str, font_size: int, emoji_font) -> float:
    tokens = _emoji_tokens(run)
    w = 0.0
    for tok in tokens:
        if _load_twemoji(tok, font_size) is not None:
            w += font_size * 1.05
        elif emoji_font is not None:
            w += emoji_font.getlength(tok)
        else:
            w += font_size
    return w


def _line_width(line: str, font: ImageFont.FreeTypeFont, emoji_font, font_size: int | None = None) -> float:
    size = font_size if font_size is not None else font.size
    width = 0.0
    for run, is_emoji in _split_runs(line):
        if is_emoji:
            width += _emoji_run_width(run, size, emoji_font)
        else:
            width += font.getlength(run)
    return width


def _wrap(text: str, font: ImageFont.FreeTypeFont, emoji_font, max_width: float) -> list[str]:
    """Greedy word wrap; explicit newlines are hard breaks."""
    lines: list[str] = []
    size = font.size
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if _line_width(trial, font, emoji_font, size) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _paste_emoji_run(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    run: str,
    x: float,
    baseline: float,
    font_size: int,
    emoji_font,
) -> float:
    """Draw one emoji run; returns new x after the run."""
    for tok in _emoji_tokens(run):
        tile = _load_twemoji(tok, font_size)
        if tile is not None:
            # Align tile roughly on the text baseline.
            top = baseline - font_size * 0.88
            img.alpha_composite(tile, (int(round(x)), int(round(top))))
            x += font_size * 1.05
            continue
        if emoji_font is not None:
            draw.text((x, baseline), tok, font=emoji_font, anchor="ls", embedded_color=True)
            x += emoji_font.getlength(tok)
        else:
            x += font_size
    return x


def max_safe_width_ratio() -> float:
    """Centered wrap width that clears the wider of the left/right TikTok rails."""
    side = max(float(TIKTOK_PHONE_SAFE["left"]), float(TIKTOK_PHONE_SAFE["right"]))
    return max(0.4, 1.0 - 2.0 * side)


def phone_safe_rect(frame_w: int, frame_h: int) -> tuple[float, float, float, float]:
    """Inclusive pixel box (left, top, right, bottom) for on-screen text."""
    return (
        frame_w * float(TIKTOK_PHONE_SAFE["left"]),
        frame_h * float(TIKTOK_PHONE_SAFE["top"]),
        frame_w * (1.0 - float(TIKTOK_PHONE_SAFE["right"])),
        frame_h * (1.0 - float(TIKTOK_PHONE_SAFE["bottom"])),
    )


def _block_half_height(line_count: int, font_size: int, settings: dict) -> float:
    n = max(int(line_count), 1)
    if settings.get("captionMode") == "boxed":
        _, _, box_h, line_gap = _boxed_metrics(font_size, settings)
        return (n - 1) / 2 * line_gap + box_h / 2
    line_gap = font_size * float(settings["lineHeight"])
    return (n - 1) / 2 * line_gap + font_size * 0.55


def _safe_y_ratio(frame_h: int, half_h: float, wanted_y: float) -> float:
    left, top, right, bottom = phone_safe_rect(1, frame_h)
    del left, right
    min_cy = top + half_h
    max_cy = bottom - half_h
    if min_cy > max_cy:
        return ((top + bottom) / 2) / frame_h
    cy = float(wanted_y) * frame_h
    return min(max(cy, min_cy), max_cy) / frame_h


def fit_caption(
    caption: str, frame_w: int, frame_h: int, settings: dict
) -> tuple[list[str], int, dict]:
    """Wrap and place caption so the full block stays on a phone, off TikTok UI."""
    fitted = dict(normalize_settings(settings))
    fitted["maxWidthRatio"] = min(float(fitted["maxWidthRatio"]), max_safe_width_ratio())
    text = strip_markers(caption or "").strip()
    if fitted.get("forceLowercase"):
        text = text.lower()
    if not fitted.get("preserveLineBreaks", True):
        text = re.sub(r"\s*\n\s*", " ", text)

    max_width = frame_w * float(fitted["maxWidthRatio"])
    max_lines = int(fitted.get("maxLines") or MAX_LINES_DEFAULT)
    size_ratio = float(fitted["fontSizeRatio"])
    weight = int(fitted["fontWeight"])
    safe_h = frame_h * (
        1.0 - float(TIKTOK_PHONE_SAFE["top"]) - float(TIKTOK_PHONE_SAFE["bottom"])
    )

    lines: list[str] = [""]
    font_size = max(8, int(frame_h * size_ratio))
    while True:
        font_size = max(8, int(frame_h * size_ratio))
        font = _text_font(font_size, weight)
        emoji_font = _emoji_font(font_size)
        lines = _wrap(text, font, emoji_font, max_width) if text else [""]
        half = _block_half_height(len(lines), font_size, fitted)
        too_many = len(lines) > max_lines
        too_tall = 2 * half > safe_h
        if (not too_many and not too_tall) or size_ratio <= MIN_FONT_SIZE_RATIO:
            fitted["fontSizeRatio"] = size_ratio
            fitted["yRatio"] = _safe_y_ratio(
                frame_h, half, float(fitted["yRatio"])
            )
            return lines, font_size, fitted
        size_ratio = max(MIN_FONT_SIZE_RATIO, size_ratio - 0.002)


def layout_caption(
    caption: str, frame_w: int, frame_h: int, settings: dict
) -> tuple[list[str], int]:
    """Return (wrapped lines, font_size_px) after shrink-to-fit in the phone safe zone."""
    lines, font_size, _fitted = fit_caption(caption, frame_w, frame_h, settings)
    return lines, font_size


def caption_block_rect(
    caption: str, frame_w: int, frame_h: int, settings: dict
) -> tuple[float, float, float, float]:
    """Pixel box of the burned caption (left, top, right, bottom)."""
    lines, font_size, fitted = fit_caption(caption, frame_w, frame_h, settings)
    font = _text_font(font_size, int(fitted["fontWeight"]))
    emoji_font = _emoji_font(font_size)
    widths = [
        _line_width(line, font, emoji_font, font_size)
        for line in lines
        if line
    ]
    text_w = max(widths) if widths else 0.0
    if fitted.get("captionMode") == "boxed":
        pad_x, _, box_h, _line_gap = _boxed_metrics(font_size, fitted)
        text_w += 2 * pad_x
        del box_h
    half_h = _block_half_height(len(lines), font_size, fitted)
    cy = frame_h * float(fitted["yRatio"])
    left = (frame_w - text_w) / 2
    return (left, cy - half_h, left + text_w, cy + half_h)


def _boxed_metrics(font_size: int, settings: dict) -> tuple[float, float, float, float]:
    """(pad_x, pad_y, box_h, line_gap) for the wrapped-box style.

    Box geometry measured from a real TikTok caption screenshot
    (em = font size): pad_x ~= 0.38em, box height ~= 1.8em, line gap
    ~= 1.15em, so consecutive line boxes overlap and fuse into one shape.
    """
    font = _text_font(font_size, int(settings["fontWeight"]))
    ascent, descent = font.getmetrics()
    text_h = ascent + descent
    pad_x = font_size * float(settings.get("boxPadRatio", 0.38))
    pad_y = pad_x * 0.66
    box_h = text_h + 2 * pad_y
    line_gap = font_size * float(settings.get("lineHeight", 1.15))
    return pad_x, pad_y, box_h, line_gap


def main_caption_bottom(caption: str, frame_w: int, frame_h: int, settings: dict) -> float:
    """Y of the bottom edge of the main caption block (for end-pill placement)."""
    lines, font_size, fitted = fit_caption(caption or " ", frame_w, frame_h, settings)
    cy = frame_h * float(fitted["yRatio"])
    n = max(len(lines), 1)
    if fitted.get("captionMode") == "boxed":
        _, _, box_h, line_gap = _boxed_metrics(font_size, fitted)
        return cy + (n - 1) / 2 * line_gap + box_h / 2
    line_gap = font_size * float(fitted["lineHeight"])
    return cy + (n - 1) / 2 * line_gap + font_size * 0.55


def _boxed_line_boxes(
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    emoji_font,
    font_size: int,
    frame_w: int,
    cy: float,
    line_gap: float,
    pad_x: float,
    box_h: float,
) -> list[tuple[float, float, float, float]]:
    """Padded rectangles (left, top, right, bottom) for each non-empty line."""
    n = len(lines)
    boxes: list[tuple[float, float, float, float]] = []
    for i, line in enumerate(lines):
        if not line:
            continue
        line_center_y = cy + (i - (n - 1) / 2) * line_gap
        total_w = _line_width(line, font, emoji_font, font_size)
        left = (frame_w - total_w) / 2 - pad_x
        right = (frame_w + total_w) / 2 + pad_x
        boxes.append((left, line_center_y - box_h / 2, right, line_center_y + box_h / 2))
    return boxes


def _draw_wrapped_mask(
    frame_w: int,
    frame_h: int,
    boxes: list[tuple[float, float, float, float]],
    radius: float,
    ss: int = 3,
) -> Image.Image:
    """Antialiased L-mode mask of the fused TikTok wrap shape.

    Per-line rounded rects (they overlap vertically), plus small concave
    flares where a narrower line's side edge meets a wider line's
    horizontal edge — the signature TikTok caption silhouette.
    Drawn at `ss`x supersampling, then downsampled for smooth edges.
    """
    mask = Image.new("L", (frame_w * ss, frame_h * ss), 0)
    d = ImageDraw.Draw(mask)
    sboxes = [tuple(v * ss for v in b) for b in boxes]
    r = radius * ss

    for left, top, right, bottom in sboxes:
        rr = min(r, (bottom - top) / 2, (right - left) / 2)
        d.rounded_rectangle((left, top, right, bottom), radius=rr, fill=255)

    for i in range(len(sboxes) - 1):
        al, at, ar, ab = sboxes[i]
        bl, bt, br, bb = sboxes[i + 1]
        if ab <= bt:  # no vertical overlap (blank line between) — separate blobs
            continue
        # Square off inner corners inside the shared overlap band.
        jl, jr = max(al, bl), min(ar, br)
        if jr > jl:
            d.rectangle((jl, bt, jr, ab), fill=255)

        # Concave flares. For each side/edge pair where the neighbor is wider,
        # fill a small square outside the narrower box and carve a quarter
        # disc back out of it.
        def flare(x_edge: float, y_edge: float, dx: int, dy: int, avail: float) -> None:
            f = min(r, avail - r)
            if f < 1.5:
                return
            sq = (min(x_edge, x_edge + dx * f), min(y_edge, y_edge + dy * f),
                  max(x_edge, x_edge + dx * f), max(y_edge, y_edge + dy * f))
            d.rectangle(sq, fill=255)
            cx, cy_ = x_edge + dx * f, y_edge + dy * f
            d.ellipse((cx - f, cy_ - f, cx + f, cy_ + f), fill=0)

        if bl < al - 2:  # lower box extends further left: flare above its top edge
            flare(al, bt, -1, -1, al - bl)
        if al < bl - 2:  # upper box wider: flare below its bottom edge
            flare(bl, ab, -1, +1, bl - al)
        if br > ar + 2:
            flare(ar, bt, +1, -1, br - ar)
        if ar > br + 2:
            flare(br, ab, +1, +1, ar - br)

    return mask.resize((frame_w, frame_h), Image.Resampling.LANCZOS)


def _render_outline_overlay(
    caption: str, frame_w: int, frame_h: int, settings: dict
) -> Image.Image:
    """White fill + black stroke (classic TikTok outline)."""
    lines, font_size, settings = fit_caption(caption, frame_w, frame_h, settings)
    font = _text_font(font_size, int(settings["fontWeight"]))
    emoji_font = _emoji_font(font_size)

    # CSS -webkit-text-stroke is centered on the glyph edge with the fill
    # painted on top, so the visible rim is strokeWidth / 2. Pillow's
    # stroke_width is fully outside the glyph -> halve to match the preview.
    stroke_w = max(1, round(font_size * float(settings["strokeRatio"]) / 2))

    line_gap = font_size * float(settings["lineHeight"])
    ascent, descent = font.getmetrics()
    cy = frame_h * float(settings["yRatio"])
    n = len(lines)

    img = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for i, line in enumerate(lines):
        if not line:
            continue
        line_center_y = cy + (i - (n - 1) / 2) * line_gap
        baseline = line_center_y + (ascent - descent) / 2
        total_w = _line_width(line, font, emoji_font, font_size)
        x = (frame_w - total_w) / 2
        for run, is_emoji in _split_runs(line):
            if is_emoji:
                x = _paste_emoji_run(img, draw, run, x, baseline, font_size, emoji_font)
            else:
                draw.text(
                    (x, baseline),
                    run,
                    font=font,
                    anchor="ls",
                    fill=settings["fill"],
                    stroke_width=stroke_w,
                    stroke_fill=settings["stroke"],
                )
                x += font.getlength(run)
    return img


def _render_boxed_overlay(
    caption: str, frame_w: int, frame_h: int, settings: dict
) -> Image.Image:
    """Black text on a white per-line wrapped background (no stroke).

    Same font/layout as the outline style — only the paint changes:
    tall overlapping line boxes, small corner radius, concave flares
    at the width steps (the TikTok caption-sticker silhouette).
    """
    lines, font_size, settings = fit_caption(caption, frame_w, frame_h, settings)
    font = _text_font(font_size, int(settings["fontWeight"]))
    emoji_font = _emoji_font(font_size)

    pad_x, _pad_y, box_h, line_gap = _boxed_metrics(font_size, settings)
    radius = font_size * float(settings.get("boxCornerRatio", 0.20))
    ascent, descent = font.getmetrics()
    cy = frame_h * float(settings["yRatio"])
    n = len(lines)

    boxes = _boxed_line_boxes(
        lines, font, emoji_font, font_size, frame_w, cy, line_gap, pad_x, box_h
    )
    mask = _draw_wrapped_mask(frame_w, frame_h, boxes, radius)
    img = Image.new("RGBA", (frame_w, frame_h), (255, 255, 255, 0))
    img.putalpha(mask)
    draw = ImageDraw.Draw(img)

    for i, line in enumerate(lines):
        if not line:
            continue
        line_center_y = cy + (i - (n - 1) / 2) * line_gap
        baseline = line_center_y + (ascent - descent) / 2
        total_w = _line_width(line, font, emoji_font, font_size)
        x = (frame_w - total_w) / 2
        for run, is_emoji in _split_runs(line):
            if is_emoji:
                x = _paste_emoji_run(img, draw, run, x, baseline, font_size, emoji_font)
            else:
                draw.text(
                    (x, baseline),
                    run,
                    font=font,
                    anchor="ls",
                    fill=(0, 0, 0, 255),
                )
                x += font.getlength(run)
    return img


def render_overlay(caption: str, frame_w: int, frame_h: int, settings: dict) -> Image.Image:
    """Render the caption to a transparent RGBA image sized to the video frame."""
    settings = normalize_settings(settings)
    if settings.get("captionMode") == "boxed":
        return _render_boxed_overlay(caption, frame_w, frame_h, settings)
    return _render_outline_overlay(caption, frame_w, frame_h, settings)


def render_end_pill(
    text: str,
    frame_w: int,
    frame_h: int,
    settings: dict | None = None,
    *,
    main_caption: str = "",
    x_offset: float = 0.0,
    y_offset: float = 0.0,
) -> Image.Image:
    """White pill + black text/emoji, below the main caption (position adjustable).

    x_offset / y_offset are percent-of-frame shifts from the default placement
    (negative = left/up, positive = right/down). Range is typically -40..+40.
    """
    settings = normalize_settings(settings)
    text = strip_markers(text or "").strip()
    if not text:
        return Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))

    # ~58% of main caption (0.022/0.038) so the pill tracks fontSizeRatio + frame size.
    # Em-relative padding — avoid absolute px floors that blow up on small crops.
    main_ratio = float(settings.get("fontSizeRatio") or 0.038)
    pill_ratio = main_ratio * (0.022 / 0.038)
    font_size = max(8, int(frame_h * pill_ratio))
    weight = min(600, int(settings["fontWeight"]))
    font = _text_font(font_size, weight)
    emoji_font = _emoji_font(font_size)
    ascent, descent = font.getmetrics()

    pad_x = max(2, int(font_size * 0.55))
    pad_y = max(2, int(font_size * 0.36))
    text_w = _line_width(text, font, emoji_font, font_size)
    pill_w = text_w + pad_x * 2
    pill_h = (ascent + descent) + pad_y * 2
    radius = pill_h / 2  # true pill shape

    # Place below main caption block, slightly left of center (screenshot layout).
    if main_caption and main_caption.strip():
        top = main_caption_bottom(main_caption, frame_w, frame_h, settings) + frame_h * 0.018
    else:
        top = frame_h * float(settings["yRatio"]) + frame_h * 0.06
    # Soft left bias (screenshot: “tap belowww” sits under left half of main block).
    center_x = frame_w * 0.36

    # User sliders: percent of frame size.
    ox = max(-40.0, min(40.0, float(x_offset or 0.0)))
    oy = max(-40.0, min(40.0, float(y_offset or 0.0)))
    center_x += frame_w * (ox / 100.0)
    top += frame_h * (oy / 100.0)

    left = center_x - pill_w / 2
    # Clamp so the pill stays in the phone safe zone (off TikTok chrome).
    safe_l, safe_t, safe_r, safe_b = phone_safe_rect(frame_w, frame_h)
    left = max(safe_l, min(left, safe_r - pill_w))
    top = max(safe_t, min(top, safe_b - pill_h))

    img = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Soft shadow so the pill lifts off busy frames.
    shadow = (
        left + frame_w * 0.003,
        top + frame_h * 0.002,
        left + pill_w + frame_w * 0.003,
        top + pill_h + frame_h * 0.002,
    )
    draw.rounded_rectangle(shadow, radius=radius, fill=(0, 0, 0, 55))
    draw.rounded_rectangle(
        (left, top, left + pill_w, top + pill_h),
        radius=radius,
        fill=(255, 255, 255, 255),
    )

    baseline = top + pad_y + ascent
    x = left + pad_x
    for run, is_emoji in _split_runs(text):
        if is_emoji:
            x = _paste_emoji_run(img, draw, run, x, baseline, font_size, emoji_font)
        else:
            draw.text((x, baseline), run, font=font, anchor="ls", fill=(0, 0, 0, 255))
            x += font.getlength(run)
    return img


def render_combined_overlay(
    caption: str,
    frame_w: int,
    frame_h: int,
    settings: dict | None = None,
    *,
    end_caption: str | dict | None = None,
) -> Image.Image:
    """Main caption + optional end pill (for still previews that show both)."""
    img = render_overlay(caption, frame_w, frame_h, settings or {})
    text = ""
    x_off = 0.0
    y_off = 0.0
    if isinstance(end_caption, dict):
        text = (end_caption.get("text") or "").strip()
        try:
            x_off = float(end_caption.get("xOffset") or 0.0)
        except (TypeError, ValueError):
            x_off = 0.0
        try:
            y_off = float(end_caption.get("yOffset") or 0.0)
        except (TypeError, ValueError):
            y_off = 0.0
    elif end_caption:
        text = str(end_caption).strip()
    if text:
        pill = render_end_pill(
            text,
            frame_w,
            frame_h,
            settings,
            main_caption=caption,
            x_offset=x_off,
            y_offset=y_off,
        )
        img = Image.alpha_composite(img, pill)
    return img
