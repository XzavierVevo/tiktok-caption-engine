"""Internal MCP + spark + cleanup routes (Worker → container only).

Auth: ``X-Internal-Api-Key`` must match ``INTERNAL_API_KEY`` when set.
Correlation: ``X-Request-Id`` is logged with uid/thread/tool.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .. import chat_store
from .. import mcp_keys
from .. import spark_ingress as ingress
from .. import usage as usage_mod
from .. import userstore
from ..agent import tools as toolreg
from ..duoplus import extract_spark_code, looks_like_spark_code

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


def _require_internal(
    x_internal_api_key: str | None,
    x_request_id: str | None,
) -> str:
    expected = (os.environ.get("INTERNAL_API_KEY") or "").strip()
    firebase = bool((os.environ.get("FIREBASE_PROJECT_ID") or "").strip())
    if firebase and not expected:
        raise HTTPException(503, "INTERNAL_API_KEY is not configured")
    if expected and (x_internal_api_key or "").strip() != expected:
        raise HTTPException(403, "Invalid internal API key")
    return (x_request_id or "").strip() or "-"


class SessionBody(BaseModel):
    uid: str
    keyId: str = "default"
    threadId: str | None = None
    pendingConfirms: dict[str, Any] | None = None
    action: str = "get"  # get | set


@router.post("/mcp/session")
def mcp_session(
    body: SessionBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_api_key, x_request_id)
    uid = (body.uid or "").strip()
    kid = (body.keyId or "default").strip() or "default"
    if not uid:
        raise HTTPException(400, "uid required")
    doc_id = kid[:80]
    if body.action == "set":
        payload = {
            "threadId": (body.threadId or "").strip(),
            "pendingConfirms": body.pendingConfirms or {},
            "updatedAt": time.time(),
        }
        userstore.set_doc(uid, "mcp_sessions", doc_id, payload)
        return {"ok": True, **payload}
    data = userstore.get_doc(uid, "mcp_sessions", doc_id) or {}
    return {
        "ok": True,
        "threadId": str(data.get("threadId") or ""),
        "pendingConfirms": data.get("pendingConfirms") or {},
    }


class VerifyKeyBody(BaseModel):
    key: str


@router.post("/mcp/verify-key")
def verify_key(
    body: VerifyKeyBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    rid = _require_internal(x_internal_api_key, x_request_id)
    hit = mcp_keys.verify_key(body.key)
    if not hit:
        raise HTTPException(401, "Invalid MCP API key")
    _log.info("mcp verify-key ok uid=%s key=%s rid=%s", hit["uid"], hit["keyId"], rid)
    return hit


class ThreadsBody(BaseModel):
    uid: str
    action: str = "create"  # create | list | delete
    title: str = "MCP"
    threadId: str = ""
    limit: int = 20
    ephemeral: bool = False


@router.post("/mcp/threads")
def threads(
    body: ThreadsBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    rid = _require_internal(x_internal_api_key, x_request_id)
    uid = (body.uid or "").strip()
    if not uid:
        raise HTTPException(400, "uid required")
    if body.action == "list":
        items = chat_store.list_threads(uid)[: max(1, min(100, int(body.limit or 20)))]
        return {"ok": True, "threads": items}
    if body.action == "delete":
        tid = (body.threadId or "").strip()
        if not tid:
            raise HTTPException(400, "threadId required")
        try:
            chat_store.delete_thread(uid, tid)
        except KeyError:
            pass
        _log.info("mcp delete_thread uid=%s thread=%s rid=%s", uid, tid, rid)
        return {"ok": True, "threadId": tid, "deleted": True}
    title = (body.title or "MCP").strip() or "MCP"
    ephemeral = bool(body.ephemeral) or chat_store.is_ephemeral_title(title)
    thread = chat_store.create_thread(uid, title=title, ephemeral=ephemeral)
    tid = str(thread.get("id") or "")
    _log.info(
        "mcp create_thread uid=%s thread=%s ephemeral=%s rid=%s",
        uid,
        tid,
        ephemeral,
        rid,
    )
    return {"ok": True, "threadId": tid, "thread": thread}


class CallBody(BaseModel):
    uid: str
    threadId: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotencyKey: str | None = None
    mcpMode: str = "trusted"
    keyId: str = ""


def _idempotency_doc_id(key: str) -> str:
    """D1 document ids only allow [A-Za-z0-9._~-]. A colon in the key 500s after the tool already ran."""
    cleaned = re.sub(r"[^A-Za-z0-9._~-]", "_", (key or "").strip())[:120]
    return cleaned or "idem"


def _idempotency_get(uid: str, key: str) -> dict | None:
    if not key:
        return None
    return userstore.get_doc(uid, "mcp_idempotency", _idempotency_doc_id(key))


def _idempotency_put(uid: str, key: str, result: dict) -> None:
    if not key:
        return
    userstore.set_doc(
        uid,
        "mcp_idempotency",
        _idempotency_doc_id(key),
        {"result": result, "at": time.time()},
    )


@router.post("/mcp/call")
def mcp_call(
    body: CallBody,
    request: Request,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    from ..routers import chat as chat_routes

    rid = _require_internal(x_internal_api_key, x_request_id)
    uid = (body.uid or "").strip()
    name = (body.name or "").strip()
    if not uid or not name:
        raise HTTPException(400, "uid and name required")

    # Invalidate userstore cache so Worker-written docs (if any) are visible.
    userstore.invalidate_uid(uid)

    idem = (body.idempotencyKey or "").strip()
    if idem:
        prev = _idempotency_get(uid, f"{name}:{idem}")
        if isinstance(prev, dict) and isinstance(prev.get("result"), dict):
            _log.info("mcp call idempotent hit name=%s uid=%s rid=%s", name, uid, rid)
            return prev["result"]

    thread_id = (body.threadId or "").strip()
    if not thread_id and name in (
        "fetch_spark",
        "start_carousel_upload",
        "list_plans",
        "set_render_plan",
        "set_carousel_plan",
    ):
        thread = chat_store.create_thread(uid, title="MCP")
        thread_id = str(thread.get("id") or "")

    if name == "get_spark_job":
        from .. import job_runtime
        from .. import spark_jobs

        job_id = str((body.arguments or {}).get("jobId") or "").strip()
        job = job_runtime.sparks.get(job_id, uid) if job_id else None
        if job is None:
            # Try durable snapshot
            snap = userstore.get_doc(uid, "spark_jobs", job_id) if job_id else None
            if not snap:
                raise HTTPException(404, "Spark job not found")
            return {"ok": True, **snap}
        return {"ok": True, **spark_jobs.job_state(job)}

    ctx = chat_routes.tool_context(thread_id)
    args = dict(body.arguments or {})
    # Never block on wait — MCP / Worker path is start+poll only.
    if name in ("fetch_spark", "start_carousel_upload"):
        args["wait"] = False

    _log.info(
        "mcp call name=%s uid=%s thread=%s key=%s rid=%s",
        name,
        uid,
        thread_id,
        body.keyId,
        rid,
    )
    # Warm-up must see the edge heartbeat, not the container KV row. A stale
    # "online" flag created a Business Center that nobody claimed, and the
    # failed response was retried into a second and third profile.
    from ..provision_jobs import apply_edge_worker_headers

    apply_edge_worker_headers(request.headers)
    result = toolreg.execute(uid, name, args, ctx)
    if not isinstance(result, dict):
        result = {"ok": True, "result": result}
    result.setdefault("threadId", thread_id)
    result.setdefault("requestId", rid)

    if idem and result.get("ok") is not False:
        try:
            _idempotency_put(uid, f"{name}:{idem}", result)
        except Exception:
            _log.exception("mcp idempotency save failed name=%s", name)

    return result


class QuotaBody(BaseModel):
    uid: str
    tool: str
    keyId: str = ""
    sparkDayCap: int = 20


@router.post("/mcp/quota-check")
def quota_check(
    body: QuotaBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_api_key, x_request_id)
    uid = (body.uid or "").strip()
    tool = (body.tool or "").strip()
    if not uid:
        raise HTTPException(400, "uid required")

    day = time.strftime("%Y-%m-%d", time.gmtime())
    doc_id = f"mcp_{day}"
    data = userstore.get_doc(uid, "quotas", doc_id) or {"day": day, "counts": {}}
    counts = data.setdefault("counts", {})
    if not isinstance(counts, dict):
        counts = {}
        data["counts"] = counts

    if tool == "fetch_spark":
        n = int(counts.get("spark") or 0)
        cap = max(0, int(body.sparkDayCap or 20))
        if n >= cap:
            raise HTTPException(
                429,
                f"MCP spark daily cap reached ({cap}). Try again tomorrow.",
            )
        counts["spark"] = n + 1
    else:
        counts[tool] = int(counts.get(tool) or 0) + 1

    data["counts"] = counts
    data["updatedAt"] = time.time()
    userstore.set_doc(uid, "quotas", doc_id, data)
    try:
        usage_mod.record_pinterest(uid, thread_id="", credits=0)  # touch usage store
    except Exception:
        pass
    return {"ok": True, "counts": counts}


class SparkIngressBody(BaseModel):
    taskName: str = ""
    body: Any = None
    token: str = ""


@router.post("/spark/ingress")
def spark_ingress(
    body: SparkIngressBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    rid = _require_internal(x_internal_api_key, x_request_id)
    if not ingress.verify_callback_token(body.token):
        # Worker already checked env secret; allow empty when secret unset.
        if ingress.secret_required():
            raise HTTPException(403, "Invalid spark callback token")

    raw = body.body
    post_url = ""
    spark = ""
    if isinstance(raw, str):
        spark = extract_spark_code(raw, post_url=post_url)
    elif isinstance(raw, dict):
        post_url = str(raw.get("postUrl") or raw.get("post_url") or "").strip()
        for key in (
            "sparkCode",
            "spark_code",
            "spark_output",
            "sparkOutput",
            "code",
            "content",
            "value",
        ):
            spark = extract_spark_code(str(raw.get(key) or ""), post_url=post_url)
            if spark:
                break
        if not spark:
            import json

            spark = extract_spark_code(json.dumps(raw), post_url=post_url)

    if not spark or not looks_like_spark_code(spark):
        raise HTTPException(400, "No spark code in body")

    ingress.save_spark(
        spark_code=spark,
        task_name=body.taskName,
        post_url=post_url,
        source="worker_callback",
    )
    # Also durable Firestore doc for multi-instance / sleep survival.
    key = (body.taskName or post_url or spark)[:120]
    userstore.set_doc(
        "_system",
        "spark_callbacks",
        ingress._key(body.taskName, post_url),  # noqa: SLF001
        {
            "sparkCode": spark,
            "taskName": body.taskName,
            "postUrl": post_url,
            "receivedAt": time.time(),
            "requestId": rid,
            "key": key,
        },
    )
    return {"ok": True, "sparkCode": spark, "taskName": body.taskName, "postUrl": post_url}


class MintKeyBody(BaseModel):
    uid: str
    mode: str = "trusted"
    label: str = "OAuth exchange"


@router.post("/mcp/mint-key")
def mint_key(
    body: MintKeyBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    """Worker OAuth exchange → create indexed MCP key for uid."""
    _require_internal(x_internal_api_key, x_request_id)
    uid = (body.uid or "").strip()
    if not uid:
        raise HTTPException(400, "uid required")
    created = mcp_keys.create_key_indexed(
        uid,
        label=body.label or "OAuth exchange",
        mode=body.mode or "trusted",
    )
    return {"ok": True, **created}


class TikTokOAuthBody(BaseModel):
    state: str = ""
    auth_code: str = ""
    uid: str = ""


@router.post("/tiktok/oauth/exchange")
def exchange_tiktok_oauth(
    body: TikTokOAuthBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_api_key, x_request_id)
    from ..tiktok_marketing.oauth import exchange_auth_code
    from ..tiktok_marketing.store import consume_oauth_state, save_connection, mirror_connections

    raw_state = (body.state or "").strip()
    uid = (body.uid or "").strip()
    if not uid and "." in raw_state:
        uid = raw_state.split(".", 1)[0]
    uid = uid or "dev"
    profile_id = consume_oauth_state(uid, raw_state)
    if not profile_id:
        raise HTTPException(400, "OAuth state was already used or does not match")
    token = exchange_auth_code(body.auth_code)
    if not token.get("accessToken"):
        raise HTTPException(400, "TikTok did not return an access token")
    saved = save_connection(
        uid,
        profile_id,
        {
            "accessToken": token["accessToken"],
            "advertiserIds": token.get("advertiserIds") or [],
            "oauthStatus": "connected",
            "scope": token.get("scope") or [],
        },
    )
    mirror_connections(uid)
    return {"ok": True, "connection": saved}


class AffiliateGoBody(BaseModel):
    s2: str = ""
    ttclid: str = ""


@router.post("/affiliate/go")
def affiliate_go(
    body: AffiliateGoBody,
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_api_key, x_request_id)
    from ..affiliate.service import resolve_go
    from ..provision.owner import owner_uid

    return resolve_go(owner_uid(), body.s2, body.ttclid)


@router.post("/tiktok/spark-refresh")
def tiktok_spark_refresh(
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    """Idle job: refresh stored Sparks, then save the snapshot the page reads."""
    _require_internal(x_internal_api_key, x_request_id)
    from ..affiliate.service import refresh_idle_sparks
    from ..provision.owner import owner_uid

    snapshot = refresh_idle_sparks(owner_uid())
    return {"ok": True, "sparks": len(snapshot.get("sparks") or [])}


@router.post("/tiktok/sync")
def sync_tiktok(
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_api_key, x_request_id)
    from ..provision.owner import owner_uid
    from ..tiktok_marketing.reports import sync_all

    return {"ok": True, **sync_all(owner_uid())}


@router.post("/cleanup")
def cleanup(
    x_internal_api_key: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_api_key, x_request_id)
    from .. import cleanup as cleanup_mod

    cleanup_mod.run_once()
    return {"ok": True}
