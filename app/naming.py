"""Output filename templates, collision handling, and batch folders."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_NAME_TEMPLATE

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")


def slugify(text: str, max_words: int = 4, max_len: int = 32) -> str:
    words = re.sub(r"[^\w\s'-]", "", text, flags=re.UNICODE).split()[:max_words]
    slug = "-".join(w.lower() for w in words)
    return slug[:max_len].strip("-") or "caption"


def filename_caption(text: str, max_len: int = 80) -> str:
    """Sanitise full caption text for use in a Windows filename."""
    t = _INVALID.sub("_", (text or "").strip())
    t = _WS.sub(" ", t)
    return t[:max_len].strip(" .") or "caption"


def build_name(
    template: str | None,
    video_stem: str,
    caption: str,
    caption_index: int,
    video_index: int,
    *,
    date_str: str | None = None,
) -> str:
    """Expand output name tokens.

    Tokens:
      {video}         video stem (file name without extension)
      {video01}       video index 01-based zero-padded
      {caption-name}  full caption text (sanitised)
      {caption}       alias for {caption-name}
      {caption01}     legacy alias for zero-padded caption number
      {caption-number} caption index 01-based zero-padded
      {slug}          short slug from caption
      {date}          batch date/time, same format as the output folder
                      (e.g. 08-03-2026 04-10 PM)
    """
    template = (template or DEFAULT_NAME_TEMPLATE).strip() or DEFAULT_NAME_TEMPLATE
    date_token = date_str if date_str else batch_folder_name()
    cap_text = filename_caption(caption)
    cap_num = f"{caption_index + 1:02d}"
    # Longer / hyphenated tokens first so they aren't partially replaced.
    name = (
        template
        .replace("{video01}", f"{video_index + 1:02d}")
        .replace("{video}", video_stem)
        .replace("{caption-name}", cap_text)
        .replace("{caption-number}", cap_num)
        .replace("{caption01}", cap_num)  # legacy
        .replace("{caption}", cap_text)   # alias of caption-name
        .replace("{slug}", slugify(caption))
        .replace("{date}", date_token)
    )
    name = _INVALID.sub("_", name).strip(" .")
    return name[:160] or "output"


def resolve_collision(directory: Path, name: str, ext: str = ".mp4") -> Path:
    """Return a non-existing path, auto-suffixing _2, _3, ..."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{name}{ext}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{name}_{counter}{ext}"
        counter += 1
    return candidate


def batch_folder_name(when: datetime | None = None) -> str:
    """Windows-safe folder name: date then 12-hour time with AM/PM.

    Example: 08-03-2026 04-10 PM
    """
    when = when or datetime.now()
    # %I is 01-12 (zero-padded); colons aren't legal in Windows folder names.
    return when.strftime("%m-%d-%Y %I-%M %p")


def make_batch_output_dir(base: Path, when: datetime | None = None) -> Path:
    """Create a unique run folder under the user's output dir for one batch."""
    base.mkdir(parents=True, exist_ok=True)
    name = batch_folder_name(when)
    path = base / name
    counter = 2
    while path.exists():
        path = base / f"{name}_{counter}"
        counter += 1
    path.mkdir(parents=True, exist_ok=False)
    return path
