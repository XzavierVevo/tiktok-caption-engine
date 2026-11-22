"""Workspace-scoped named render plans (shared by Create + Chat)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from . import userstore
from .workspace import _clean_render_pairs

KV_KEY = "workspace_plans"


def _empty() -> dict[str, Any]:
    return {"activePlanId": "", "plans": []}


def _normalize_plan(raw: dict | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    name = str(raw.get("name") or raw.get("title") or "").strip() or "Untitled plan"
    pairs = _clean_render_pairs(raw.get("renderPairs") or raw.get("pairs") or [])
    selected = raw.get("selected")
    if not isinstance(selected, list):
        selected = []
    selected_clean = []
    for item in selected:
        if isinstance(item, dict):
            sid = str(item.get("sourceId") or "").strip()
            cap = str(item.get("caption") or "").strip()
            if sid and cap:
                selected_clean.append({"sourceId": sid, "caption": cap})
        elif isinstance(item, str) and item.strip():
            selected_clean.append(item.strip())
    now = float(raw.get("updatedAt") or time.time())
    created = float(raw.get("createdAt") or now)
    return {
        "id": pid,
        "name": name,
        "title": name,
        "renderPairs": pairs,
        "selected": selected_clean,
        "createdAt": created,
        "updatedAt": now,
        "pairCount": len(pairs),
        # Chat uses the same plan object. Keep its execution metadata in the
        # workspace store rather than creating a second chat-only copy.
        **{
            key: raw[key]
            for key in ("captions", "captionMeta", "mode", "batchId", "tiktokAccountId", "tiktokAccountName")
            if key in raw
        },
    }


def _normalize(data: dict | None) -> dict[str, Any]:
    base = _empty()
    if not isinstance(data, dict):
        return base
    plans_out: list[dict] = []
    for item in data.get("plans") or []:
        p = _normalize_plan(item if isinstance(item, dict) else None)
        if p:
            plans_out.append(p)
    active = str(data.get("activePlanId") or "").strip()
    if active and not any(p["id"] == active for p in plans_out):
        active = plans_out[0]["id"] if plans_out else ""
    if not active and plans_out:
        active = plans_out[0]["id"]
    return {"activePlanId": active, "plans": plans_out}


def load_store(uid: str) -> dict[str, Any]:
    # Plan navigation is latency-sensitive.  A healthy local snapshot should
    # not wait through three 20-second Firestore deadlines before it can open.
    return _normalize(userstore.get_kv_local_first(uid, KV_KEY))


def save_store(uid: str, data: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(data)
    userstore.set_kv(uid, KV_KEY, normalized)
    return normalized


def list_plans(uid: str) -> dict[str, Any]:
    store = load_store(uid)
    summaries = [
        {
            "id": p["id"],
            "name": p["name"],
            "updatedAt": p["updatedAt"],
            "pairCount": p["pairCount"],
            "active": p["id"] == store.get("activePlanId"),
        }
        for p in store["plans"]
    ]
    return {"activePlanId": store.get("activePlanId") or "", "plans": summaries}


def _enrich_plan(uid: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Attach catalog render / TikTok fields onto plan pairs for the preview UI."""
    try:
        from .chat_store import enrich_render_pairs_from_catalog

        pairs = enrich_render_pairs_from_catalog(uid, plan.get("renderPairs") or [])
    except Exception:
        pairs = plan.get("renderPairs") or []
    return {**plan, "renderPairs": pairs}


def get_plan(uid: str, plan_id: str) -> dict[str, Any]:
    store = load_store(uid)
    pid = str(plan_id or "").strip()
    for p in store["plans"]:
        if p["id"] == pid:
            enriched = _enrich_plan(uid, p)
            return {**enriched, "active": p["id"] == store.get("activePlanId")}
    raise KeyError("Plan not found")


def create_plan(
    uid: str,
    *,
    name: str = "",
    render_pairs: list | None = None,
    selected: list | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    store = load_store(uid)
    now = time.time()
    plan = _normalize_plan(
        {
            "id": uuid.uuid4().hex[:12],
            "name": name or f"Plan {len(store['plans']) + 1}",
            "renderPairs": render_pairs or [],
            "selected": selected or [],
            "createdAt": now,
            "updatedAt": now,
        }
    )
    assert plan is not None
    store["plans"].insert(0, plan)
    if activate:
        store["activePlanId"] = plan["id"]
    save_store(uid, store)
    if activate:
        _mirror_to_workspace(uid, plan)
    enriched = _enrich_plan(uid, plan)
    return {**enriched, "active": store["activePlanId"] == plan["id"]}


def update_plan(
    uid: str,
    plan_id: str,
    patch: dict[str, Any] | None,
) -> dict[str, Any]:
    store = load_store(uid)
    pid = str(plan_id or "").strip()
    target = next((p for p in store["plans"] if p["id"] == pid), None)
    if not target:
        raise KeyError("Plan not found")
    patch = dict(patch or {})
    if "name" in patch or "title" in patch:
        target["name"] = str(patch.get("name") or patch.get("title") or target["name"]).strip() or target["name"]
        target["title"] = target["name"]
    if "renderPairs" in patch or "pairs" in patch:
        target["renderPairs"] = _clean_render_pairs(patch.get("renderPairs") or patch.get("pairs"))
        target["pairCount"] = len(target["renderPairs"])
    if "selected" in patch:
        target["selected"] = patch.get("selected") if isinstance(patch.get("selected"), list) else []
    for key in ("captions", "captionMeta", "mode", "batchId", "tiktokAccountId", "tiktokAccountName"):
        if key in patch:
            target[key] = patch[key]
    target["updatedAt"] = time.time()
    save_store(uid, store)
    return {**target, "active": store.get("activePlanId") == target["id"]}


def _mirror_to_workspace(uid: str, plan: dict[str, Any]) -> None:
    from . import workspace as ws

    ws.save_workspace(
        uid,
        {
            "renderPairs": plan.get("renderPairs") or [],
            "mode": "custom" if plan.get("renderPairs") else ws.load_workspace(uid).get("mode"),
            "activePlanId": plan["id"],
        },
    )


def activate_plan(uid: str, plan_id: str) -> dict[str, Any]:
    store = load_store(uid)
    pid = str(plan_id or "").strip()
    target = next((p for p in store["plans"] if p["id"] == pid), None)
    if not target:
        raise KeyError("Plan not found")
    store["activePlanId"] = pid
    save_store(uid, store)
    _mirror_to_workspace(uid, target)
    enriched = _enrich_plan(uid, target)
    return {**enriched, "active": True}


def delete_plan(uid: str, plan_id: str) -> dict[str, Any]:
    store = load_store(uid)
    pid = str(plan_id or "").strip()
    before = len(store["plans"])
    store["plans"] = [p for p in store["plans"] if p["id"] != pid]
    if len(store["plans"]) == before:
        raise KeyError("Plan not found")
    if store.get("activePlanId") == pid:
        store["activePlanId"] = store["plans"][0]["id"] if store["plans"] else ""
    save_store(uid, store)
    return list_plans(uid)


def get_active_plan(uid: str) -> dict[str, Any] | None:
    store = load_store(uid)
    aid = str(store.get("activePlanId") or "").strip()
    if not aid:
        return None
    try:
        return get_plan(uid, aid)
    except KeyError:
        return None
