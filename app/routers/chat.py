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
from .. import job_runtime
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
    VIDEO_EXTENSIONS,
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
    extract_frame,
    probe,
    resolve_output_size,
    trim_payload,
)
from ..ingest import IngestError, download_url, save_image, save_upload, validate_url
from ..layout import render_combined_overlay, render_overlay
from ..facebook_ads import FacebookError
from ..tiktok_accounts import AccountError
from .support import (
    _annotate_source,
    _attach_payload_for_source,
    _backfill_library,
    _chat_image_path,
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
from .batch import (
    BatchRequest,
    _batch_videos,
    _ordered_ids,
    _origin_kwargs,
    _pairs_override,
)

router = APIRouter(dependencies=[Depends(get_current_uid)])
_log = logging.getLogger(__name__)

@router.post("/chat/attach")
async def chat_attach_file(
    file: UploadFile = File(...), uid: str = Depends(get_current_uid)
) -> dict:
    """Upload a video or image for chat context (first frame / still only to the model)."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    filename = file.filename or "file"
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        try:
            path = await run_in_threadpool(save_image, uid, filename, data)
        except IngestError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            storage.mirror_up(uid, path)
        except Exception:
            pass
        library_id = ""
        try:
            from .. import images as image_lib

            registered = await run_in_threadpool(
                image_lib.register_image, uid, path, "chat", None
            )
            library_id = str((registered or {}).get("id") or "")
        except Exception:
            pass
        slim = _slim_image_attachment(path.name, filename, library_id)
        slim["thumbUrl"] = (
            f"/api/images/{library_id}/file" if library_id else f"/api/chat/images/{path.name}"
        )
        return slim
    if ext not in VIDEO_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")
    try:
        path = await run_in_threadpool(save_upload, uid, filename, data)
        src = await run_in_threadpool(_register_source, uid, path, "upload", None, False)
        return _attach_payload_for_source(src)
    except (IngestError, EncodeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/chat/images/{image_id}")
def chat_image(image_id: str, uid: str = Depends(get_current_uid)) -> FileResponse:
    path = _chat_image_path(uid, image_id)
    media = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(path, media_type=media)


@router.get("/chat/attach/source/{source_id}")
def chat_attach_source(source_id: str, uid: str = Depends(get_current_uid)) -> dict:
    src = _get_source(uid, source_id)
    return _attach_payload_for_source(src)


@router.get("/chat/attach/image/{image_id}")
def chat_attach_library_image(image_id: str, uid: str = Depends(get_current_uid)) -> dict:
    """Attach an existing library still to the chat composer."""
    from .. import images as image_lib

    try:
        img = image_lib.get_image(uid, image_id)
        path = image_lib.ensure_image_file(uid, img)
    except KeyError as exc:
        raise HTTPException(404, "Image not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    lid = str(img.get("id") or image_id)
    slim = _slim_image_attachment(path.name, img.get("name") or path.name, lid)
    slim["thumbUrl"] = f"/api/images/{lid}/file"
    slim["width"] = img.get("width") or 0
    slim["height"] = img.get("height") or 0
    slim["origin"] = img.get("origin") or "upload"
    return slim


# ---------------------------------------------------------------------------
# Settings: yt-dlp cookies for URL downloads
# ---------------------------------------------------------------------------

@router.post("/settings/ytdlp-cookies")
async def upload_ytdlp_cookies(
    file: UploadFile = File(...), uid: str = Depends(get_current_uid)
) -> dict:
    raw = await file.read()
    if not raw or len(raw) > 1024 * 1024:
        raise HTTPException(400, "Cookie file is empty or too large")
    path = user_ytdlp_cookies_path(uid)
    path.write_bytes(raw)
    storage.mirror_up(uid, path)
    return {"ok": True}


@router.delete("/settings/ytdlp-cookies")
def delete_ytdlp_cookies(uid: str = Depends(get_current_uid)) -> dict:
    path = user_ytdlp_cookies_path(uid)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    storage.mirror_delete(uid, "ytdlp_cookies.txt")
    return {"ok": True}




# ---------------------------------------------------------------------------
# Chat (multi-thread agent)
# ---------------------------------------------------------------------------

class ChatThreadCreate(BaseModel):
    title: str | None = None
    model: str | None = None
    providerModels: dict[str, str] | None = None


class ChatThreadUpdate(BaseModel):
    title: str | None = None
    model: str | None = None
    providerModels: dict[str, str] | None = None


class ChatAttachment(BaseModel):
    kind: Literal["source", "image", "pin"] = "source"
    sourceId: str = ""
    imageId: str = ""
    libraryId: str = ""
    name: str = ""
    pinUrl: str = ""
    thumbUrl: str = ""
    title: str = ""
    previewUrl: str = ""
    previewKind: str = ""
    width: int | float = 0
    height: int | float = 0
    duration: int | float = 0


class ChatMessageRequest(BaseModel):
    content: str
    model: str | None = None
    providerModels: dict[str, str] | None = None
    models: list[str] | None = None
    approvedIds: list[str] = []
    deniedIds: list[str] = []
    attachments: list[ChatAttachment] = []


class ChatConfirmRequest(BaseModel):
    accept: bool = True


class ChatReviewRequest(BaseModel):
    action: Literal["submit", "rewrite", "replace_video", "proceed", "save"] = "submit"
    approvedIds: list[str] = []
    deniedIds: list[str] = []
    ideaIds: list[str] = []
    feedback: str = ""


class ChatReviewChoicesRequest(BaseModel):
    approvedIds: list[str] = []
    deniedIds: list[str] = []


class ChatPickCaptionsRequest(BaseModel):
    captions: list[str]
    count: int = 8
    model: str | None = None
    prompt: str = ""
    threadId: str = ""


def _tool_context(thread_id: str = ""):
    from ..agent import tools as toolreg
    from .. import workspace as ws

    def _list_sources(uid: str) -> list[dict]:
        # Chat can operate without touching Create's job list — expose the full library.
        return _library_items(uid)

    def _list_library(uid: str) -> list[dict]:
        return _library_items(uid)

    def _delete_source(uid: str, source_id: str) -> None:
        _unlink_source(uid, source_id)

    def _register_downloaded(
        uid: str,
        path: Path,
        origin: str,
        url: str | None = None,
        add_to_job: bool = False,
    ) -> dict:
        # Default off so chat downloads stay in the library until Send to Create.
        return _register_source(uid, path, origin, url=url, add_to_job=add_to_job)

    def _set_crop(uid: str, source_id: str, crop: dict | None) -> dict:
        src = _get_source(uid, source_id)
        src["crop"] = crop_payload(crop)
        _save_session(uid)
        return src

    def _set_end(uid: str, source_id: str, payload: dict) -> dict:
        src = _get_source(uid, source_id)
        src["endCaption"] = {
            "enabled": bool(payload.get("enabled", True)),
            "text": (payload.get("text") or "").strip(),
            "durationSeconds": max(2.0, min(5.0, float(payload.get("durationSeconds") or 3))),
            "xOffset": max(-40.0, min(40.0, float(payload.get("xOffset") or 0))),
            "yOffset": max(-40.0, min(40.0, float(payload.get("yOffset") or 0))),
        }
        _save_session(uid)
        return src

    def _list_outs(uid: str, limit: int) -> dict:
        items = catalog.list_output_items(uid, limit=limit)
        for item in items:
            item["downloadUrl"] = storage.presign(
                uid, f"output/{item['path']}", filename=item["filename"]
            )
        return {"items": items, "count": len(items)}

    def _del_outs(uid: str, paths: list[str]) -> dict:
        out_root = user_output_dir(uid).resolve()
        deleted = 0
        rels: list[str] = []
        for raw in paths[:500]:
            p = (user_output_dir(uid) / raw).resolve()
            try:
                p.relative_to(out_root)
            except ValueError:
                continue
            rels.append(f"output/{Path(raw).as_posix()}")
            if p.is_file():
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
        storage.delete_remote_many(uid, rels)
        return {"deleted": deleted}

    def _start_from_pairs(uid: str, args: dict) -> dict:
        from .. import chat_store

        w = ws.load_workspace(uid)
        plan = chat_store.get_thread_plan(uid, thread_id) if thread_id else {}
        captions = ws.caption_list(plan) or ws.caption_list(w)
        state = _state(uid)
        pairs_arg = args.get("pairs")
        if not pairs_arg:
            stored = (plan.get("renderPairs") or []) if thread_id else (w.get("renderPairs") or [])
            if stored:
                pairs_arg = stored
        mode = args.get("mode") or (plan.get("mode") if thread_id else None) or w.get("mode") or "matrix"
        if pairs_arg:
            if pairs_arg and all(
                isinstance(p, dict) and (p.get("kind") == "carousel" or p.get("slides"))
                for p in pairs_arg
            ):
                raise ValueError(
                    "This is a photo carousel plan — use start_carousel_upload, not start_render"
                )
            source_ids = _ordered_ids(pairs_arg)
            missing = [sid for sid in source_ids if sid not in state["sources"]]
            if missing:
                raise ValueError(f"Source not found: {missing[0]}")
            if not source_ids:
                raise ValueError("No source videos in plan — add or download clips first")
            caps = [p.get("caption") or "" for p in pairs_arg]
            mode = "custom"
        else:
            srcs = [
                state["sources"][sid]
                for sid in state["order"]
                if sid in state["sources"]
            ]
            if not srcs:
                raise ValueError("No source videos — add videos first")
            source_ids = [s["id"] for s in srcs]
            caps = captions
            if mode == "custom":
                mode = "matrix"
        if not caps:
            raise ValueError("No captions in the chat plan")
        req = BatchRequest(
            sourceIds=source_ids,
            captions=caps,
            mode=mode,
            settings=w.get("style") or {},
            nameTemplate=w.get("nameTemplate"),
            skipExisting=bool(w.get("skipExisting", True)),
            dedupe=bool(w.get("dedupe", True)),
            pairs=pairs_arg,
            createdVia="chat",
            chatThreadId=thread_id,
            offerId=w.get("offerId") or "",
        )
        videos = _batch_videos(uid, req)
        pairs_override = _pairs_override(videos, req.pairs)
        if req.pairs is not None:
            pairs_override = pairs_override or []
            if not pairs_override:
                raise ValueError("No valid pairs to render")
        batch = jobs.start_batch(
            uid,
            videos,
            caps,
            mode,
            w.get("style") or {},
            w.get("nameTemplate"),
            skip_existing=bool(w.get("skipExisting", True)),
            pairs_override=pairs_override,
            **_origin_kwargs(
                uid,
                req,
                created_via="chat",
                chat_thread_id=thread_id,
            ),
        )
        return {
            "batchId": batch.id,
            "status": batch.status,
            "queuePosition": job_runtime.batches.queue_position(batch),
            "jobs": len(batch.items),
            "outputFolder": Path(batch.output_dir).name,
        }

    def _build_batch_videos(uid: str, ids: list[str] | None, extra: dict | None) -> list[dict]:
        state = _state(uid)
        wanted = ids or [sid for sid in state["order"] if sid in state["sources"]]
        extra = extra or {}
        out = []
        for sid in wanted:
            src = state["sources"].get(sid)
            if src:
                row = dict(src)
                row.update(extra)
                out.append(row)
        return out

    return toolreg.ToolContext(
        list_sources=_list_sources,
        delete_source=_delete_source,
        register_downloaded=_register_downloaded,
        set_end_caption=_set_end,
        list_outputs=_list_outs,
        delete_outputs=_del_outs,
        build_batch_videos=_build_batch_videos,
        start_batch_from_pairs=_start_from_pairs,
        set_source_crop=_set_crop,
        list_library=_list_library,
        thread_id=thread_id,
    )


@router.get("/chat/usage")
def chat_usage(
    range: Literal["session", "today", "week", "lifetime"] = "session",
    threadId: str = "",
    tz: str = "",
    utcOffset: int = 0,
    uid: str = Depends(get_current_uid),
) -> dict:
    from .. import usage as usage_mod

    return usage_mod.summarize(
        uid,
        range_name=range,
        thread_id=threadId,
        tz_name=tz,
        utc_offset_min=utcOffset,
    )


@router.get("/chat/models")
def chat_models_list(uid: str = Depends(get_current_uid)) -> dict:
    from .. import chat_models
    from .. import workspace as ws
    from ..agent import loop as agent_loop

    saved = ws.load_workspace(uid)
    return {
        "models": chat_models.list_models(),
        "providers": chat_models.provider_status(),
        "aliases": chat_models.MODEL_ALIASES,
        "commands": agent_loop.list_slash_commands(),
        "prefs": {
            "provider": saved.get("chatProvider") or "",
            "models": chat_models.normalize_provider_models(saved.get("chatModels")),
        },
    }


@router.get("/chat/threads")
def chat_list_threads(uid: str = Depends(get_current_uid)) -> list[dict]:
    from .. import chat_store

    start_time = time.time()

    threads = chat_store.list_threads(uid)

    elapsed = time.time() - start_time
    if elapsed > 0.5:  # Log slow requests
        _log.warning(
            "Slow /chat/threads request: uid=%s, count=%d, elapsed=%.2fs",
            uid[:8] if uid else "?",
            len(threads),
            elapsed
        )

    return threads


@router.post("/chat/threads")
def chat_create_thread(
    req: ChatThreadCreate, uid: str = Depends(get_current_uid)
) -> dict:
    from .. import chat_models
    from .. import chat_store

    model_id = req.model
    provider = ""
    provider_models = chat_models.normalize_provider_models(req.providerModels)
    try:
        m = chat_models.resolve_model(model_id)
        model_id = m["id"]
        provider = m["provider"]
        provider_models[provider] = model_id
    except ValueError:
        model_id = req.model or ""
    return chat_store.create_thread(
        uid,
        title=req.title,
        model=model_id,
        provider=provider,
        provider_models=provider_models,
    )


@router.get("/chat/threads/{thread_id}")
def chat_get_thread(thread_id: str, uid: str = Depends(get_current_uid)) -> dict:
    import logging
    import time
    from .. import chat_store

    _log = logging.getLogger(__name__)
    start_time = time.time()

    try:
        thread = chat_store.get_thread(uid, thread_id)

        elapsed = time.time() - start_time
        if elapsed > 1.0:  # Log slow requests
            msg_count = len(thread.get("messages", []))
            _log.warning(
                "Slow /chat/threads/%s request: messages=%d, elapsed=%.2fs",
                thread_id[:8],
                msg_count,
                elapsed
            )

        return thread
    except chat_store.StorageUnavailable as exc:
        raise HTTPException(
            503,
            f"Cloud storage temporarily unavailable. Please try again in a moment. ({exc})",
        ) from exc
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc


@router.get("/chat/threads/{thread_id}/plan")
def chat_get_thread_plan(thread_id: str, uid: str = Depends(get_current_uid)) -> dict:
    from .. import chat_store

    try:
        return chat_store.get_thread_plan(uid, thread_id)
    except chat_store.StorageUnavailable as exc:
        raise HTTPException(503, f"Cloud storage temporarily unavailable. Please try again in a moment. ({exc})") from exc
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc


@router.get("/chat/threads/{thread_id}/plan/tiktok-status")
def chat_plan_tiktok_status(
    thread_id: str,
    uid: str = Depends(get_current_uid),
    plan_id: str | None = None,
) -> dict:
    from .. import chat_store

    try:
        return chat_store.plan_tiktok_status(uid, thread_id, plan_id)
    except chat_store.StorageUnavailable as exc:
        raise HTTPException(503, f"Cloud storage temporarily unavailable. Please try again in a moment. ({exc})") from exc
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc


class ChatPlanSparkPair(BaseModel):
    sourceId: str
    caption: str


class ChatPlanFetchSparkRequest(BaseModel):
    planId: str | None = None
    pairs: list[ChatPlanSparkPair] = []


@router.post("/chat/threads/{thread_id}/plan/fetch-spark")
def chat_plan_fetch_spark(
    thread_id: str,
    req: ChatPlanFetchSparkRequest,
    uid: str = Depends(get_current_uid),
) -> dict:
    from .. import spark_jobs
    from ..duoplus import DuoPlusError, configured, format_duoplus_user_error
    from ..chat_store import StorageUnavailable

    if not configured():
        raise HTTPException(
            503,
            "DuoPlus is not configured. Set DUOPLUS_API_KEY in .env.",
        )
    try:
        job = spark_jobs.start_plan_spark(
            uid,
            thread_id,
            plan_id=req.planId,
            pairs=[p.model_dump() for p in req.pairs],
        )
    except DuoPlusError as exc:
        raise HTTPException(400, format_duoplus_user_error(exc)) from exc
    except StorageUnavailable as exc:
        # Cloud storage (D1/Firestore) is unreachable — tell the user to retry
        raise HTTPException(
            503,
            f"Cloud storage temporarily unavailable. Please try again in a moment. ({exc})"
        ) from exc
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc
    return spark_jobs.job_state(job)


@router.get("/chat/threads/{thread_id}/plan/spark-status")
def chat_plan_spark_status(
    thread_id: str,
    uid: str = Depends(get_current_uid),
    plan_id: str | None = None,
) -> dict:
    from .. import chat_store
    from .. import spark_jobs

    try:
        payload = chat_store.plan_spark_status(uid, thread_id, plan_id)
    except chat_store.StorageUnavailable as exc:
        raise HTTPException(503, f"Cloud storage temporarily unavailable. Please try again in a moment. ({exc})") from exc
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc
    # Keep live Spark progress scoped to the plan being displayed.  A chat can
    # have several plans fetching codes at once.
    job = spark_jobs.latest_for_thread(uid, thread_id, plan_id)
    if job:
        payload["job"] = spark_jobs.job_state(job)
    return payload


class ChatThreadPlanUpdate(BaseModel):
    renderPairs: list[dict] | None = None
    captions: str | None = None
    captionMeta: list[dict] | None = None
    mode: str | None = None
    title: str | None = None
    batchId: str | None = None
    tiktokAccountId: str | None = None
    tiktokAccountName: str | None = None
    newPlan: bool = False
    planId: str | None = None


@router.get("/chat/threads/{thread_id}/plans")
def chat_list_thread_plans(thread_id: str, uid: str = Depends(get_current_uid)) -> dict:
    from .. import chat_store

    try:
        return chat_store.list_thread_plans(uid, thread_id)
    except chat_store.StorageUnavailable as exc:
        raise HTTPException(503, f"Cloud storage temporarily unavailable. Please try again in a moment. ({exc})") from exc
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc


@router.post("/chat/threads/{thread_id}/plans")
def chat_create_thread_plan(
    thread_id: str,
    req: ChatThreadPlanUpdate,
    uid: str = Depends(get_current_uid),
) -> dict:
    from .. import chat_store

    try:
        chat_store.get_thread(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc
    patch = req.model_dump(exclude_unset=True)
    patch.pop("newPlan", None)
    patch.pop("planId", None)
    title = str(patch.pop("title", "") or "")
    if not patch:
        patch = {
            "renderPairs": [],
            "captions": "",
            "captionMeta": [],
            "mode": "custom",
        }
    chat_store.save_thread_plan(uid, thread_id, patch, new_plan=True, title=title)
    return chat_store.list_thread_plans(uid, thread_id)


class ChatThreadPlanMeta(BaseModel):
    title: str | None = None
    active: bool | None = None


@router.patch("/chat/threads/{thread_id}/plans/{plan_id}")
def chat_patch_thread_plan_meta(
    thread_id: str,
    plan_id: str,
    req: ChatThreadPlanMeta,
    uid: str = Depends(get_current_uid),
) -> dict:
    from .. import chat_store, plans as plan_store

    try:
        if req.title is not None:
            plan_store.update_plan(uid, plan_id, {"name": req.title})
        if req.active:
            plan_store.activate_plan(uid, plan_id)
            chat_store.save_thread_plan(uid, thread_id, {}, plan_id=plan_id)
        return chat_store.list_thread_plans(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "Plan not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/chat/threads/{thread_id}/plans/{plan_id}")
def chat_delete_thread_plan_route(
    thread_id: str, plan_id: str, uid: str = Depends(get_current_uid)
) -> dict:
    from .. import chat_store, plans as plan_store

    try:
        chat_store.get_thread(uid, thread_id)
        plan_store.delete_plan(uid, plan_id)
        return chat_store.list_thread_plans(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "Plan not found") from exc


@router.put("/chat/threads/{thread_id}/plan")
def chat_put_thread_plan(
    thread_id: str,
    req: ChatThreadPlanUpdate,
    uid: str = Depends(get_current_uid),
) -> dict:
    from .. import chat_store

    try:
        chat_store.get_thread(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc
    patch = req.model_dump(exclude_unset=True)
    new_plan = bool(patch.pop("newPlan", False))
    plan_id = patch.pop("planId", None)
    title = str(patch.pop("title", "") or "")
    if not patch and not new_plan and not title:
        return chat_store.get_thread_plan(uid, thread_id, plan_id)
    try:
        return chat_store.save_thread_plan(
            uid, thread_id, patch, new_plan=new_plan, title=title, plan_id=plan_id
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc) or "Plan not found") from exc


@router.patch("/chat/threads/{thread_id}")
def chat_patch_thread(
    thread_id: str, req: ChatThreadUpdate, uid: str = Depends(get_current_uid)
) -> dict:
    from .. import chat_models
    from .. import chat_store

    try:
        provider = None
        model = req.model
        provider_models = chat_models.normalize_provider_models(req.providerModels)
        if model:
            try:
                m = chat_models.resolve_model(model)
                model = m["id"]
                provider = m["provider"]
                provider_models[provider] = model
            except ValueError:
                pass
        return chat_store.update_thread(
            uid,
            thread_id,
            title=req.title,
            model=model,
            provider=provider,
            provider_models=provider_models or None,
        )
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc


@router.delete("/chat/threads/{thread_id}")
def chat_delete_thread(thread_id: str, uid: str = Depends(get_current_uid)) -> dict:
    from .. import chat_store

    try:
        chat_store.delete_thread(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc
    return {"ok": True}


@router.post("/chat/threads/{thread_id}/messages")
async def chat_post_message(
    thread_id: str, req: ChatMessageRequest, uid: str = Depends(get_current_uid)
):
    from fastapi.responses import StreamingResponse

    from ..agent import loop as agent_loop
    from .. import chat_store

    try:
        chat_store.get_thread(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc

    text = (req.content or "").strip()
    att_raw = [a.model_dump() for a in (req.attachments or [])][:CHAT_ATTACH_MAX]
    prepared = _resolve_chat_attachments(uid, att_raw) if att_raw else {
        "items": [],
        "vision": [],
        "note": "",
    }
    if not text and not prepared["items"]:
        raise HTTPException(400, "Message is empty")
    if not text:
        text = "Use the attached clips/images."

    ctx = _tool_context(thread_id)

    def gen():
        yield from agent_loop.run_turn(
            uid,
            thread_id,
            text,
            model_id=req.model,
            ctx=ctx,
            approved_ids=list(req.approvedIds or []),
            denied_ids=list(req.deniedIds or []),
            provider_models=req.providerModels,
            models=list(req.models or []) or None,
            vision_images=prepared["vision"] or None,
            attachment_note=prepared["note"] or "",
            attachments=prepared["items"] or None,
        )

    return StreamingResponse(
        iterate_in_threadpool(gen()), media_type="text/event-stream; charset=utf-8"
    )


@router.post("/chat/threads/{thread_id}/confirm")
async def chat_confirm(
    thread_id: str, req: ChatConfirmRequest, uid: str = Depends(get_current_uid)
):
    from fastapi.responses import StreamingResponse

    from ..agent import loop as agent_loop
    from .. import chat_store

    try:
        chat_store.get_thread(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc

    ctx = _tool_context(thread_id)

    def gen():
        yield from agent_loop.confirm_and_continue(
            uid, thread_id, accepted=bool(req.accept), ctx=ctx
        )

    return StreamingResponse(
        iterate_in_threadpool(gen()), media_type="text/event-stream; charset=utf-8"
    )


@router.post("/chat/threads/{thread_id}/review")
async def chat_review(
    thread_id: str, req: ChatReviewRequest, uid: str = Depends(get_current_uid)
):
    from fastapi.responses import StreamingResponse

    from ..agent import loop as agent_loop
    from .. import chat_store

    try:
        chat_store.get_thread(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc

    ctx = _tool_context(thread_id)

    def gen():
        action = req.action or "submit"
        if action in ("rewrite", "replace_video"):
            yield from agent_loop.revise_ideas_and_continue(
                uid,
                thread_id,
                action=action,
                idea_ids=list(req.ideaIds or []),
                feedback=req.feedback or "",
                ctx=ctx,
            )
            return
        yield from agent_loop.review_and_continue(
            uid,
            thread_id,
            approved_ids=list(req.approvedIds or []),
            denied_ids=list(req.deniedIds or []),
            ctx=ctx,
            proceed=action == "proceed",
            save_only=action == "save",
        )

    return StreamingResponse(
        iterate_in_threadpool(gen()), media_type="text/event-stream; charset=utf-8"
    )


@router.post("/chat/threads/{thread_id}/review-choices")
def chat_review_choices(
    thread_id: str, req: ChatReviewChoicesRequest, uid: str = Depends(get_current_uid)
):
    from ..agent import loop as agent_loop
    from .. import chat_store

    try:
        chat_store.get_thread(uid, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "Thread not found") from exc
    try:
        return agent_loop.save_review_choices(
            uid,
            thread_id,
            approved_ids=list(req.approvedIds or []),
            denied_ids=list(req.deniedIds or []),
        )
    except KeyError as exc:
        raise HTTPException(409, "No pending idea review") from exc


@router.post("/chat/pick-captions")
def chat_pick_captions(req: ChatPickCaptionsRequest, uid: str = Depends(get_current_uid)) -> dict:
    from ..agent import caption_pick as cap_pick

    try:
        return cap_pick.pick_favorites(
            uid,
            captions=list(req.captions or []),
            count=req.count,
            model_id=req.model,
            prompt=req.prompt or "",
            thread_id=req.threadId or "",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc) or "Pick failed") from exc


tool_context = _tool_context

