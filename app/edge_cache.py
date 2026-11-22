"""Publish lightweight JSON snapshots to R2 for the Cloudflare Worker edge.

Idle UI / provision-worker polls read these from the Worker without waking the
Python container (Container Memory is billed while the instance is awake).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import storage

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_last_activity_sig: dict[str, str] = {}


def activity_key(uid: str) -> str:
    return f"edge-cache/{uid}/activity.json"


def inventory_key(uid: str) -> str:
    return f"edge-cache/{uid}/inventory.json"


def provision_jobs_key(uid: str) -> str:
    return f"edge-cache/{uid}/provision-jobs.json"


def jobs_active_key(uid: str) -> str:
    return f"edge-cache/{uid}/jobs-active.json"


def put_edge_json(key: str, data: dict[str, Any]) -> bool:
    """Write a bucket-root JSON object (same R2 bucket the Worker binds as DATA)."""
    if not storage.ENABLED:
        return False
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        storage._s3().put_object(
            Bucket=storage.R2_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        return True
    except Exception as exc:
        _log.warning("edge_cache put failed key=%s: %s", key, exc)
        return False


def _pending_step_count(uid: str) -> int:
    try:
        from .provision import inventory as inv

        store = inv._load_steps(uid)  # noqa: SLF001 — shared KV shape
    except Exception:
        return 0
    if not isinstance(store, dict):
        return 0
    by_id = store.get("byId") or {}
    n = 0
    for row in by_id.values():
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") in ("pending", "claimed"):
            n += 1
    return n


def _runtime_active_count(uid: str) -> int:
    try:
        from . import job_runtime
    except Exception:
        return 0
    n = 0
    for reg in (
        job_runtime.batches,
        job_runtime.uploads,
        job_runtime.facebook,
        job_runtime.provisions,
    ):
        try:
            n += len(reg.list_for(uid, statuses=("running", "queued")))
        except Exception:
            pass
    return n


def build_activity(uid: str) -> dict[str, Any]:
    pending = _pending_step_count(uid)
    runtime = _runtime_active_count(uid)
    has_work = pending > 0 or runtime > 0
    return {
        "uid": uid,
        "hasWork": has_work,
        "pendingSteps": pending,
        "runtimeJobs": runtime,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def publish_activity(uid: str, *, force: bool = False) -> dict[str, Any] | None:
    """Upsert activity.json. Skips identical signatures unless ``force``."""
    uid = (uid or "").strip()
    if not uid:
        return None
    payload = build_activity(uid)
    sig = f"{payload['hasWork']}:{payload['pendingSteps']}:{payload['runtimeJobs']}"
    with _lock:
        if not force and _last_activity_sig.get(uid) == sig:
            return payload
        _last_activity_sig[uid] = sig
    put_edge_json(activity_key(uid), payload)
    return payload


def publish_inventory(uid: str, data: dict[str, Any]) -> None:
    uid = (uid or "").strip()
    if not uid or not isinstance(data, dict):
        return
    out = dict(data)
    out["edgePublishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    put_edge_json(inventory_key(uid), out)
    publish_activity(uid)


def publish_provision_jobs(uid: str, payload: dict[str, Any]) -> None:
    uid = (uid or "").strip()
    if not uid or not isinstance(payload, dict):
        return
    out = dict(payload)
    out["edgePublishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    put_edge_json(provision_jobs_key(uid), out)
    publish_activity(uid)


def publish_jobs_active(uid: str, payload: dict[str, Any]) -> None:
    uid = (uid or "").strip()
    if not uid or not isinstance(payload, dict):
        return
    out = dict(payload)
    out["edgePublishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    put_edge_json(jobs_active_key(uid), out)
    publish_activity(uid)


def publish_activity_async(uid: str) -> None:
    """Fire-and-forget activity refresh (step enqueue / complete paths)."""
    uid = (uid or "").strip()
    if not uid:
        return

    def _run() -> None:
        try:
            publish_activity(uid, force=True)
        except Exception:
            _log.debug("publish_activity_async failed", exc_info=True)

    threading.Thread(target=_run, name="edge-activity", daemon=True).start()
