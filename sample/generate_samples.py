"""
Caption style sample — generates PNG previews with the same settings
the real tool will use (TikTok Sans, white fill, black stroke, centered).

Run:
  pip install pillow
  python generate_samples.py

Outputs land in ./out/
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.config import CAPTION_STYLE as APP_STYLE  # noqa: E402
from app.config import FONTS_DIR  # noqa: E402

# Mirror locked burn-in metrics from app.config.CAPTION_STYLE.
CAPTION_STYLE = {
    "font_file": str(FONTS_DIR / APP_STYLE["fontFile"]),
    "font_weight": APP_STYLE["fontWeight"],
    "fill": (255, 255, 255, 255),
    "stroke": (0, 0, 0, 255),
    "stroke_ratio": APP_STYLE["strokeRatio"],
    "font_size_ratio": APP_STYLE["fontSizeRatio"],
    "max_width_ratio": APP_STYLE["maxWidthRatio"],
    "y_ratio": APP_STYLE["yRatio"],
    "line_height": APP_STYLE["lineHeight"],
}

# Sample captions (manual line breaks OR one long line that auto-wraps)
EXAMPLES = [
    {
        "name": "01_wallet_pass",
        "text": "mood bc that wallet pass\nbe paying u $2k per week\nto play games",
        # Note: Pillow won't render color emoji well; use the HTML preview for emoji.
    },
    {
        "name": "02_grind",
        "text": "when the grind finally\nstarts paying off",
    },
    {
        "name": "03_3am",
        "text": "nobody:\nme at 3am still editing\nthis one clip",
    },
    {
        "name": "04_long_wrap",
        "text": "POV you finally booked the client that pays what you're actually worth",
    },
]

FRAME = (1080, 1920)  # 9:16
OUT = HERE / "out"


def load_font(size: int) -> ImageFont.FreeTypeFont:
    path = Path(CAPTION_STYLE["font_file"])
    return ImageFont.truetype(str(path), size=size)


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap; preserve explicit newlines as hard breaks."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if font.getlength(trial) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def draw_caption(img: Image.Image, text: str) -> None:
    w, h = img.size
    style = CAPTION_STYLE
    font_size = int(h * style["font_size_ratio"])
    stroke_w = max(1, int(font_size * style["stroke_ratio"]))
    max_width = int(w * style["max_width_ratio"])
    font = load_font(font_size)

    lines = wrap_text(text, font, max_width)
    line_gap = int(font_size * style["line_height"])
    block_h = line_gap * len(lines)
    y0 = int(h * style["y_ratio"] - block_h / 2)

    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        lw = font.getlength(line)
        x = (w - lw) / 2
        y = y0 + i * line_gap
        draw.text(
            (x, y),
            line,
            font=font,
            fill=style["fill"],
            stroke_width=stroke_w,
            stroke_fill=style["stroke"],
        )


def make_bg(size: tuple[int, int], seed: int) -> Image.Image:
    """Simple gradient stand-in for a video frame."""
    w, h = size
    img = Image.new("RGBA", size)
    px = img.load()
    r0, g0, b0 = (30 + seed * 20) % 80, (40 + seed * 15) % 90, (50 + seed * 10) % 100
    for y in range(h):
        t = y / h
        r = int(r0 + (20 - r0) * t)
        g = int(g0 + (25 - g0) * t)
        b = int(b0 + (35 - b0) * t)
        for x in range(w):
            # slight horizontal vignette
            v = 1.0 - abs(x / w - 0.5) * 0.25
            px[x, y] = (int(r * v), int(g * v), int(b * v), 255)
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    print("CAPTION_STYLE =", CAPTION_STYLE)
    for i, ex in enumerate(EXAMPLES):
        img = make_bg(FRAME, seed=i)
        draw_caption(img, ex["text"])
        path = OUT / f"{ex['name']}.png"
        img.convert("RGB").save(path, "PNG")
        print(f"wrote {path}")
    print("Done. Open caption-preview.html for live + emoji preview.")


if __name__ == "__main__":
    main()
