from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from .. import catalog
from .. import library
from .. import storage
from ..config import CHAT_ATTACH_MAX, CHAT_VISION_MAX, user_chat_images_dir

_registry_lock = library.registry_lock
_state = library.state
_save_session = library.save_session
_infer_platform = library.infer_platform
_annotate_source = library.annotate_source
_register_source = library.register_source
_unlink_source = library.unlink_source
_purge_source = library.purge_source
_backfill_library = library.backfill_library
_job_sources = library.job_sources
_library_items = library.library_items
_get_source = library.get_source
_ensure_source_file = library.ensure_source_file
_ensure_preview_frame = library.ensure_preview_frame

def _source_preview_jpeg(uid: str, src: dict) -> bytes | None:
    """First-frame JPEG for a library video (cached next to other previews)."""
    try:
        frame_path = _ensure_preview_frame(uid, src)
    except HTTPException:
        return None
    if not frame_path.is_file():
        return None
    return frame_path.read_bytes()


def _chat_image_path(uid: str, image_id: str) -> Path:
    safe = Path(image_id or "").name
    if not safe or safe != image_id:
        raise HTTPException(404, "Image not found")
    path = (user_chat_images_dir(uid) / safe).resolve()
    root = user_chat_images_dir(uid).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(404, "Image not found") from exc
    if path.is_file():
        return path
    restored = storage.ensure_local(uid, f"chat_images/{safe}")
    if not restored:
        raise HTTPException(404, "Image not found")
    return restored


def _jpeg_thumb_from_bytes(blob: bytes) -> dict | None:
    from ..agent.loop import _jpeg_thumb_b64

    try:
        return _jpeg_thumb_b64(blob)
    except Exception:
        return None


def _slim_source_attachment(src: dict) -> dict:
    return {
        "kind": "source",
        "sourceId": src.get("id") or "",
        "name": src.get("name") or "video",
        "url": src.get("url") or "",
        "platform": src.get("platform") or src.get("origin") or "",
        "origin": src.get("origin") or "",
        "width": src.get("width") or 0,
        "height": src.get("height") or 0,
        "duration": src.get("duration") or 0,
        "title": src.get("title") or src.get("name") or "",
    }


def _slim_image_attachment(image_id: str, name: str, library_id: str = "") -> dict:
    out = {
        "kind": "image",
        "imageId": image_id,
        "name": name or image_id,
    }
    if library_id:
        out["libraryId"] = library_id
    return out


def _slim_pin_attachment(att: dict) -> dict:
    pin_url = (att.get("pinUrl") or att.get("url") or "").strip()
    title = (att.get("title") or att.get("name") or "Pinterest pin").strip()
    return {
        "kind": "pin",
        "pinUrl": pin_url,
        "url": pin_url,
        "name": title or "Pinterest pin",
        "title": title or "Pinterest pin",
        "thumbUrl": (att.get("thumbUrl") or att.get("thumbnail") or "").strip(),
        "previewUrl": (att.get("previewUrl") or "").strip(),
        "previewKind": (att.get("previewKind") or "").strip(),
        "platform": "pinterest",
        "width": att.get("width") or 0,
        "height": att.get("height") or 0,
        "duration": att.get("duration") or 0,
    }


def _attach_payload_for_source(src: dict) -> dict:
    slim = _slim_source_attachment(src)
    slim["thumbUrl"] = f"/api/sources/{src['id']}/frame"
    return slim


def _format_attachment_note(items: list[dict]) -> str:
    if not items:
        return ""
    lines = [
        "Attached media (models receive a first-frame/still only — not the full video). "
        "Frame order matches the list below (Frame 1 = first image, etc.):"
    ]
    for i, it in enumerate(items, 1):
        kind = it.get("kind") or "source"
        name = it.get("name") or it.get("title") or "untitled"
        if kind == "image":
            bits = [f"Frame {i}: still image “{name}”"]
            lid = (it.get("libraryId") or "").strip()
            if lid:
                bits.append(f"imageId={lid}")
            else:
                iid = (it.get("imageId") or "").strip()
                if iid:
                    bits.append(f"chatImage={iid}")
            lines.append(" · ".join(bits))
            continue
        if kind == "pin":
            bits = [f"Frame {i}: Pinterest pin “{name}” (not downloaded yet)"]
            w, h = it.get("width") or 0, it.get("height") or 0
            if w and h:
                bits.append(f"{int(w)}×{int(h)}")
            dur = it.get("duration") or 0
            try:
                dur_f = float(dur)
            except (TypeError, ValueError):
                dur_f = 0.0
            if dur_f:
                bits.append(f"{dur_f:.1f}s")
            url = (it.get("pinUrl") or it.get("url") or "").strip()
            if url:
                bits.append(url)
            lines.append(" · ".join(bits))
            continue
        sid = (it.get("sourceId") or it.get("id") or "").strip()
        bits = [f"Frame {i}: video “{name}”"]
        if sid:
            bits.append(f"sourceId={sid}")
        plat = (it.get("platform") or it.get("origin") or "").strip()
        if plat:
            bits.append(plat)
        w, h = it.get("width") or 0, it.get("height") or 0
        if w and h:
            bits.append(f"{int(w)}×{int(h)}")
        dur = it.get("duration") or 0
        try:
            dur_f = float(dur)
        except (TypeError, ValueError):
            dur_f = 0.0
        if dur_f:
            bits.append(f"{dur_f:.1f}s")
        url = (it.get("url") or "").strip()
        if url:
            bits.append(url)
        lines.append(" · ".join(bits))
    lines.append(
        "When writing captions for these clips, group them under each Frame/sourceId "
        "like:\n"
        "### Frame 1 — <name> (sourceId=<id>)\n"
        "1. <finished on-screen caption>\n"
        "2. <finished on-screen caption>\n"
        "Use these as the clips/images the user is talking about. "
        "Pinterest pins are selected in chat but not downloaded yet."
    )
    return "\n".join(lines)


def _resolve_chat_attachments(uid: str, raw: list) -> dict:
    """Build vision JPEGs + slim metadata for a chat turn."""
    items: list[dict] = []
    vision: list[dict] = []
    seen: set[str] = set()
    pin_thumbs: list[str] = []
    for att in (raw or [])[:CHAT_ATTACH_MAX]:
        if not isinstance(att, dict):
            continue
        kind = (att.get("kind") or "source").strip().lower()
        if kind == "image":
            library_id = (att.get("libraryId") or "").strip()
            image_id = (att.get("imageId") or "").strip()
            key = f"image:{library_id or image_id}"
            if (not library_id and not image_id) or key in seen:
                continue
            seen.add(key)
            path = None
            blob = b""
            name = str(att.get("name") or "").strip()
            if library_id:
                try:
                    from .. import images as image_lib

                    img = image_lib.get_image(uid, library_id)
                    path = image_lib.ensure_image_file(uid, img)
                    blob = path.read_bytes()
                    name = name or str(img.get("name") or path.name)
                except Exception:
                    library_id = ""
                    path = None
            if path is None and image_id:
                try:
                    path = _chat_image_path(uid, image_id)
                    blob = path.read_bytes()
                    name = name or path.name
                except HTTPException:
                    continue
            if path is None:
                continue
            if len(vision) < CHAT_VISION_MAX:
                thumb = _jpeg_thumb_from_bytes(blob)
                if thumb:
                    vision.append(thumb)
            slim = _slim_image_attachment(
                image_id or path.name, name, library_id
            )
            if not slim.get("libraryId"):
                try:
                    from .. import images as image_lib

                    registered = image_lib.promote_chat_image(uid, path.name)
                    slim["libraryId"] = str((registered or {}).get("id") or "")
                except Exception:
                    pass
            if slim.get("libraryId"):
                slim["thumbUrl"] = f"/api/images/{slim['libraryId']}/file"
            elif image_id:
                slim["thumbUrl"] = f"/api/chat/images/{image_id}"
            items.append(slim)
            continue
        if kind == "pin":
            pin_url = (att.get("pinUrl") or att.get("url") or "").strip()
            key = f"pin:{pin_url}"
            if not pin_url or key in seen:
                continue
            seen.add(key)
            slim = _slim_pin_attachment(att)
            items.append(slim)
            thumb_url = (slim.get("thumbUrl") or "").strip()
            if thumb_url and len(pin_thumbs) < CHAT_VISION_MAX:
                pin_thumbs.append(thumb_url)
            continue
        sid = (att.get("sourceId") or "").strip()
        key = f"source:{sid}"
        if not sid or key in seen:
            continue
        seen.add(key)
        try:
            src = _get_source(uid, sid)
        except HTTPException:
            continue
        slim = _slim_source_attachment(src)
        if len(vision) < CHAT_VISION_MAX:
            blob = _source_preview_jpeg(uid, src)
            if blob:
                thumb = _jpeg_thumb_from_bytes(blob)
                if thumb:
                    vision.append(thumb)
        items.append(slim)
    if pin_thumbs:
        try:
            from ..pinterest import fetch_thumbnail_jpegs

            remain = max(0, CHAT_VISION_MAX - len(vision))
            if remain:
                vision.extend(fetch_thumbnail_jpegs(pin_thumbs, max_n=remain))
        except Exception:
            pass
    return {
        "items": items,
        "vision": vision,
        "note": _format_attachment_note(items),
    }


_VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/mp4",
    ".mkv": "video/mp4",
    ".avi": "video/mp4",
}


def _inline_video_response(path: Path, download_name: str | None = None) -> FileResponse:
    """Serve a video for <video> playback (inline, not Content-Disposition: attachment)."""
    media = _VIDEO_MEDIA_TYPES.get(path.suffix.lower(), "video/mp4")
    return FileResponse(
        path,
        media_type=media,
        filename=download_name or path.name,
        content_disposition_type="inline",
    )

