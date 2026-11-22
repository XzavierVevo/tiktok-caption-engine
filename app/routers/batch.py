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
from .. import job_runtime
from .. import jobs
from .. import library
from .. import provision_jobs
from .. import remote_login
from .. import spark_jobs
from .. import storage
from .. import tiktok_accounts
from .. import tiktok_upload
from ..auth import get_current_uid
from ..config import (
    CHAT_ATTACH_MAX,
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_MB,
    SOFT_JOB_CAP,
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
    nvenc_available,
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

router = APIRouter(dependencies=[Depends(get_current_uid)])

# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

class BatchRequest(BaseModel):
    sourceIds: list[str]
    captions: list[str]
    mode: str = "matrix"  # matrix | pair
    settings: dict | None = None
    nameTemplate: str | None = None
    skipExisting: bool = True
    dedupe: bool = True
    # Optional client overrides keyed by source id (else uses session endCaption).
    endCaptions: dict[str, dict] | None = None
    # Master toggle: if False, skip all end captions regardless of per-source settings.
    useEndCaptions: bool = True
    crops: dict[str, dict] | None = None
    trims: dict[str, dict] | None = None
    # Optional explicit pairs from Chat Preview checkboxes:
    # [{sourceId, caption}] — when set, mode expansion is skipped.
    pairs: list[dict] | None = None
    createdVia: str | None = None
    chatThreadId: str | None = None
    offerId: str | None = None


def _active_end_caption(src: dict, overrides: dict | None) -> dict | None:
    raw = None
    if overrides and src["id"] in overrides:
        raw = overrides[src["id"]]
    elif isinstance(src.get("endCaption"), dict):
        raw = src["endCaption"]
    if not raw:
        return None
    if raw.get("enabled") is False:
        return None
    text = (raw.get("text") or "").strip()
    if not text:
        return None
    try:
        dur = float(raw.get("durationSeconds") or 3.0)
    except (TypeError, ValueError):
        dur = 3.0
    try:
        x_off = float(raw.get("xOffset") or 0.0)
    except (TypeError, ValueError):
        x_off = 0.0
    try:
        y_off = float(raw.get("yOffset") or 0.0)
    except (TypeError, ValueError):
        y_off = 0.0
    return {
        "text": text,
        "durationSeconds": max(2.0, min(5.0, dur)),
        "xOffset": max(-40.0, min(40.0, x_off)),
        "yOffset": max(-40.0, min(40.0, y_off)),
    }


def _batch_videos(uid: str, req: BatchRequest) -> list[dict]:
    state = _state(uid)
    videos = []
    for sid in req.sourceIds:
        src = state["sources"].get(sid)
        if not src:
            raise HTTPException(404, f"Source not found: {sid}")
        if src.get("burnedCaption"):
            raise HTTPException(
                409,
                f"{src.get('name') or sid} already has a caption burned in and is "
                "blocked from rendering. Remove it from the job and use a clean base clip.",
            )
        # Shallow copy so batch endCaption overrides don't mutate the session.
        v = dict(src)
        # Master toggle: skip end captions if useEndCaptions is False
        end = _active_end_caption(src, req.endCaptions) if req.useEndCaptions else None
        v["endCaption"] = end
        if req.crops and src["id"] in req.crops:
            v["crop"] = crop_payload(req.crops[src["id"]])
        else:
            v["crop"] = crop_payload(src.get("crop"))
        dur = float(src.get("duration") or 0)
        if req.trims and src["id"] in req.trims:
            v["trim"] = trim_payload(req.trims[src["id"]], dur)
        else:
            v["trim"] = trim_payload(src.get("trim"), dur)
        videos.append(v)
    skip_dedupe = bool(req.pairs)
    if req.dedupe and req.mode == "matrix" and not skip_dedupe:
        seen: set[str] = set()
        videos = [v for v in videos if not (v["hash"] in seen or seen.add(v["hash"]))]
    return videos


def _pairs_override(
    videos: list[dict], pair_dicts: list[dict] | None
) -> list[tuple[dict, int, str, int]] | None:
    if not pair_dicts:
        return None
    by_id = {v["id"]: (v, i) for i, v in enumerate(videos)}
    out: list[tuple[dict, int, str, int]] = []
    for i, p in enumerate(pair_dicts):
        sid = (p or {}).get("sourceId")
        cap = ((p or {}).get("caption") or "").strip()
        if not sid or not cap or sid not in by_id:
            continue
        v, vi = by_id[sid]
        if (p or {}).get("pinUrl") or (p or {}).get("sourceName"):
            v = dict(v)
            if (p or {}).get("pinUrl"):
                v["pinUrl"] = p.get("pinUrl")
        out.append((v, vi, cap, i))
    return out


def _ordered_ids(pairs: list[dict]) -> list[str]:
    seen: list[str] = []
    for p in pairs:
        sid = (p or {}).get("sourceId")
        if sid and sid not in seen:
            seen.append(sid)
    return seen


def _origin_kwargs(
    uid: str,
    req: BatchRequest | None,
    *,
    created_via: str,
    chat_thread_id: str = "",
) -> dict:
    from ..hooks import offer_snapshot
    from .. import workspace as ws

    offer_id = ""
    if req is not None:
        offer_id = (req.offerId or "").strip()
        if req.createdVia:
            created_via = req.createdVia
        if req.chatThreadId:
            chat_thread_id = req.chatThreadId
    if not offer_id:
        offer_id = (ws.load_workspace(uid).get("offerId") or "").strip()
    snap = offer_snapshot(uid, offer_id)
    via = (created_via or "unknown").strip().lower()
    if via not in ("create", "chat", "unknown"):
        via = "unknown"
    return {
        "created_via": via,
        "chat_thread_id": (chat_thread_id or "").strip(),
        "offer_id": snap.get("offer_id") or offer_id,
        "offer_name": snap.get("offer_name") or "",
        "payout_type": snap.get("payout_type") or "unknown",
        "payout_amount": snap.get("payout_amount") or "",
        "payout_share_percent": snap.get("payout_share_percent") or "",
        "payout_currency": snap.get("payout_currency") or "USD",
        "payout_note": snap.get("payout_note") or "",
    }


@router.post("/batch/estimate")
def batch_estimate(req: BatchRequest, uid: str = Depends(get_current_uid)) -> dict:
    videos = _batch_videos(uid, req)
    captions = [c for c in (c.strip() for c in req.captions) if c]
    if req.pairs is not None:
        override = _pairs_override(videos, req.pairs) or []
        est = jobs.estimate_seconds(videos, captions, req.mode, pairs_override=override)
        return {
            "jobs": len(override),
            "estimatedSeconds": round(est),
            "overCap": len(override) > SOFT_JOB_CAP,
            "encoder": "h264_nvenc" if nvenc_available() else "libx264",
        }
    if not videos or not captions:
        return {"jobs": 0, "estimatedSeconds": 0, "overCap": False}
    try:
        pairs = jobs.expand_jobs(videos, captions, req.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    est = jobs.estimate_seconds(videos, captions, req.mode)
    return {
        "jobs": len(pairs),
        "estimatedSeconds": round(est),
        "overCap": len(pairs) > SOFT_JOB_CAP,
        "encoder": "h264_nvenc" if nvenc_available() else "libx264",
    }


@router.post("/batch/resolve-outputs")
def batch_resolve_outputs(req: BatchRequest, uid: str = Depends(get_current_uid)) -> dict:
    """Return already-rendered output paths for plan pairs (no encode)."""
    if not req.pairs:
        raise HTTPException(400, "pairs required")
    source_ids = req.sourceIds or _ordered_ids(req.pairs)
    if not source_ids:
        raise HTTPException(400, "No source videos in pairs")
    shaped = BatchRequest(
        sourceIds=source_ids,
        captions=req.captions
        or [str((p or {}).get("caption") or "") for p in req.pairs],
        mode=req.mode or "custom",
        settings=req.settings,
        nameTemplate=req.nameTemplate,
        skipExisting=True,
        dedupe=False,
        endCaptions=req.endCaptions,
        crops=req.crops,
        trims=req.trims,
        pairs=req.pairs,
    )
    try:
        videos = _batch_videos(uid, shaped)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    override = _pairs_override(videos, shaped.pairs) or []
    if not override:
        raise HTTPException(400, "No valid pairs")
    result = jobs.resolve_existing_outputs(uid, override, shaped.settings or {})
    return {"ok": True, **result}


@router.post("/batch/start")
def batch_start(req: BatchRequest, uid: str = Depends(get_current_uid)) -> dict:
    videos = _batch_videos(uid, req)
    captions = [c for c in (c.strip() for c in req.captions) if c]
    if not videos:
        raise HTTPException(400, "No videos selected")
    if not captions and not req.pairs:
        raise HTTPException(400, "No captions provided")
    pairs_override = _pairs_override(videos, req.pairs)
    if req.pairs is not None:
        pairs_override = pairs_override or []
        if not pairs_override:
            raise HTTPException(400, "No valid pairs to render")
    try:
        batch = jobs.start_batch(
            uid,
            videos,
            captions or [p[2] for p in (pairs_override or [])],
            req.mode,
            req.settings or {},
            req.nameTemplate,
            skip_existing=req.skipExisting,
            pairs_override=pairs_override,
            **_origin_kwargs(uid, req, created_via=req.createdVia or "create"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "batchId": batch.id,
        "status": batch.status,
        "queuePosition": job_runtime.batches.queue_position(batch),
        "jobs": len(batch.items),
        "outputDir": batch.date_label,
        "outputFolder": Path(batch.output_dir).name,
        "estimatedSeconds": round(
            sum(max(0.1, float(i.duration or 30.0)) * encode_factor() for i in batch.items)
        ),
    }


@router.get("/batch/{batch_id}")
def batch_status(batch_id: str, uid: str = Depends(get_current_uid)) -> dict:
    batch = jobs.get_batch(batch_id, uid)
    if not batch:
        raise HTTPException(404, "Batch not found")
    return jobs.batch_state(batch)


@router.get("/jobs/active")
def jobs_active(uid: str = Depends(get_current_uid)) -> dict:
    """All in-flight work for UI resume after tab close / refresh.

    Jobs run in server daemon threads — closing the browser never cancels them.
    The frontend reattaches by polling this endpoint on boot.
    """
    batches = [
        jobs.batch_state(b)
        for b in job_runtime.batches.list_for(uid, statuses=("running", "queued"))
    ]
    uploads = [
        tiktok_upload.job_state(u)
        for u in job_runtime.uploads.list_for(uid, statuses=("running", "queued"))
    ]
    facebook = [
        facebook_jobs.job_state(j)
        for j in job_runtime.facebook.list_for(uid, statuses=("running", "queued"))
    ]
    provisions = [
        provision_jobs.job_state(j)
        for j in job_runtime.provisions.list_for(uid, statuses=("running", "queued"))
    ]
    sparks = [
        spark_jobs.job_state(j)
        for j in job_runtime.sparks.list_for(uid, statuses=("running", "queued"))
    ]
    logins = remote_login.list_active(uid)
    hook_jobs = hooks.list_active_generate_jobs(uid)
    out = {
        "batches": batches,
        "uploads": uploads,
        "facebook": facebook,
        "provisions": provisions,
        "sparks": sparks,
        "logins": logins,
        "hooks": hook_jobs,
    }
    try:
        from ..edge_cache import publish_jobs_active

        publish_jobs_active(uid, out)
    except Exception:
        pass
    return out


@router.post("/batch/{batch_id}/cancel")
def batch_cancel(batch_id: str, uid: str = Depends(get_current_uid)) -> dict:
    if not jobs.cancel_batch(batch_id, uid):
        raise HTTPException(404, "Batch not found")
    return {"ok": True}


@router.get("/catalog")
def catalog_list(
    uid: str = Depends(get_current_uid),
    createdVia: str | None = None,
    destination: str | None = None,
    onTiktok: str | None = None,
    onFacebook: str | None = None,
    offerId: str | None = None,
    status: str | None = None,
    dateFrom: str | None = Query(None, alias="from"),
    dateTo: str | None = Query(None, alias="to"),
    sort: str = "-created_at",
    limit: int = 500,
) -> dict:
    from .. import catalog

    def _flag(raw: str | None) -> bool | None:
        if raw is None or raw == "":
            return None
        return raw.strip().lower() in ("1", "true", "yes")

    rows = catalog.list_renders(
        uid,
        created_via=createdVia,
        destination=destination,
        on_tiktok=_flag(onTiktok),
        on_facebook=_flag(onFacebook),
        offer_id=offerId,
        status=status,
        date_from=dateFrom,
        date_to=dateTo,
        sort=sort,
        limit=limit,
    )
    return {"items": rows, "count": len(rows)}


@router.get("/catalog.csv")
def catalog_csv_download(uid: str = Depends(get_current_uid)) -> Response:
    from .. import catalog

    rows = catalog.list_renders(uid, limit=2000)
    return Response(
        content=catalog.to_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="renders.csv"',
        },
    )


@router.get("/batch/{batch_id}/zip")
def batch_zip(batch_id: str, uid: str = Depends(get_current_uid)) -> Response:
    batch = jobs.get_batch(batch_id, uid)
    if not batch:
        raise HTTPException(404, "Batch not found")
    outputs: list[Path] = []
    raw_items = getattr(batch, "items", None)
    if raw_items is None and hasattr(batch, "as_dict"):
        raw_items = (batch.as_dict() or {}).get("items") or []
    for i in raw_items or []:
        if isinstance(i, dict):
            output = i.get("output")
            status = i.get("status")
        else:
            output = getattr(i, "output", None)
            status = getattr(i, "status", None)
        if not output or status not in ("done", "skipped"):
            continue
        p = Path(str(output))
        if not p.is_file():
            try:
                restored = storage.ensure_local(uid, storage.rel_of(uid, p))
            except ValueError:
                restored = None
            if not restored:
                continue
            p = restored
        outputs.append(p)
    if not outputs:
        raise HTTPException(400, "No finished outputs in this batch")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for p in outputs:
            zf.write(p, p.name)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="captions_{stamp}.zip"'
        },
    )


# ---------------------------------------------------------------------------
# Outputs (per user; durable in R2)
# ---------------------------------------------------------------------------

@router.get("/outputs")
def list_outputs(limit: int = 200, uid: str = Depends(get_current_uid)) -> dict:
    try:
        items = catalog.list_output_items(uid, limit=limit)
    except Exception as exc:
        raise HTTPException(500, f"Could not list outputs: {exc}") from exc
    for item in items:
        url = storage.presign(
            uid, f"output/{item['path']}", filename=item["filename"]
        )
        item["downloadUrl"] = url  # None → frontend falls back to /api/output/…
    return {"items": items, "count": len(items)}


class DeleteOutputsRequest(BaseModel):
    paths: list[str]


@router.post("/outputs/delete")
def delete_outputs(req: DeleteOutputsRequest, uid: str = Depends(get_current_uid)) -> dict:
    out_root = user_output_dir(uid).resolve()
    deleted = 0
    rels: list[str] = []
    for raw in req.paths[:500]:
        p = (user_output_dir(uid) / raw).resolve()
        try:
            p.relative_to(out_root)
        except ValueError:
            continue
        rel = f"output/{Path(raw).as_posix()}"
        rels.append(rel)
        if p.is_file():
            try:
                p.unlink()
                deleted += 1
            except OSError:
                pass
    storage.delete_remote_many(uid, rels)
    return {"ok": True, "deleted": deleted}


@router.get("/output/{filename:path}")
def get_output(filename: str, uid: str = Depends(get_current_uid)) -> FileResponse:
    out_dir = user_output_dir(uid)
    path = (out_dir / filename).resolve()
    try:
        path.relative_to(out_dir.resolve())
    except ValueError as exc:
        raise HTTPException(404, "File not found") from exc
    if not path.is_file():
        restored = storage.ensure_local(uid, f"output/{Path(filename).as_posix()}")
        if not restored:
            raise HTTPException(404, "File not found")
        path = restored
    return _inline_video_response(path)


