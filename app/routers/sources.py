from __future__ import annotations

import io
import json
import logging
import os
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from .. import catalog
from .. import facebook_ads
from .. import facebook_jobs
from .. import hooks
from .. import jobs
from .. import library
from .. import remote_login
from .. import storage
from .. import tiktok_accounts
from .. import tiktok_upload
from ..auth import get_current_uid
from ..config import (
    CHAT_ATTACH_MAX,
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_MB,
    user_chat_images_dir,
    user_downloads_dir,
    user_output_dir,
    user_previews_dir,
    user_uploads_dir,
    user_ytdlp_cookies_path,
)
from ..encode import (
    EncodeError,
    crop_payload,
    encode_factor,
    probe,
    resolve_output_size,
    trim_payload,
)
from ..hooks import HooksError
from ..ingest import IngestError, download_url, save_image, save_upload, validate_url
from ..layout import render_combined_overlay, render_overlay
from ..facebook_ads import FacebookError
from ..tiktok_accounts import AccountError
from .support import (
    _annotate_source,
    _attach_payload_for_source,
    _backfill_library,
    _chat_image_path,
    _ensure_preview_frame,
    _ensure_source_file,
    _format_attachment_note,
    _get_source,
    _inline_video_response,
    _job_sources,
    _jpeg_thumb_from_bytes,
    _library_items,
    _purge_source,
    _register_source,
    _registry_lock,
    _resolve_chat_attachments,
    _save_session,
    _slim_image_attachment,
    _slim_source_attachment,
    _source_preview_jpeg,
    _state,
    _unlink_source,
)

router = APIRouter(dependencies=[Depends(get_current_uid)])

@router.post("/sources/upload")
async def upload_source(
    file: UploadFile = File(...),
    addToJob: bool = True,
    uid: str = Depends(get_current_uid),
) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    try:
        path = await run_in_threadpool(save_upload, uid, file.filename or "video.mp4", data)
        return await run_in_threadpool(
            _register_source, uid, path, "upload", None, addToJob
        )
    except (IngestError, EncodeError) as exc:
        raise HTTPException(400, str(exc)) from exc


class UrlRequest(BaseModel):
    url: str
    addToJob: bool = True


@router.post("/sources/validate-url")
def validate_url_endpoint(req: UrlRequest, uid: str = Depends(get_current_uid)) -> dict:
    try:
        platform = validate_url(req.url)
        return {"ok": True, "platform": platform}
    except IngestError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/sources/url")
async def add_url_source(req: UrlRequest, uid: str = Depends(get_current_uid)) -> dict:
    try:
        path = await run_in_threadpool(download_url, uid, req.url)
        return await run_in_threadpool(
            _register_source, uid, path, "url", req.url, bool(req.addToJob)
        )
    except (IngestError, EncodeError) as exc:
        raise HTTPException(400, str(exc)) from exc


class PinterestSearchRequest(BaseModel):
    query: str
    limit: int = 12
    cursor: str | None = None
    mediaType: str = "video"


@router.post("/pinterest/search")
def pinterest_search(req: PinterestSearchRequest, uid: str = Depends(get_current_uid)) -> dict:
    from ..pinterest import PinterestError, search_pins

    media = (req.mediaType or "video").strip().lower()
    if media not in ("video", "image", "any"):
        media = "video"
    try:
        result = search_pins(req.query, media=media, limit=req.limit, cursor=req.cursor)
    except PinterestError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        from .. import usage as usage_mod

        usage_mod.record_pinterest(
            uid, thread_id="", credits=int(result.get("pages") or 0)
        )
    except Exception:
        pass
    return result


@router.get("/pinterest/media")
def pinterest_media(u: str, request: Request, uid: str = Depends(get_current_uid)) -> Response:
    from ..pinterest import PinterestError, fetch_media

    auth = (request.query_params.get("auth") or "").strip() or None
    try:
        media = fetch_media(
            u,
            auth=auth,
            range_header=(request.headers.get("range") or "").strip() or None,
        )
    except PinterestError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(
        content=media["body"],
        status_code=int(media.get("status") or 200),
        media_type=media["content_type"],
        headers=media.get("headers") or {},
    )


@router.get("/sources")
def list_sources(uid: str = Depends(get_current_uid)) -> list[dict]:
    return _job_sources(uid)


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, uid: str = Depends(get_current_uid)) -> dict:
    return _unlink_source(uid, source_id)


@router.get("/library")
def list_library(uid: str = Depends(get_current_uid)) -> list[dict]:
    return _library_items(uid)


@router.get("/sources/zip")
def sources_zip(
    ids: str = Query("", max_length=800), uid: str = Depends(get_current_uid)
) -> Response:
    """Zip original source videos for a browser download."""
    raw_ids = [x.strip() for x in (ids or "").split(",") if x.strip()][:40]
    if not raw_ids:
        raise HTTPException(400, "No videos to download")
    files: list[tuple[Path, str]] = []
    used_names: set[str] = set()
    for sid in raw_ids:
        try:
            src = _get_source(uid, sid)
            video = _ensure_source_file(uid, src)
        except HTTPException:
            continue
        name = src.get("name") or video.name or f"{sid}.mp4"
        stem, ext = Path(name).stem, Path(name).suffix or video.suffix or ".mp4"
        candidate = f"{stem}{ext}"
        n = 1
        while candidate in used_names:
            candidate = f"{stem}_{n}{ext}"
            n += 1
        used_names.add(candidate)
        files.append((video, candidate))
    if not files:
        raise HTTPException(404, "None of those videos are available")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for path, name in files:
            zf.write(path, name)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="videos_{stamp}.zip"'
        },
    )


@router.post("/library/{source_id}/use")
def use_library_source(source_id: str, uid: str = Depends(get_current_uid)) -> dict:
    with _registry_lock:
        state = _state(uid)
        src = state["sources"].get(source_id)
        if not src:
            raise HTTPException(404, "Source not found")
        if library.is_blocked(src):
            raise HTTPException(
                409,
                f"{src.get('name') or source_id} already has a caption burned in "
                "and is blocked from rendering. Use a clean base clip.",
            )
        if source_id not in state["order"]:
            state["order"].append(source_id)
    _save_session(uid)
    return _annotate_source(src, True)


class BurnedCaptionRequest(BaseModel):
    burnedCaption: bool = True


@router.put("/sources/{source_id}/burned-caption")
def set_source_burned_caption(
    source_id: str, req: BurnedCaptionRequest, uid: str = Depends(get_current_uid)
) -> dict:
    """Mark a clip as already captioned (blocked from renders), or clear it."""
    src = library.set_burned_caption(uid, source_id, req.burnedCaption, reason="manual")
    return _annotate_source(src, source_id in _state(uid)["order"])


class LibraryApplyRequest(BaseModel):
    addIds: list[str] = []
    removeIds: list[str] = []


@router.post("/library/apply")
def apply_library_selection(
    req: LibraryApplyRequest, uid: str = Depends(get_current_uid)
) -> dict:
    """Batch add/remove library videos on the current Create job (one save)."""
    return library.apply_library_selection(
        uid, add_ids=req.addIds or [], remove_ids=req.removeIds or []
    )


@router.delete("/library/{source_id}")
def delete_library_source(source_id: str, uid: str = Depends(get_current_uid)) -> dict:
    return _purge_source(uid, source_id)


class ReorderRequest(BaseModel):
    sourceIds: list[str]


@router.put("/sources/order")
def reorder_sources(req: ReorderRequest, uid: str = Depends(get_current_uid)) -> list[dict]:
    with _registry_lock:
        state = _state(uid)
        known = set(state["sources"])
        ordered: list[str] = []
        seen: set[str] = set()
        for sid in req.sourceIds:
            if sid in known and sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        for sid in state["order"]:
            if sid not in seen and sid in known:
                ordered.append(sid)
                seen.add(sid)
        state["order"][:] = ordered
    _save_session(uid)
    state = _state(uid)
    return [state["sources"][sid] for sid in state["order"]]


class EndCaptionRequest(BaseModel):
    enabled: bool = False
    text: str = ""
    durationSeconds: float = 3.0
    xOffset: float = 0.0  # % of frame width, -40..40 (left ← → right)
    yOffset: float = 0.0  # % of frame height, -40..40 (up ↑ ↓ down)


def _pack_end_caption(req: EndCaptionRequest) -> dict:
    text = (req.text or "").strip()
    dur = max(2.0, min(5.0, float(req.durationSeconds or 3.0)))
    x_off = max(-40.0, min(40.0, float(req.xOffset or 0.0)))
    y_off = max(-40.0, min(40.0, float(req.yOffset or 0.0)))
    return {
        "enabled": bool(req.enabled),
        "text": text,
        "durationSeconds": dur,
        "xOffset": x_off,
        "yOffset": y_off,
    }


@router.put("/sources/{source_id}/end-caption")
def set_end_caption(
    source_id: str, req: EndCaptionRequest, uid: str = Depends(get_current_uid)
) -> dict:
    src = _get_source(uid, source_id)
    # Keep drafted text/position when toggled off so reopening the panel is easy.
    packed = _pack_end_caption(req)
    src["endCaption"] = packed
    logging.info(f"[END_CAPTION] Setting end caption for {source_id}: enabled={packed.get('enabled')}, text={packed.get('text')[:30] if packed.get('text') else ''}")
    _save_session(uid)
    logging.info(f"[END_CAPTION] Session saved for uid={uid}")
    return src


class CropRequest(BaseModel):
    enabled: bool = False
    x: float = 0.0  # left edge as fraction of frame width
    y: float = 0.0  # top edge as fraction of frame height
    w: float = 1.0  # width as fraction of frame width
    h: float = 1.0  # height as fraction of frame height


def _pack_crop(req: CropRequest) -> dict | None:
    if not req.enabled:
        return None
    return crop_payload(
        {"enabled": True, "x": req.x, "y": req.y, "w": req.w, "h": req.h}
    )


@router.put("/sources/{source_id}/crop")
def set_crop(source_id: str, req: CropRequest, uid: str = Depends(get_current_uid)) -> dict:
    src = _get_source(uid, source_id)
    src["crop"] = _pack_crop(req)
    _save_session(uid)
    return src


class TrimRequest(BaseModel):
    start: float = 0.0
    end: float | None = None


@router.put("/sources/{source_id}/trim")
def set_trim(source_id: str, req: TrimRequest, uid: str = Depends(get_current_uid)) -> dict:
    src = _get_source(uid, source_id)
    dur = float(src.get("duration") or 0.0)
    end = req.end if req.end is not None else dur
    src["trim"] = trim_payload({"start": req.start, "end": end}, dur)
    _save_session(uid)
    return src


# ---------------------------------------------------------------------------
# Preview: still frame + caption overlay
# ---------------------------------------------------------------------------

@router.get("/sources/{source_id}/frame")
def source_frame(
    source_id: str,
    request: Request,
    at: float | None = None,
    generate: bool = Query(False),
    uid: str = Depends(get_current_uid),
) -> FileResponse:
    """Serve a preview JPEG.

    Default ``generate=false`` so library grids never block on yt-dlp / ffmpeg.
    Pass ``?generate=1`` (or a specific ``at=``) when a real frame is required.
    """
    src = _get_source(uid, source_id)
    # Explicit timestamp implies the caller wants a real extract.
    do_gen = bool(generate) or at is not None
    frame_path = _ensure_preview_frame(uid, src, at_seconds=at, generate=do_gen)
    versioned = bool((request.query_params.get("v") or "").strip())
    return FileResponse(
        frame_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": (
                "private, max-age=31536000, immutable"
                if versioned else "private, max-age=3600"
            )
        },
    )


@router.get("/sources/{source_id}/hover")
def source_hover(
    source_id: str,
    request: Request,
    uid: str = Depends(get_current_uid),
) -> FileResponse:
    """Serve the small, silent library hover clip (never the full source)."""
    src = _get_source(uid, source_id)
    hover = library.ensure_hover_preview(uid, src)
    versioned = bool((request.query_params.get("v") or "").strip())
    return FileResponse(
        hover,
        media_type="video/mp4",
        headers={
            "Cache-Control": (
                "private, max-age=31536000, immutable"
                if versioned else "private, max-age=3600"
            )
        },
    )


@router.get("/sources/{source_id}/media")
def source_media(source_id: str, uid: str = Depends(get_current_uid)) -> FileResponse:
    src = _get_source(uid, source_id)
    video = _ensure_source_file(uid, src)
    return _inline_video_response(video, src.get("name") or video.name)


class OverlayRequest(BaseModel):
    caption: str
    width: int = 1080
    height: int = 1920
    settings: dict | None = None
    # str for legacy, or {text, xOffset, yOffset}
    endCaption: str | dict | None = None


@router.post("/preview/overlay")
def preview_overlay(req: OverlayRequest, uid: str = Depends(get_current_uid)) -> Response:
    if not req.caption.strip():
        raise HTTPException(400, "Caption is empty")
    if not (64 <= req.width <= 4096 and 64 <= req.height <= 4096):
        raise HTTPException(400, "Bad dimensions")
    # Match encode: still overlays use ≥720p so end-pill floors/ratios match final output.
    width, height = resolve_output_size(req.width, req.height)
    end = req.endCaption
    has_end = False
    if isinstance(end, dict):
        has_end = bool((end.get("text") or "").strip())
    elif end:
        has_end = bool(str(end).strip())
    if has_end:
        img = render_combined_overlay(
            req.caption, width, height, req.settings or {}, end_caption=end
        )
    else:
        img = render_overlay(req.caption, width, height, req.settings or {})
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


class PreviewEncodeRequest(BaseModel):
    sourceId: str
    caption: str
    settings: dict | None = None
    seconds: float = 3.0
    endCaption: dict | None = None
    crop: dict | None = None
    trim: dict | None = None
    fromEnd: bool = False
    full: bool = False


@router.post("/preview/encode")
async def preview_encode(
    req: PreviewEncodeRequest, uid: str = Depends(get_current_uid)
) -> FileResponse:
    src = _get_source(uid, req.sourceId)
    if not req.caption.strip():
        raise HTTPException(400, "Caption is empty")
    video = _ensure_source_file(uid, src)
    out = user_previews_dir(uid) / f"preview_{uuid.uuid4().hex[:8]}.mp4"
    from ..encode import BurnedCaptionError, burn_caption

    end = req.endCaption
    if end is None and isinstance(src.get("endCaption"), dict):
        ec = src["endCaption"]
        if ec.get("enabled") and (ec.get("text") or "").strip():
            end = ec

    crop = crop_payload(req.crop) if req.crop is not None else crop_payload(src.get("crop"))
    duration = float(src.get("duration") or 0)
    trim = (
        trim_payload(req.trim, duration)
        if req.trim is not None
        else trim_payload(src.get("trim"), duration)
    )
    trim_start = float(trim["start"]) if trim else 0.0
    trim_end = float(trim["end"]) if trim else duration
    trim_len = max(0.1, trim_end - trim_start)

    seek_start = trim_start
    end_always_on = False
    if req.full:
        # Full trimmed clip (same path as a one-off render of this pair).
        seconds = trim_len
        seek_start = trim_start
    elif req.fromEnd and trim_len > 0:
        seconds = min(max(req.seconds, 1.0), 10.0)
        end_dur = 3.0
        if end and end.get("durationSeconds"):
            try:
                end_dur = float(end["durationSeconds"])
            except (TypeError, ValueError):
                end_dur = 3.0
        end_dur = max(2.0, min(5.0, end_dur))
        seconds = min(seconds, end_dur + 0.25, trim_len)
        seek_start = max(trim_start, trim_end - seconds)
        end_always_on = True
    else:
        # Mid-clip preview stays inside the trim window.
        seconds = min(max(req.seconds, 1.0), 10.0, trim_len)
        seek_start = trim_start

    try:
        await run_in_threadpool(
            burn_caption,
            video,
            req.caption,
            out,
            req.settings or {},
            duration_limit=seconds,
            end_caption=end,
            seek_start=seek_start,
            end_always_on=end_always_on,
            crop=crop,
        )
    except BurnedCaptionError as exc:
        try:
            library.set_burned_caption(uid, req.sourceId, True, reason="render-check")
        except Exception:
            pass
        raise HTTPException(409, str(exc)) from exc
    except EncodeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return _inline_video_response(out, "preview.mp4")


