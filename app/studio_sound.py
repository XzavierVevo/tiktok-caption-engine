"""Seed a TikTok original sound (video with baked-in audio) and read Studio review."""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .config import FFMPEG_PATH, user_output_dir

_log = logging.getLogger(__name__)

_DURATION = re.compile(r"^00:\d{2}$")
_PRIVACY = re.compile(r"^(Everyone|Only me|Friends)$", re.I)


def classify_studio_review(blob: str) -> str:
    """Studio Content row → review state.

    Approved means exactly what ops saw on the seed: Privacy is Everyone
    and there is no "Content under review" badge. Only then can a later
    photo/video post use this original sound.
    """
    low = (blob or "").lower()
    if any(
        tok in low
        for tok in (
            "violat",
            "not approved",
            "taken down",
            "removed by",
            "ineligible",
            "copyright strike",
            "muted",
        )
    ):
        return "rejected"
    under_review = "content under review" in low or "under review" in low
    everyone = bool(re.search(r"\beveryone\b", low))
    if under_review:
        return "pending"
    if everyone:
        return "approved"
    if "only me" in low:
        return "private"
    return "unknown"


def parse_studio_content_rows(text: str) -> list[dict]:
    """Parse Studio Content innerText into post rows."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    rows: list[dict] = []
    i = 0
    while i < len(lines):
        if not _DURATION.match(lines[i]):
            i += 1
            continue
        duration = lines[i]
        title = lines[i + 1] if i + 1 < len(lines) else ""
        rest: list[str] = []
        j = i + 2
        while j < len(lines) and not _DURATION.match(lines[j]):
            rest.append(lines[j])
            if _PRIVACY.match(lines[j]) and len(rest) >= 1:
                j += 1
                break
            j += 1
        blob = "\n".join([duration, title, *rest])
        privacy = next((x for x in rest if _PRIVACY.match(x)), "")
        rows.append(
            {
                "duration": duration,
                "title": title,
                "privacy": privacy,
                "review": classify_studio_review(blob),
                "underReview": "under review" in blob.lower(),
            }
        )
        i = j
    return rows


def pick_sound_row(rows: list[dict], *, query: str = "", duration: str = "00:20") -> dict | None:
    q = (query or "").strip().lower()
    hits = list(rows)
    if duration:
        prefer = [r for r in hits if r.get("duration") == duration]
        if prefer:
            hits = prefer
    if q:
        named = [r for r in hits if q in str(r.get("title") or "").lower()]
        if named:
            hits = named
    return hits[0] if hits else None


def mux_still_audio(audio_path: str | Path, still_path: str | Path | None = None) -> Path:
    audio = Path(audio_path)
    if not audio.is_file():
        raise FileNotFoundError(f"Audio not found: {audio}")
    ff = FFMPEG_PATH
    if not ff:
        raise RuntimeError("ffmpeg is not available")
    still = Path(still_path) if still_path else None
    if still is None or not still.is_file():
        from PIL import Image

        tmp = Path(tempfile.mkdtemp(prefix="tt-sound-")) / "still.jpg"
        Image.new("RGB", (1080, 1920), (20, 20, 20)).save(tmp, quality=90)
        still = tmp
    out = Path(tempfile.mkdtemp(prefix="tt-sound-")) / "original_sound.mp4"
    cmd = [
        str(ff),
        "-y",
        "-loop",
        "1",
        "-i",
        str(still),
        "-i",
        str(audio),
        "-shortest",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def start_custom_sound(
    uid: str,
    *,
    account_id: str,
    video_path: str = "",
    audio_path: str = "",
    image_path: str = "",
    source_id: str = "",
    caption: str = "",
) -> dict:
    from . import tiktok_upload

    path = str(video_path or "").strip()
    if not path and source_id:
        from . import library

        try:
            row = library.get_source(uid, source_id)
        except Exception as exc:
            raise ValueError(f"sourceId not found: {source_id}") from exc
        if isinstance(row, dict):
            path = str(row.get("path") or row.get("videoPath") or row.get("file") or "")
    if not path and audio_path:
        path = str(mux_still_audio(audio_path, image_path or None))
    if not path or not Path(path).is_file():
        raise ValueError("Need a videoPath, sourceId, or audioPath to seed an original sound")
    cap = (caption or "original sound seed").strip()
    dest = user_output_dir(uid) / "sounds"
    dest.mkdir(parents=True, exist_ok=True)
    stored = dest / f"seed_{Path(path).stem[:24]}_{int(time.time())}{Path(path).suffix}"
    try:
        stored.write_bytes(Path(path).read_bytes())
        path = str(stored)
    except Exception:
        _log.debug("could not copy sound seed into outputs", exc_info=True)
    job = tiktok_upload.start_upload(
        uid,
        [{"videoPath": path, "caption": cap, "filename": Path(path).name}],
        caption_template="{caption}",
        account_id=account_id,
        automatic_copyright_checks=True,
        content_checks=True,
    )
    state = tiktok_upload.job_state(job)
    state["kind"] = "custom_sound"
    state["caption"] = cap
    state["hint"] = "Poll get_custom_sound until review is approved, then upload"
    return state


def scrape_studio_content(uid: str, account_id: str, *, query: str = "") -> list[dict]:
    from . import tiktok_accounts as accounts
    from . import tiktok_upload as tu

    cookie = accounts.cookie_path_for(uid, account_id)
    if not cookie.is_file():
        raise FileNotFoundError(f"No cookies for account {account_id}")
    session = tu._UploadSession(headless=True)
    session.start()
    try:
        session.ensure_logged_in(account_id, str(cookie))
        page = session.page
        page.goto(
            "https://www.tiktok.com/tiktokstudio/content?lang=en",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            body = page.inner_text("body") or ""
            if "00:" in body:
                break
            time.sleep(0.8)
        if query:
            box = page.get_by_placeholder(re.compile(r"Search for post", re.I))
            if box.count():
                box.first.fill(query)
                box.first.press("Enter")
                time.sleep(4)
        body = page.inner_text("body") or ""
        return parse_studio_content_rows(body)
    finally:
        session.close()


def get_custom_sound(
    uid: str,
    *,
    job_id: str = "",
    account_id: str = "",
    query: str = "",
) -> dict:
    from . import tiktok_upload

    job = tiktok_upload.get_job(job_id, uid) if job_id else None
    job_state = tiktok_upload.job_state(job) if job else {}
    if job and job.status not in ("done", "cancelled"):
        return {
            "ok": True,
            "review": "uploading",
            "ready": False,
            **job_state,
        }
    item = job.items[0] if job and job.items else None
    cap = query or (item.caption if item else "") or (item.description if item else "")
    acct = account_id or (item.account_id if item else "") or ""
    if not acct:
        raise ValueError("accountId required to read Studio review")
    rows = scrape_studio_content(uid, str(acct), query=str(cap or ""))
    row = pick_sound_row(rows, query=str(cap or ""))
    review = str((row or {}).get("review") or "unknown")
    return {
        "ok": True,
        "review": review,
        "ready": review == "approved",
        "post": row,
        "posts": rows[:12],
        "hint": (
            "Use start_carousel_upload / start_tiktok_upload with useOriginalSound "
            "once review is approved"
            if review == "approved"
            else "Keep polling get_custom_sound until review is approved"
        ),
        **({k: v for k, v in job_state.items() if k not in {"ok"}} if job_state else {}),
    }
