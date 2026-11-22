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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
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
    extract_frame,
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
public_router = APIRouter()

# ---------------------------------------------------------------------------
# TikTok accounts + login + upload
# Static path segments (login/import) MUST be registered before {account_id}
# routes, or Starlette returns 405 Method Not Allowed.
# ---------------------------------------------------------------------------

@router.get("/tiktok/accounts")
def tiktok_list_accounts(uid: str = Depends(get_current_uid)) -> list[dict]:
    return tiktok_accounts.list_accounts(uid, annotate_spark=True)


class TikTokAccountCreate(BaseModel):
    name: str


@router.post("/tiktok/accounts")
def tiktok_create_account(
    req: TikTokAccountCreate, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return tiktok_accounts.create_account(uid, req.name)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


class TikTokLoginRequest(BaseModel):
    name: str
    accountId: str | None = None  # re-login existing account


@router.post("/tiktok/accounts/login")
def tiktok_start_login(req: TikTokLoginRequest, uid: str = Depends(get_current_uid)) -> dict:
    try:
        return remote_login.start_login(uid, req.name, account_id=req.accountId)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/tiktok/accounts/login/{login_id}")
def tiktok_login_status(login_id: str, uid: str = Depends(get_current_uid)) -> dict:
    sess = remote_login.get_status(login_id, uid)
    if not sess:
        raise HTTPException(404, "Login session not found")
    return sess


@router.get("/tiktok/accounts/login/{login_id}/screen")
def tiktok_login_screen(login_id: str, uid: str = Depends(get_current_uid)) -> Response:
    frame = remote_login.get_frame(login_id, uid)
    if frame is None:
        raise HTTPException(404, "No frame yet")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class RemoteInputRequest(BaseModel):
    kind: str  # click | move | scroll | text | key
    x: float | None = None
    y: float | None = None
    dy: float | None = None
    text: str | None = None
    key: str | None = None


@router.post("/tiktok/accounts/login/{login_id}/input")
def tiktok_login_input(
    login_id: str, req: RemoteInputRequest, uid: str = Depends(get_current_uid)
) -> dict:
    ok = remote_login.send_input(login_id, uid, req.model_dump(exclude_none=True))
    if not ok:
        raise HTTPException(404, "Login session not active")
    return {"ok": True}


@router.post("/tiktok/accounts/login/{login_id}/cancel")
def tiktok_login_cancel(login_id: str, uid: str = Depends(get_current_uid)) -> dict:
    if not remote_login.close_session(login_id, uid):
        raise HTTPException(404, "Login session not found")
    return {"ok": True}


class TikTokImportCookies(BaseModel):
    name: str | None = None
    accountId: str | None = None
    cookiesText: str = ""
    sessionid: str | None = None


@router.post("/tiktok/accounts/import")
def tiktok_import_cookies(
    req: TikTokImportCookies, uid: str = Depends(get_current_uid)
) -> dict:
    text = (req.cookiesText or req.sessionid or "").strip()
    if not text:
        raise HTTPException(400, "Provide cookiesText or sessionid")
    try:
        if req.accountId:
            return tiktok_accounts.import_cookies_text(uid, req.accountId, text)
        name = (req.name or "").strip() or "TikTok account"
        return tiktok_accounts.import_cookies_for_new_account(uid, name, text)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/tiktok/accounts/import-file")
async def tiktok_import_cookies_file(
    file: UploadFile = File(...),
    name: str = Form("TikTok account"),
    accountId: str | None = Form(None),
    uid: str = Depends(get_current_uid),
) -> dict:
    raw = await file.read()
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise HTTPException(400, "Could not read cookie file") from exc
    try:
        if accountId:
            return tiktok_accounts.import_cookies_text(uid, accountId, text)
        return tiktok_accounts.import_cookies_for_new_account(
            uid, name or "TikTok account", text
        )
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


class TikTokAccountUpdate(BaseModel):
    name: str | None = None
    captionSuffix: str | None = None


@router.put("/tiktok/accounts/{account_id}")
def tiktok_update_account(
    account_id: str, req: TikTokAccountUpdate, uid: str = Depends(get_current_uid)
) -> dict:
    if req.name is None and req.captionSuffix is None:
        raise HTTPException(400, "Provide name or captionSuffix")
    try:
        return tiktok_accounts.update_account(
            uid, account_id, name=req.name, caption_suffix=req.captionSuffix
        )
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


class TikTokAccountEnable(BaseModel):
    enabled: bool = True


@router.put("/tiktok/accounts/{account_id}/enabled")
def tiktok_enable_account(
    account_id: str, req: TikTokAccountEnable, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return tiktok_accounts.set_enabled(uid, account_id, req.enabled)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/tiktok/accounts/{account_id}/verify")
def tiktok_verify_account(account_id: str, uid: str = Depends(get_current_uid)) -> dict:
    try:
        return tiktok_accounts.verify_account(uid, account_id)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/tiktok/accounts/{account_id}")
def tiktok_delete_account(account_id: str, uid: str = Depends(get_current_uid)) -> dict:
    try:
        tiktok_accounts.delete_account(uid, account_id)
    except AccountError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.get("/tiktok/settings")
def tiktok_get_settings(uid: str = Depends(get_current_uid)) -> dict:
    return tiktok_accounts.load_settings(uid)


class TikTokSettingsUpdate(BaseModel):
    captionTemplate: str | None = None
    hashtags: str | None = None
    delaySeconds: int | None = None
    autoUpload: bool | None = None
    continueOnError: bool | None = None
    automaticCopyrightChecks: bool | None = None
    contentChecks: bool | None = None
    selectedAccountIds: list[str] | None = None
    roundRobinIndex: int | None = None
    uploadEngine: str | None = None
    httpMaxInFlightPerAccount: int | None = None
    httpGlobalMaxInFlight: int | None = None
    minSecondsBetweenPostsPerAccount: int | None = None


@router.put("/tiktok/settings")
def tiktok_put_settings(
    req: TikTokSettingsUpdate, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return tiktok_accounts.save_settings(uid, req.model_dump(exclude_unset=True))
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc


class TikTokUploadRequest(BaseModel):
    batchId: str | None = None
    paths: list[str] | None = None  # relative to the user's output dir
    captionTemplate: str | None = None
    hashtags: str | None = None
    delaySeconds: int | None = None
    continueOnError: bool | None = None
    automaticCopyrightChecks: bool | None = None
    contentChecks: bool | None = None
    accountId: str | None = None  # force this account; ignores Settings RR pool


@router.get("/tiktok/outputs")
def tiktok_list_outputs(limit: int = 200, uid: str = Depends(get_current_uid)) -> dict:
    """List rendered videos for manual upload selection."""
    try:
        items = catalog.list_output_items(uid, limit=limit)
    except Exception as exc:
        raise HTTPException(500, f"Could not list outputs: {exc}") from exc
    return {"items": items, "count": len(items)}


@router.post("/tiktok/upload")
def tiktok_start_upload(
    req: TikTokUploadRequest, uid: str = Depends(get_current_uid)
) -> dict:
    videos: list[dict] = []
    batch_id = req.batchId

    if req.batchId:
        videos = tiktok_upload.collect_videos_for_batch(uid, req.batchId)
        if not videos:
            raise HTTPException(400, "No finished videos in this batch to upload")
    elif req.paths:
        try:
            videos = tiktok_upload.resolve_output_paths(uid, req.paths)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        raise HTTPException(400, "Provide batchId or paths")

    try:
        job = tiktok_upload.start_upload(
            uid,
            videos,
            caption_template=req.captionTemplate,
            hashtags=req.hashtags,
            delay_seconds=req.delaySeconds,
            continue_on_error=req.continueOnError,
            automatic_copyright_checks=req.automaticCopyrightChecks,
            content_checks=req.contentChecks,
            source_batch_id=batch_id,
            account_id=req.accountId,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"uploadId": job.id, **tiktok_upload.job_state(job)}


class CarouselUploadRequest(BaseModel):
    pairs: list[dict] | None = None
    planId: str | None = None
    threadId: str | None = None
    captionTemplate: str | None = None
    hashtags: str | None = None
    delaySeconds: int | None = None
    continueOnError: bool | None = None
    accountId: str | None = None


@router.post("/tiktok/carousel-upload")
def tiktok_start_carousel_upload(
    req: CarouselUploadRequest, uid: str = Depends(get_current_uid)
) -> dict:
    from .. import chat_store

    pairs = [p for p in (req.pairs or []) if isinstance(p, dict)]
    thread_id = str(req.threadId or "").strip()
    account_id = req.accountId
    if not pairs and thread_id:
        plan = chat_store.get_thread_plan(uid, thread_id, req.planId)
        pairs = [p for p in (plan.get("renderPairs") or []) if isinstance(p, dict)]
        if not account_id:
            account_id = str(plan.get("tiktokAccountId") or "") or None
    carousels = [
        p
        for p in pairs
        if (p.get("kind") == "carousel" or p.get("slides"))
        and str(p.get("caption") or "").strip()
    ]
    if not carousels:
        raise HTTPException(400, "No carousel rows to upload")
    try:
        job = tiktok_upload.start_carousel_upload(
            uid,
            carousels,
            caption_template=req.captionTemplate,
            hashtags=req.hashtags,
            delay_seconds=req.delaySeconds,
            continue_on_error=req.continueOnError,
            account_id=account_id,
            thread_id=thread_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"uploadId": job.id, **tiktok_upload.job_state(job)}


@router.get("/tiktok/upload-latest")
def tiktok_upload_latest(uid: str = Depends(get_current_uid)) -> dict:
    from .. import job_runtime

    active = job_runtime.uploads.list_for(uid, statuses=("running", "queued"))
    job = active[0] if active else tiktok_upload.latest_job(uid)
    if not job:
        return {"id": None}
    return tiktok_upload.job_state(job)


@router.get("/tiktok/upload/{upload_id}")
def tiktok_upload_status(upload_id: str, uid: str = Depends(get_current_uid)) -> dict:
    job = tiktok_upload.get_job(upload_id, uid)
    if not job:
        raise HTTPException(404, "Upload job not found")
    return tiktok_upload.job_state(job)


@router.post("/tiktok/upload/{upload_id}/cancel")
def tiktok_upload_cancel(upload_id: str, uid: str = Depends(get_current_uid)) -> dict:
    if not tiktok_upload.cancel_job(upload_id, uid):
        raise HTTPException(404, "Upload job not found")
    return {"ok": True}


@router.post("/tiktok/upload/continue")
def tiktok_continue_upload(
    req: TikTokUploadRequest, uid: str = Depends(get_current_uid)
) -> dict:
    """Resume upload for a batch, skipping videos already posted to TikTok."""
    if not req.batchId and not req.paths:
        raise HTTPException(400, "Provide batchId or paths")
    try:
        job = tiktok_upload.continue_upload(
            uid,
            batch_id=req.batchId,
            paths=req.paths,
            caption_template=req.captionTemplate,
            hashtags=req.hashtags,
            delay_seconds=req.delaySeconds,
            continue_on_error=req.continueOnError,
            automatic_copyright_checks=req.automaticCopyrightChecks,
            content_checks=req.contentChecks,
            account_id=req.accountId,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"uploadId": job.id, **tiktok_upload.job_state(job)}


@router.get("/batch/{batch_id}/tiktok-status")
def batch_tiktok_status(batch_id: str, uid: str = Depends(get_current_uid)) -> dict:
    """Per-output TikTok upload status for a render batch (plan + resume UI)."""
    from .. import jobs
    from .. import userstore
    from .. import catalog

    batch = jobs.get_batch(batch_id, uid)
    rows = [
        r
        for r in userstore.list_docs(uid, "renders")
        if str(r.get("batch_id") or "") == batch_id
    ]
    if not rows and not batch:
        raise HTTPException(404, "Batch not found")
    by_rel = {
        str(r.get("output_rel") or "").replace("\\", "/").lstrip("/"): r for r in rows
    }
    items: list[dict] = []
    if batch:
        for bi in batch.items:
            if bi.status not in ("done", "skipped") or not bi.output:
                continue
            rel = catalog.output_rel_of(uid, bi.output)
            row = by_rel.get(rel) or catalog.get_by_output_rel(uid, rel) or {}
            st = str(row.get("tiktok_status") or "")
            on_tt = catalog.is_on_tiktok(row)
            items.append(
                {
                    "sourceId": str(row.get("source_id") or bi.source_id or ""),
                    "caption": str(row.get("hook") or bi.caption or ""),
                    "outputRel": rel,
                    "tiktokStatus": "done" if on_tt else st,
                    "onTiktok": on_tt,
                    "tiktokAccountName": str(row.get("tiktok_account_name") or ""),
                    "tiktokUploadedAt": str(row.get("tiktok_uploaded_at") or ""),
                    "tiktokError": str(row.get("tiktok_error") or ""),
                }
            )
    else:
        for row in rows:
            rel = str(row.get("output_rel") or "").replace("\\", "/").lstrip("/")
            on_tt = catalog.is_on_tiktok(row)
            items.append(
                {
                    "sourceId": str(row.get("source_id") or ""),
                    "caption": str(row.get("hook") or ""),
                    "outputRel": rel,
                    "tiktokStatus": "done" if on_tt else str(row.get("tiktok_status") or ""),
                    "onTiktok": on_tt,
                    "tiktokAccountName": str(row.get("tiktok_account_name") or ""),
                    "tiktokUploadedAt": str(row.get("tiktok_uploaded_at") or ""),
                    "tiktokError": str(row.get("tiktok_error") or ""),
                }
            )
    posted = sum(1 for it in items if it.get("onTiktok"))
    return {
        "batchId": batch_id,
        "total": len(items),
        "posted": posted,
        "remaining": max(0, len(items) - posted),
        "items": items,
    }


@router.post("/tiktok/test-upload")
def tiktok_test_upload(uid: str = Depends(get_current_uid)) -> dict:
    """Upload the fixed fixture video + caption with the current engine."""
    try:
        job = tiktok_upload.start_test_upload(uid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"uploadId": job.id, **tiktok_upload.job_state(job)}


class TikTokIncidentAck(BaseModel):
    incidentId: str


@router.get("/tiktok/incidents")
def tiktok_list_incidents(
    limit: int = 20, uid: str = Depends(get_current_uid)
) -> dict:
    from ..tiktok_engines import incidents as inc_mod

    items = inc_mod.list_incidents(uid, limit=limit)
    return {"items": items, "count": len(items)}


@router.post("/tiktok/incidents/ack")
def tiktok_ack_incident(
    req: TikTokIncidentAck, uid: str = Depends(get_current_uid)
) -> dict:
    from ..tiktok_engines import incidents as inc_mod

    ok = inc_mod.ack_incident(uid, (req.incidentId or "").strip())
    # Also clear on running/latest job so poll stops re-showing
    job = tiktok_upload.running_job_for_uid(uid) or tiktok_upload.latest_job(uid)
    if job and getattr(job, "incident_to_show", None):
        shown = job.incident_to_show or {}
        if shown.get("id") == req.incidentId:
            job.incident_to_show = None
    return {"ok": ok}


def _public_origin(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    host = (host or "").split(",")[0].strip()
    return f"{proto}://{host}"


def _ui_settings_url() -> str:
    origin = (os.environ.get("PUBLIC_UI_ORIGIN") or "http://localhost:8765").rstrip("/")
    return f"{origin}/#/settings"


def _fb_callback(request: Request) -> str:
    try:
        return facebook_ads.redirect_uri(_public_origin(request))
    except FacebookError:
        return _public_origin(request).rstrip("/") + facebook_ads.OAUTH_CALLBACK_PATH


# ---------------------------------------------------------------------------
# Facebook Ads — OAuth, catalog, paused ad create
# ---------------------------------------------------------------------------

@router.get("/facebook/status")
def facebook_status(uid: str = Depends(get_current_uid)) -> dict:
    return facebook_ads.public_status(uid)


@router.get("/facebook/oauth/url")
def facebook_oauth_url(
    request: Request,
    uid: str = Depends(get_current_uid),
    copy: bool = False,
) -> dict:
    try:
        callback = _fb_callback(request)
        url = facebook_ads.oauth_url(uid, callback=callback)
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"url": url, "redirectUri": callback, "copy": copy}


@public_router.get("/oauth/callback")
@public_router.get("/meta/oauth/callback")
@public_router.get("/facebook/oauth/callback")
def facebook_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    settings = _ui_settings_url()
    err = error_description or error
    if err and not code:
        body = (
            "<!doctype html><meta charset=utf-8><title>Facebook</title>"
            f"<p>Facebook login failed: {err}</p>"
            f"<p><a href='{settings}'>Back to Settings</a></p>"
        )
        return HTMLResponse(body, status_code=400)
    if not code or not state:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><p>Missing code. "
            f"<a href='{settings}'>Back to Settings</a></p>",
            status_code=400,
        )
    try:
        uid, state_cb = facebook_ads.parse_state(state)
        facebook_ads.complete_oauth(
            uid, code, callback=state_cb or _fb_callback(request)
        )
    except FacebookError as exc:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Facebook</title>"
            f"<p>{exc}</p><p><a href='{settings}'>Back to Settings</a></p>",
            status_code=400,
        )
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Facebook connected</title>"
        "<p>Facebook connected. Returning to Settings…</p>"
        f"<script>location.replace({settings!r});</script>"
    )


class FacebookOAuthCode(BaseModel):
    url: str | None = None
    code: str | None = None


@router.post("/facebook/oauth/code")
def facebook_oauth_code(
    req: FacebookOAuthCode, request: Request, uid: str = Depends(get_current_uid)
) -> dict:
    raw = (req.url or req.code or "").strip()
    try:
        return facebook_ads.complete_from_redirect(
            uid, raw, callback=_fb_callback(request)
        )
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/facebook")
def facebook_disconnect(uid: str = Depends(get_current_uid)) -> dict:
    return facebook_ads.disconnect(uid)


class FacebookSettingsUpdate(BaseModel):
    adAccountId: str | None = None
    pageId: str | None = None
    pixelId: str | None = None
    instagramUserId: str | None = None
    customEventType: str | None = None
    dailyBudget: float | None = None
    bidStrategy: str | None = None
    bidAmount: float | None = None
    country: str | None = None
    cta: str | None = None


@router.put("/facebook/settings")
def facebook_put_settings(
    req: FacebookSettingsUpdate, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return facebook_ads.save_settings(uid, req.model_dump(exclude_unset=True))
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/facebook/adaccounts")
def facebook_adaccounts(uid: str = Depends(get_current_uid)) -> dict:
    try:
        return {"items": facebook_ads.list_ad_accounts(uid)}
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/facebook/pages")
def facebook_pages(uid: str = Depends(get_current_uid)) -> dict:
    st = facebook_ads.public_status(uid)
    return {"items": st.get("pages") or []}


@router.get("/facebook/pixels")
def facebook_pixels(
    adAccountId: str | None = None, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return {"items": facebook_ads.list_pixels(uid, adAccountId)}
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/facebook/campaigns")
def facebook_campaigns(
    adAccountId: str | None = None, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return {"items": facebook_ads.list_campaigns(uid, adAccountId)}
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/facebook/adsets")
def facebook_adsets(
    campaignId: str | None = None,
    adAccountId: str | None = None,
    uid: str = Depends(get_current_uid),
) -> dict:
    try:
        return {
            "items": facebook_ads.list_adsets(
                uid, campaign_id=campaignId, ad_account_id=adAccountId
            )
        }
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc


class FacebookAdsRequest(BaseModel):
    paths: list[str] | None = None
    batchId: str | None = None
    mode: str = "new_campaign"
    landingUrl: str
    campaignId: str | None = None
    adsetId: str | None = None
    campaignName: str | None = None
    adsetName: str | None = None
    dailyBudget: float | None = None
    bidStrategy: str | None = None
    bidAmount: float | None = None
    pixelId: str | None = None
    customEventType: str | None = None
    pageId: str | None = None
    instagramUserId: str | None = None
    country: str | None = None
    cta: str | None = None
    continueOnError: bool = True


@router.post("/facebook/ads")
def facebook_start_ads(
    req: FacebookAdsRequest, uid: str = Depends(get_current_uid)
) -> dict:
    videos: list[dict] = []
    if req.batchId:
        collected = tiktok_upload.collect_videos_for_batch(uid, req.batchId)
        paths = []
        for v in collected:
            rel = str(v.get("relPath") or "")
            if rel:
                paths.append(rel)
                continue
            p = Path(v.get("videoPath") or "")
            try:
                rel = storage.rel_of(uid, p)
                if rel.startswith("output/"):
                    rel = rel[len("output/") :]
                paths.append(rel)
            except Exception:
                continue
        if not paths:
            raise HTTPException(400, "No finished videos in this batch")
        try:
            videos = facebook_jobs.videos_from_paths(uid, paths)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    elif req.paths:
        try:
            videos = facebook_jobs.videos_from_paths(uid, req.paths)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        raise HTTPException(400, "paths or batchId required")
    try:
        job = facebook_jobs.start_job(
            uid,
            videos,
            mode=req.mode,
            landing_url=req.landingUrl,
            campaign_id=req.campaignId,
            adset_id=req.adsetId,
            campaign_name=req.campaignName,
            adset_name=req.adsetName,
            daily_budget=req.dailyBudget,
            bid_strategy=req.bidStrategy,
            bid_amount=req.bidAmount,
            pixel_id=req.pixelId,
            custom_event_type=req.customEventType,
            page_id=req.pageId,
            instagram_user_id=req.instagramUserId,
            country=req.country,
            cta=req.cta,
            continue_on_error=req.continueOnError,
        )
    except FacebookError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"jobId": job.id, **facebook_jobs.job_state(job)}


@router.get("/facebook/ads/latest")
def facebook_ads_latest(uid: str = Depends(get_current_uid)) -> dict:
    job = facebook_jobs.latest_job(uid)
    if not job:
        return {"id": None}
    return facebook_jobs.job_state(job)


@router.get("/facebook/ads/{job_id}")
def facebook_ads_status(job_id: str, uid: str = Depends(get_current_uid)) -> dict:
    job = facebook_jobs.get_job(job_id, uid)
    if not job:
        raise HTTPException(404, "Facebook ads job not found")
    return facebook_jobs.job_state(job)


@router.post("/facebook/ads/{job_id}/cancel")
def facebook_ads_cancel(job_id: str, uid: str = Depends(get_current_uid)) -> dict:
    if not facebook_jobs.cancel_job(job_id, uid):
        raise HTTPException(404, "Facebook ads job not found")
    return {"ok": True}


