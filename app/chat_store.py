"""Chat thread persistence (ChatGPT-style multi-conversation)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from . import userstore
from . import workspace as ws
from .agent.perplexity import repair_mojibake


class StorageUnavailable(RuntimeError):
    """Raised when cloud storage (D1/Firestore) is unreachable.

    This is distinct from KeyError("Thread not found") which means the thread
    genuinely doesn't exist. StorageUnavailable means we couldn't check.
    """
    pass


THREAD_CAP = 50
MESSAGE_CAP = 80
TITLE_MAX = 80
PLAN_CAP = 30
PLAN_TITLE_MAX = 80

Mutator = Callable[[dict[str, Any]], dict[str, Any] | None]

VALID_PLAN_MODES = ("matrix", "pair", "custom")

_index_cache: dict[str, dict[str, Any]] = {}
_index_dirty: set[str] = set()


def _empty_index() -> dict:
    return {"threads": []}


def is_ephemeral_title(title: str | None) -> bool:
    """Titles used by ship-gate / smoke scripts — never show in Recents."""
    t = str(title or "").strip().lower()
    return t.startswith("e2e-") or t.startswith("e2e ")


def thread_is_ephemeral(thread: dict | None) -> bool:
    if not isinstance(thread, dict):
        return False
    if thread.get("ephemeral") is True:
        return True
    return is_ephemeral_title(thread.get("title"))


def empty_plan() -> dict:
    return {
        "renderPairs": [],
        "captions": "",
        "captionMeta": [],
        "mode": "custom",
        "batchId": "",
        "tiktokAccountId": "",
        "tiktokAccountName": "",
    }


def normalize_plan(data: dict | None) -> dict:
    base = empty_plan()
    if not isinstance(data, dict):
        return base
    out = {**base, **{k: data[k] for k in base if k in data}}
    out["renderPairs"] = ws._clean_render_pairs(data.get("renderPairs") or [])
    out["captionMeta"] = ws._clean_caption_meta(data.get("captionMeta") or [])
    if out.get("mode") not in VALID_PLAN_MODES:
        out["mode"] = "custom"
    if not isinstance(out.get("captions"), str):
        out["captions"] = ""
    out["tiktokAccountId"] = str(out.get("tiktokAccountId") or "").strip()
    out["tiktokAccountName"] = str(out.get("tiktokAccountName") or "").strip()
    return out


def _default_plan_title(index: int = 1) -> str:
    """Name new plans with the local date and time (index kept for call-site compat)."""
    _ = index
    dt = datetime.now().astimezone()
    return f"{dt.strftime('%b %d, %Y')} · {dt.strftime('%I:%M %p').lstrip('0')}"


def normalize_plan_doc(data: dict | None, *, index: int = 1) -> dict:
    body = normalize_plan(data)
    now = time.time()
    raw = data if isinstance(data, dict) else {}
    pid = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    title = str(raw.get("title") or "").strip()[:PLAN_TITLE_MAX] or _default_plan_title(
        index
    )
    chat_id = str(raw.get("chatId") or "").strip()
    chat_title = str(raw.get("chatTitle") or "").strip()[:TITLE_MAX]
    return {
        "id": pid,
        "title": title,
        "createdAt": float(raw.get("createdAt") or now),
        "updatedAt": float(raw.get("updatedAt") or now),
        "chatId": chat_id,
        "chatTitle": chat_title,
        **body,
    }


def _plan_summary(doc: dict) -> dict:
    pairs = doc.get("renderPairs") or []
    videos = {str(p.get("sourceId") or "") for p in pairs if (p or {}).get("sourceId")}
    captions = {str(p.get("caption") or "").strip() for p in pairs if (p or {}).get("caption")}
    videos.discard("")
    captions.discard("")
    kinds = {(p or {}).get("kind") for p in pairs if isinstance(p, dict)}
    kind = "carousel" if kinds == {"carousel"} else ("mixed" if "carousel" in kinds else "video")
    return {
        "id": doc.get("id") or "",
        "title": doc.get("title") or "Plan",
        "pairCount": len(pairs),
        "videoCount": len(videos),
        "captionCount": len(captions),
        "kind": kind,
        "createdAt": doc.get("createdAt") or 0,
        "updatedAt": doc.get("updatedAt") or 0,
        "mode": doc.get("mode") or "custom",
        "tiktokAccountId": str(doc.get("tiktokAccountId") or "").strip(),
        "tiktokAccountName": str(doc.get("tiktokAccountName") or "").strip(),
        # Internal tracking — UI does not display these.
        "chatId": str(doc.get("chatId") or ""),
        "chatTitle": str(doc.get("chatTitle") or ""),
    }


def _stamp_plan_chat(plan: dict, thread: dict) -> dict:
    """Attach owning chat id/title for cross-chat tracking."""
    if not isinstance(plan, dict):
        return plan
    plan["chatId"] = str(thread.get("id") or plan.get("chatId") or "").strip()
    plan["chatTitle"] = str(thread.get("title") or plan.get("chatTitle") or "").strip()[
        :TITLE_MAX
    ]
    return plan


def _active_plan_chat_id(thread: dict) -> str:
    """Home chat for the viewing thread's active plan (may differ from thread.id)."""
    acid = str(thread.get("activePlanChatId") or "").strip()
    tid = str(thread.get("id") or "").strip()
    if acid and acid != tid:
        return acid
    return tid


def _active_plan_doc(thread: dict) -> dict | None:
    plans = thread.get("plans") or []
    if not isinstance(plans, list) or not plans:
        return None
    aid = str(thread.get("activePlanId") or "").strip()
    for p in plans:
        if isinstance(p, dict) and p.get("id") == aid:
            return p
    first = plans[0]
    return first if isinstance(first, dict) else None


def _ensure_plans_on_thread(thread: dict) -> dict:
    """Normalize plans[] + activePlanId; migrate legacy thread.plan if needed."""
    plans: list[dict] = []
    raw_plans = thread.get("plans")
    if isinstance(raw_plans, list):
        for i, row in enumerate(raw_plans, 1):
            if isinstance(row, dict):
                plans.append(normalize_plan_doc(row, index=i))
    if not plans:
        legacy = normalize_plan(thread.get("plan"))
        if legacy.get("renderPairs"):
            plans = [normalize_plan_doc({**legacy, "title": _default_plan_title(1)}, index=1)]
    for p in plans:
        _stamp_plan_chat(p, thread)
    active = str(thread.get("activePlanId") or "").strip()
    acid = str(thread.get("activePlanChatId") or "").strip()
    tid = str(thread.get("id") or "").strip()
    cross = bool(acid and acid != tid and active)
    if not cross and not any(p.get("id") == active for p in plans):
        active = str(plans[0]["id"]) if plans else ""
        acid = ""
    if not cross:
        acid = ""
    thread["plans"] = plans[:PLAN_CAP]
    thread["activePlanId"] = active
    thread["activePlanChatId"] = acid
    if not cross:
        active_doc = _active_plan_doc(thread)
        thread["plan"] = normalize_plan(active_doc) if active_doc else empty_plan()
    elif not isinstance(thread.get("plan"), dict):
        thread["plan"] = empty_plan()
    return thread


def _iter_chat_threads(uid: str):
    """Yield every chat thread doc for this user."""
    docs = userstore.list_docs(uid, "chats")
    for thread in docs:
        if not isinstance(thread, dict):
            continue
        if not str(thread.get("id") or "").strip():
            continue
        _repair_mojibake_inplace(thread)
        yield thread


def _find_plan_location(
    uid: str, plan_id: str
) -> tuple[str, dict, dict] | None:
    """Return (home_thread_id, thread, plan_doc) for a plan id, or None."""
    wanted = str(plan_id or "").strip()
    if not wanted:
        return None
    for thread in _iter_chat_threads(uid):
        _ensure_plans_on_thread(thread)
        for p in thread.get("plans") or []:
            if isinstance(p, dict) and p.get("id") == wanted:
                return str(thread.get("id") or ""), thread, p
    return None


_PLAN_TOOL_NAMES = frozenset({"preview_plan", "set_render_plan", "set_carousel_plan"})


def _thread_had_plan_tools(thread: dict) -> bool:
    for msg in thread.get("messages") or []:
        if msg.get("role") == "tool" and (msg.get("name") or "") in _PLAN_TOOL_NAMES:
            return True
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if (tc.get("name") or "") in _PLAN_TOOL_NAMES:
                return True
    return False


def _plan_from_tool_payload(data: dict) -> dict | None:
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    pairs = data.get("pairs") or data.get("renderPairs") or []
    if not pairs:
        for group in data.get("videos") or []:
            if not isinstance(group, dict):
                continue
            sid = str(group.get("sourceId") or "").strip()
            name = group.get("sourceName") or sid
            caps = group.get("captions") or []
            if isinstance(caps, str):
                caps = [caps]
            for cap in caps:
                text = str(cap or "").strip()
                if sid and text:
                    pairs.append({"sourceId": sid, "sourceName": name, "caption": text})
    if not pairs:
        return None
    caps = ws.unique_captions_from_pairs(pairs)
    meta = data.get("captionMeta")
    return normalize_plan(
        {
            "renderPairs": pairs,
            "captions": caps,
            "captionMeta": meta if isinstance(meta, list) else [],
            "mode": data.get("mode") or "custom",
        }
    )


def recover_plan_from_messages(thread: dict) -> dict | None:
    for msg in reversed(thread.get("messages") or []):
        if msg.get("role") != "tool":
            continue
        if (msg.get("name") or "") not in _PLAN_TOOL_NAMES:
            continue
        raw = msg.get("content") or ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        plan = _plan_from_tool_payload(data)
        if plan and plan.get("renderPairs"):
            return plan
    return None


def _workspace_plan_snapshot(uid: str) -> dict:
    w = ws.load_workspace(uid)
    pairs = w.get("renderPairs") or []
    if not pairs:
        return empty_plan()
    return normalize_plan(
        {
            "renderPairs": pairs,
            "captions": w.get("captions") or ws.unique_captions_from_pairs(pairs),
            "captionMeta": w.get("captionMeta") or [],
            "mode": w.get("mode") or "custom",
        }
    )


def _record_recovered_plan_hooks(uid: str, thread_id: str, plan: dict) -> None:
    return None


def _ensure_plan_history(uid: str, thread_id: str, thread: dict, plan: dict) -> None:
    if not (plan.get("renderPairs") or []):
        return
    if thread.get("planHistoryRecorded"):
        return

    def _apply(t: dict) -> dict:
        t["planHistoryRecorded"] = True
        return t

    _record_recovered_plan_hooks(uid, thread_id, plan)
    mutate_thread(uid, thread_id, _apply, flush_index_now=False)


def _resolve_thread_plan(uid: str, thread_id: str, thread: dict) -> dict:
    had_plans = isinstance(thread.get("plans"), list) and bool(thread.get("plans"))
    _ensure_plans_on_thread(thread)
    active = _active_plan_doc(thread)
    plan = normalize_plan(active)
    if plan.get("renderPairs"):
        _ensure_plan_history(uid, thread_id, thread, plan)
        if not had_plans:

            def _sync(t: dict) -> dict:
                t["plans"] = thread.get("plans") or []
                t["activePlanId"] = thread.get("activePlanId") or ""
                t["plan"] = plan
                return t

            mutate_thread(uid, thread_id, _sync, flush_index_now=False)
        return plan
    if thread.get("plans"):
        return plan
    recovered = recover_plan_from_messages(thread)
    if recovered and recovered.get("renderPairs"):
        save_thread_plan(
            uid, thread_id, recovered, resolve=False, new_plan=True, title=_default_plan_title()
        )

        def _apply(t: dict) -> dict:
            t["planHistoryRecorded"] = True
            return t

        _record_recovered_plan_hooks(uid, thread_id, recovered)
        mutate_thread(uid, thread_id, _apply, flush_index_now=False)
        return recovered
    if _thread_had_plan_tools(thread):
        ws_plan = _workspace_plan_snapshot(uid)
        if ws_plan.get("renderPairs"):
            save_thread_plan(
                uid, thread_id, ws_plan, resolve=False, new_plan=True, title=_default_plan_title()
            )

            def _apply(t: dict) -> dict:
                t["planHistoryRecorded"] = True
                return t

            _record_recovered_plan_hooks(uid, thread_id, ws_plan)
            mutate_thread(uid, thread_id, _apply, flush_index_now=False)
            return ws_plan
    return plan


def get_thread_plan(uid: str, thread_id: str, plan_id: str | None = None) -> dict:
    # Plans are workspace-scoped. Keep this function as the compatibility
    # boundary for agents, uploads, and older callers that still pass a chat id.
    from . import plans as plan_store

    # Use raise_on_storage_error=True so we can distinguish "thread doesn't
    # exist" from "cloud storage is down". Callers can catch StorageUnavailable
    # and return a 503 instead of a misleading 404.
    thread = _load_thread(uid, thread_id, raise_on_storage_error=True)
    if not thread:
        raise KeyError("Thread not found")
    wanted = str(plan_id or thread.get("activePlanId") or "").strip()
    if wanted:
        try:
            return plan_store.get_plan(uid, wanted)
        except KeyError:
            # A deleted plan must not make this chat silently adopt another
            # chat's active plan.
            if not plan_id:
                mutate_thread(
                    uid,
                    thread_id,
                    lambda t: {**t, "activePlanId": "", "activePlanChatId": ""},
                    flush_index_now=False,
                )
            else:
                raise
    return normalize_plan_doc({**empty_plan(), "title": _default_plan_title()}, index=1)


def list_thread_plans(uid: str, thread_id: str) -> dict:
    """List every plan across all chats; active plan is for this viewing thread."""
    from . import plans as plan_store

    thread = _load_thread(uid, thread_id)
    if not thread:
        raise KeyError("Thread not found")
    listed = plan_store.list_plans(uid)
    active_id = str(thread.get("activePlanId") or "").strip()
    active_plan = get_thread_plan(uid, thread_id)
    pairs = active_plan.get("renderPairs") or []
    posted = sum(1 for p in pairs if p.get("tiktokStatus") == "done")
    failed = sum(1 for p in pairs if p.get("tiktokStatus") == "failed")
    return {
        **listed,
        "activePlanId": active_id,
        "activePlanChatId": "",
        "plan": active_plan,
        "tiktokSummary": {"posted": posted, "failed": failed, "total": len(pairs), "remaining": max(0, len(pairs) - posted)},
    }


def _apply_catalog_tiktok_to_pair(pair: dict, row: dict | None) -> bool:
    """Fill render + TikTok fields on a plan pair from a catalog row. Returns True if changed."""
    from . import catalog

    if not row:
        return False
    changed = False
    rel = str(row.get("output_rel") or "")
    if rel and pair.get("outputRel") != rel:
        pair["outputRel"] = rel
        changed = True
    if catalog.is_on_tiktok(row):
        if pair.get("tiktokStatus") != "done":
            changed = True
        pair["tiktokStatus"] = "done"
        acct = str(row.get("tiktok_account_name") or "")
        if acct and pair.get("tiktokAccountName") != acct:
            changed = True
            pair["tiktokAccountName"] = acct
        uploaded = str(row.get("tiktok_uploaded_at") or "")
        if uploaded and pair.get("tiktokUploadedAt") != uploaded:
            changed = True
            pair["tiktokUploadedAt"] = uploaded
    elif str(row.get("tiktok_status") or "") == "failed" and not pair.get("tiktokStatus"):
        pair["tiktokStatus"] = "failed"
        changed = True
    post_url = str(row.get("tiktok_post_url") or "")
    if post_url and pair.get("tiktokPostUrl") != post_url:
        pair["tiktokPostUrl"] = post_url
        changed = True
    from .duoplus import is_spark_fail_sentinel, looks_like_spark_code

    catalog_code = str(row.get("tiktok_spark_code") or "")
    if is_spark_fail_sentinel(catalog_code) or not looks_like_spark_code(catalog_code):
        catalog_code = ""
    pair_code = str(pair.get("sparkCode") or "")
    if is_spark_fail_sentinel(pair_code) or not looks_like_spark_code(pair_code):
        if pair_code:
            pair["sparkCode"] = ""
            changed = True
        pair_code = ""
    if catalog_code and pair.get("sparkCode") != catalog_code:
        pair["sparkCode"] = catalog_code
        pair_code = catalog_code
        changed = True
    spark_st = str(row.get("tiktok_spark_status") or "")
    if spark_st and pair.get("sparkStatus") != spark_st:
        pair["sparkStatus"] = spark_st
        changed = True
    spark_err = str(row.get("tiktok_spark_error") or "")
    if spark_err and pair.get("sparkError") != spark_err:
        pair["sparkError"] = spark_err
        changed = True
    spark_at = str(row.get("tiktok_spark_fetched_at") or "")
    if spark_at and pair.get("sparkFetchedAt") != spark_at:
        pair["sparkFetchedAt"] = spark_at
        changed = True
    generated = bool(pair_code)
    if bool(pair.get("sparkGenerated")) != generated:
        pair["sparkGenerated"] = generated
        changed = True
    return changed


def enrich_render_pairs_from_catalog(
    uid: str, pairs: list[dict], *, batch_id: str = ""
) -> list[dict]:
    """Return plan pairs with TikTok upload fields filled from the render catalog."""
    from . import catalog

    bid = str(batch_id or "").strip()
    valid = [p for p in pairs if isinstance(p, dict)]
    row_map = catalog.lookup_renders_for_pairs(uid, valid, batch_id=bid)
    out: list[dict] = []
    for raw in pairs:
        if not isinstance(raw, dict):
            continue
        pair = dict(raw)
        key = (
            str(pair.get("sourceId") or "").strip(),
            str(pair.get("caption") or "").strip(),
        )
        row = row_map.get(key)
        if row:
            _apply_catalog_tiktok_to_pair(pair, row)
        out.append(pair)
    return out


def sync_plan_tiktok_from_catalog(
    uid: str, thread_id: str, plan_id: str | None = None
) -> int:
    """Persist catalog TikTok upload state onto matching plan rows."""
    from . import catalog

    updated = 0

    def _apply(thread: dict) -> dict:
        nonlocal updated
        _ensure_plans_on_thread(thread)
        target = _active_plan_doc(thread)
        wanted = str(plan_id or "").strip()
        if wanted:
            hit = next((p for p in (thread.get("plans") or []) if p.get("id") == wanted), None)
            target = hit if isinstance(hit, dict) else target
        if not target:
            return thread
        pairs = target.get("renderPairs") or []
        batch_id = str(target.get("batchId") or "").strip()
        row_map = catalog.lookup_renders_for_pairs(uid, pairs, batch_id=batch_id)
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            key = (
                str(pair.get("sourceId") or "").strip(),
                str(pair.get("caption") or "").strip(),
            )
            row = row_map.get(key)
            if not row:
                continue
            if _apply_catalog_tiktok_to_pair(pair, row):
                updated += 1
        target["renderPairs"] = pairs
        pid = target.get("id")
        if pid:
            thread["plans"] = [
                target if p.get("id") == pid else p for p in (thread.get("plans") or [])
            ]
        thread["plan"] = normalize_plan(target)
        return thread

    try:
        mutate_thread(uid, thread_id, _apply)
    except KeyError:
        pass
    return updated


def plan_tiktok_status(uid: str, thread_id: str, plan_id: str | None = None) -> dict:
    """TikTok upload state for each row in a thread plan (plan storage + catalog)."""
    from . import catalog
    from .duoplus import looks_like_spark_code

    plan = get_thread_plan(uid, thread_id, plan_id)
    pairs = [p for p in (plan.get("renderPairs") or []) if isinstance(p, dict)]
    batch_id = str(plan.get("batchId") or "").strip()
    row_map = catalog.lookup_renders_for_pairs(uid, pairs, batch_id=batch_id)
    items: list[dict] = []
    posted = 0
    for pair in pairs:
        sid = str(pair.get("sourceId") or "")
        cap = str(pair.get("caption") or "").strip()
        if not sid or not cap:
            continue
        row = row_map.get((sid, cap))
        _apply_catalog_tiktok_to_pair(pair, row)
        on_tt = catalog.is_on_tiktok(row) if row else False
        plan_st = str(pair.get("tiktokStatus") or "")
        cat_st = str(row.get("tiktok_status") or "") if row else ""
        st = "done" if (on_tt or plan_st == "done") else (plan_st or cat_st)
        if st == "done":
            posted += 1
        saved_spark_code = str(
            pair.get("sparkCode") or (row.get("tiktok_spark_code") if row else "") or ""
        ).strip()
        has_saved_spark = looks_like_spark_code(saved_spark_code)
        # A plan snapshot can carry an old restart failure after its catalog
        # row successfully received the code. The code is authoritative: show
        # it as done and never hide it behind that stale failure badge.
        saved_spark_status = "done" if has_saved_spark else str(
            pair.get("sparkStatus") or (row.get("tiktok_spark_status") if row else "") or ""
        )
        saved_spark_error = "" if has_saved_spark else str(
            pair.get("sparkError") or (row.get("tiktok_spark_error") if row else "") or ""
        )
        items.append(
            {
                "sourceId": sid,
                "carouselId": str(pair.get("carouselId") or sid),
                "kind": str(pair.get("kind") or "") or ("carousel" if pair.get("slides") else "video"),
                "slides": list(pair.get("slides") or []),
                "caption": cap,
                "onTiktok": st == "done",
                "tiktokStatus": st,
                "tiktokAccountName": str(
                    pair.get("tiktokAccountName")
                    or (row.get("tiktok_account_name") if row else "")
                    or ""
                ),
                "tiktokUploadedAt": str(
                    pair.get("tiktokUploadedAt")
                    or (row.get("tiktok_uploaded_at") if row else "")
                    or ""
                ),
                "outputRel": str(
                    pair.get("outputRel")
                    or (row.get("output_rel") if row else "")
                    or ""
                ),
                "rendered": bool(
                    pair.get("kind") == "carousel"
                    or pair.get("slides")
                    or pair.get("outputRel")
                    or (row.get("output_rel") if row else "")
                ),
                "tiktokPostUrl": str(
                    pair.get("tiktokPostUrl")
                    or (row.get("tiktok_post_url") if row else "")
                    or ""
                ),
                "sparkCode": saved_spark_code if has_saved_spark else "",
                "sparkStatus": saved_spark_status,
                "sparkError": saved_spark_error,
                "sparkFetchedAt": str(
                    pair.get("sparkFetchedAt")
                    or (row.get("tiktok_spark_fetched_at") if row else "")
                    or ""
                ),
                "sparkGenerated": bool(
                    has_saved_spark
                ),
            }
        )
    return {
        "batchId": batch_id,
        "total": len(items),
        "posted": posted,
        "remaining": max(0, len(items) - posted),
        "items": items,
    }


_TIKTOK_PAIR_FIELDS = (
    "tiktokStatus",
    "tiktokAccountName",
    "tiktokUploadedAt",
    "outputRel",
    "sparkCode",
    "sparkStatus",
    "sparkError",
    "sparkFetchedAt",
    "tiktokPostUrl",
    "sparkGenerated",
)


def _merge_render_pairs_preserving_tiktok(
    old_pairs: list[dict], new_pairs: list[dict]
) -> list[dict]:
    from .duoplus import looks_like_spark_code

    old_by_key = {
        (str(p.get("sourceId") or ""), str(p.get("caption") or "").strip()): p
        for p in old_pairs
        if isinstance(p, dict) and p.get("sourceId") and p.get("caption")
    }
    merged: list[dict] = []
    for item in ws._clean_render_pairs(new_pairs):
        prev = old_by_key.get((item["sourceId"], item["caption"])) or {}
        for field in _TIKTOK_PAIR_FIELDS:
            if field == "sparkGenerated":
                prev_code = str(prev.get("sparkCode") or "")
                if looks_like_spark_code(prev_code) and not item.get("sparkGenerated"):
                    item["sparkGenerated"] = True
                continue
            if field == "sparkCode":
                prev_code = str(prev.get(field) or "")
                item_code = str(item.get(field) or "")
                if not looks_like_spark_code(item_code):
                    item["sparkCode"] = prev_code if looks_like_spark_code(prev_code) else ""
                continue
            if not item.get(field) and prev.get(field):
                item[field] = str(prev[field])
        merged.append(item)
    return merged


def _apply_plan_patch(doc: dict, patch: dict[str, Any]) -> dict:
    current = normalize_plan(doc)
    if "captions" in patch:
        current["captions"] = str(patch.get("captions") or "")
    if "mode" in patch:
        current["mode"] = patch["mode"]
    if "batchId" in patch:
        current["batchId"] = str(patch.get("batchId") or "").strip()
    if "tiktokAccountId" in patch:
        current["tiktokAccountId"] = str(patch.get("tiktokAccountId") or "").strip()
    if "tiktokAccountName" in patch:
        current["tiktokAccountName"] = str(patch.get("tiktokAccountName") or "").strip()
    if "renderPairs" in patch:
        current["renderPairs"] = _merge_render_pairs_preserving_tiktok(
            current.get("renderPairs") or [], patch["renderPairs"]
        )
    if "captionMeta" in patch:
        current["captionMeta"] = ws._clean_caption_meta(patch["captionMeta"])
    if current.get("mode") not in VALID_PLAN_MODES:
        current["mode"] = "custom"
    title = str(patch.get("title") or doc.get("title") or "").strip()[:PLAN_TITLE_MAX]
    out = {
        **normalize_plan_doc({**doc, **current}),
        **current,
        "updatedAt": time.time(),
    }
    if title:
        out["title"] = title
    return out


def patch_plan_pair_tiktok(
    uid: str,
    thread_id: str,
    *,
    source_id: str,
    caption: str,
    tiktok_status: str = "",
    tiktok_account_name: str = "",
    tiktok_uploaded_at: str = "",
    output_rel: str = "",
    tiktok_post_url: str = "",
) -> None:
    """Mark a render plan row with TikTok upload status (matched by source + caption)."""
    sid = str(source_id or "").strip()
    cap = str(caption or "").strip()
    if not sid or not cap:
        return
    fields: dict[str, str] = {}
    if tiktok_status:
        fields["tiktokStatus"] = tiktok_status
    if tiktok_account_name:
        fields["tiktokAccountName"] = tiktok_account_name
    if tiktok_uploaded_at:
        fields["tiktokUploadedAt"] = tiktok_uploaded_at
    if output_rel:
        fields["outputRel"] = output_rel
    if tiktok_post_url:
        fields["tiktokPostUrl"] = tiktok_post_url
    if not fields:
        return

    def _apply(thread: dict) -> dict:
        _ensure_plans_on_thread(thread)
        for plan in thread.get("plans") or []:
            pairs = plan.get("renderPairs") or []
            changed = False
            for pair in pairs:
                if pair.get("sourceId") == sid and pair.get("caption") == cap:
                    pair.update(fields)
                    changed = True
            if changed:
                plan["renderPairs"] = pairs
        active = _active_plan_doc(thread)
        if active:
            thread["plan"] = normalize_plan(active)
        return thread

    try:
        mutate_thread(uid, thread_id, _apply)
    except KeyError:
        pass


def _spark_job_active_for_thread(uid: str, thread_id: str) -> bool:
    """True when an in-memory spark job is still queued/running for this thread."""
    try:
        from . import job_runtime
        from . import spark_jobs

        live = spark_jobs.latest_for_thread(uid, thread_id)
        if live and str(getattr(live, "status", "") or "") in ("queued", "running"):
            return True
        for job in job_runtime.sparks.list_for(uid) or []:
            tid = str(getattr(job, "thread_id", "") or "")
            if tid == thread_id:
                return True
    except Exception:
        return False
    return False


def heal_stale_spark_running(
    uid: str, thread_id: str, plan_id: str | None = None
) -> int:
    """Flip orphaned sparkStatus=running → failed when no job is active."""
    if _spark_job_active_for_thread(uid, thread_id):
        return 0
    healed = 0

    def _apply(thread: dict) -> dict:
        nonlocal healed
        _ensure_plans_on_thread(thread)
        targets = []
        wanted = str(plan_id or "").strip()
        for plan in thread.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            if wanted and plan.get("id") != wanted:
                continue
            targets.append(plan)
        if not targets:
            active = _active_plan_doc(thread)
            if active:
                targets = [active]
        for plan in targets:
            for pair in plan.get("renderPairs") or []:
                if not isinstance(pair, dict):
                    continue
                if str(pair.get("sparkStatus") or "") != "running":
                    continue
                if looks_like_spark_code_safe(pair.get("sparkCode")):
                    pair["sparkStatus"] = "done"
                else:
                    pair["sparkStatus"] = "failed"
                    pair["sparkError"] = (
                        pair.get("sparkError")
                        or "Spark fetch interrupted - retry Fetch Spark Codes"
                    )
                    pair["sparkGenerated"] = False
                    pair["sparkCode"] = ""
                healed += 1
                rel = str(pair.get("outputRel") or "").strip()
                if rel:
                    try:
                        from . import catalog

                        catalog.update_publish(
                            uid,
                            rel,
                            {
                                "tiktok_spark_status": pair.get("sparkStatus"),
                                "tiktok_spark_error": pair.get("sparkError") or "",
                                "tiktok_spark_code": pair.get("sparkCode") or "",
                                "tiktok_spark_generated": bool(pair.get("sparkGenerated")),
                            },
                        )
                    except Exception:
                        pass
        active = _active_plan_doc(thread)
        if active:
            thread["plan"] = normalize_plan(active)
        return thread

    try:
        mutate_thread(uid, thread_id, _apply)
    except KeyError:
        return 0
    return healed


def looks_like_spark_code_safe(code: object) -> bool:
    try:
        from .duoplus import looks_like_spark_code

        return bool(looks_like_spark_code(str(code or "")))
    except Exception:
        return False


def plan_spark_status(uid: str, thread_id: str, plan_id: str | None = None) -> dict:
    """Spark-code state for each row in a thread plan (plan storage + catalog)."""
    heal_stale_spark_running(uid, thread_id, plan_id)
    payload = plan_tiktok_status(uid, thread_id, plan_id)
    items = payload.get("items") or []
    fetched = sum(1 for it in items if str(it.get("sparkCode") or "").strip())
    failed = sum(1 for it in items if str(it.get("sparkStatus") or "") == "failed")
    running = sum(1 for it in items if str(it.get("sparkStatus") or "") == "running")
    posted = int(payload.get("posted") or 0)
    return {
        "batchId": payload.get("batchId") or "",
        "total": len(items),
        "posted": posted,
        "fetched": fetched,
        "failed": failed,
        "running": running,
        "remaining": max(0, posted - fetched),
        "items": items,
    }


def patch_plan_pair_spark(
    uid: str,
    thread_id: str,
    *,
    source_id: str,
    caption: str,
    spark_code: str = "",
    spark_status: str = "",
    spark_error: str = "",
    spark_fetched_at: str = "",
    tiktok_post_url: str = "",
    spark_generated: bool = False,
) -> None:
    """Persist spark fields onto matching plan rows (source + caption)."""
    sid = str(source_id or "").strip()
    cap = str(caption or "").strip()
    if not sid or not cap:
        return
    fields: dict[str, str | bool] = {}
    if spark_code:
        fields["sparkCode"] = spark_code
    if spark_status:
        fields["sparkStatus"] = spark_status
    if spark_error:
        fields["sparkError"] = spark_error
    elif spark_status == "done":
        fields["sparkError"] = ""
    if spark_fetched_at:
        fields["sparkFetchedAt"] = spark_fetched_at
    if tiktok_post_url:
        fields["tiktokPostUrl"] = tiktok_post_url
    real_code = bool(spark_code)
    if spark_generated and real_code:
        fields["sparkGenerated"] = True
    elif spark_status in ("failed", "cancelled") or (spark_status and not real_code):
        fields["sparkGenerated"] = False
        if not spark_code:
            fields["sparkCode"] = ""
    if not fields:
        return

    def _apply(thread: dict) -> dict:
        from .duoplus import looks_like_spark_code

        _ensure_plans_on_thread(thread)
        for plan in thread.get("plans") or []:
            pairs = plan.get("renderPairs") or []
            changed = False
            for pair in pairs:
                if pair.get("sourceId") == sid and pair.get("caption") == cap:
                    pair_fields = dict(fields)
                    # A restart repair can arrive after a valid result but
                    # before its catalog write is visible. Preserve the plan's
                    # valid code rather than replacing it with that stale
                    # failed/cancelled, code-less update.
                    previous_code = str(pair.get("sparkCode") or "")
                    if (
                        looks_like_spark_code(previous_code)
                        and not looks_like_spark_code(str(spark_code or ""))
                        and spark_status in ("failed", "cancelled")
                    ):
                        pair_fields.update(
                            {
                                "sparkCode": previous_code,
                                "sparkGenerated": True,
                                "sparkStatus": "done",
                                "sparkError": "",
                            }
                        )
                    pair.update(pair_fields)
                    changed = True
            if changed:
                plan["renderPairs"] = pairs
        active = _active_plan_doc(thread)
        if active:
            thread["plan"] = normalize_plan(active)
        return thread

    try:
        mutate_thread(uid, thread_id, _apply)
    except KeyError:
        pass


def save_thread_plan(
    uid: str,
    thread_id: str,
    patch: dict[str, Any] | None,
    *,
    resolve: bool = True,
    new_plan: bool = False,
    title: str = "",
    plan_id: str | None = None,
) -> dict:
    from . import plans as plan_store

    if not _load_thread(uid, thread_id):
        raise KeyError("Thread not found")
    data = dict(patch or {})
    wanted = str(plan_id or "").strip()
    if not new_plan and not wanted:
        # A plan belongs to the chat that created it.  Never overwrite the
        # workspace's most recently selected plan just because this is a new
        # chat with no plan yet.
        thread = _load_thread(uid, thread_id) or {}
        wanted = str(thread.get("activePlanId") or "").strip()
    if not wanted or new_plan:
        created = plan_store.create_plan(
            uid,
            name=title,
            render_pairs=data.pop("renderPairs", data.pop("pairs", [])),
            selected=data.pop("selected", []),
            activate=True,
        )
        saved = plan_store.update_plan(uid, created["id"], data) if data else created
    else:
        if title:
            data["name"] = title
        saved = plan_store.update_plan(uid, wanted, data)
        if saved.get("id") != (plan_store.get_active_plan(uid) or {}).get("id"):
            saved = plan_store.activate_plan(uid, wanted)

    def _remember_active(t: dict) -> dict:
        t["activePlanId"] = saved.get("id") or ""
        t["activePlanChatId"] = ""
        return t

    mutate_thread(uid, thread_id, _remember_active, flush_index_now=False)
    return saved


def set_active_plan(uid: str, thread_id: str, plan_id: str) -> dict:
    wanted = str(plan_id or "").strip()
    if not wanted:
        raise KeyError("Plan not found")
    loc = _find_plan_location(uid, wanted)
    if not loc:
        raise KeyError("Plan not found")
    home_id, _home_thread, plan_doc = loc

    def _apply(thread: dict) -> dict:
        _ensure_plans_on_thread(thread)
        thread["activePlanId"] = wanted
        if home_id == str(thread.get("id") or ""):
            thread["activePlanChatId"] = ""
            local = next(
                (p for p in (thread.get("plans") or []) if p.get("id") == wanted),
                plan_doc,
            )
            thread["plan"] = normalize_plan(local)
        else:
            thread["activePlanChatId"] = home_id
            thread["plan"] = normalize_plan(plan_doc)
        return thread

    mutate_thread(uid, thread_id, _apply)
    return list_thread_plans(uid, thread_id)


def rename_thread_plan(uid: str, thread_id: str, plan_id: str, title: str) -> dict:
    cleaned = (title or "").strip()[:PLAN_TITLE_MAX]
    if not cleaned:
        raise ValueError("Plan title is required")
    loc = _find_plan_location(uid, plan_id)
    if not loc:
        raise KeyError("Plan not found")
    home_id = loc[0]

    def _apply(thread: dict) -> dict:
        _ensure_plans_on_thread(thread)
        found = False
        for p in thread.get("plans") or []:
            if p.get("id") == plan_id:
                p["title"] = cleaned
                p["updatedAt"] = time.time()
                _stamp_plan_chat(p, thread)
                found = True
                break
        if not found:
            raise KeyError("Plan not found")
        return thread

    mutate_thread(uid, home_id, _apply)
    return list_thread_plans(uid, thread_id)


def delete_thread_plan(uid: str, thread_id: str, plan_id: str) -> dict:
    loc = _find_plan_location(uid, plan_id)
    if not loc:
        raise KeyError("Plan not found")
    home_id = loc[0]

    def _apply(thread: dict) -> dict:
        _ensure_plans_on_thread(thread)
        plans = [p for p in (thread.get("plans") or []) if p.get("id") != plan_id]
        if len(plans) == len(thread.get("plans") or []):
            raise KeyError("Plan not found")
        thread["plans"] = plans
        if thread.get("activePlanId") == plan_id:
            nxt = plans[-1] if plans else None
            thread["activePlanId"] = (nxt or {}).get("id") or ""
            thread["activePlanChatId"] = ""
            thread["plan"] = normalize_plan(nxt) if nxt else empty_plan()
        return thread

    mutate_thread(uid, home_id, _apply)

    if home_id != thread_id:
        def _clear_view(thread: dict) -> dict:
            if (
                thread.get("activePlanId") == plan_id
                and str(thread.get("activePlanChatId") or "") == home_id
            ):
                _ensure_plans_on_thread(thread)
                local = thread.get("plans") or []
                nxt = local[-1] if local else None
                thread["activePlanId"] = (nxt or {}).get("id") or ""
                thread["activePlanChatId"] = ""
                thread["plan"] = normalize_plan(nxt) if nxt else empty_plan()
            return thread

        try:
            mutate_thread(uid, thread_id, _clear_view)
        except KeyError:
            pass

    return list_thread_plans(uid, thread_id)


def create_empty_thread_plan(uid: str, thread_id: str, *, title: str = "") -> dict:
    return save_thread_plan(
        uid,
        thread_id,
        empty_plan(),
        new_plan=True,
        title=title,
    )


def invalidate_index(uid: str) -> None:
    """Drop in-memory Recents index so the next load re-reads storage."""
    uid = str(uid or "")
    if not uid:
        return
    _index_cache.pop(uid, None)
    _index_dirty.discard(uid)


def _rebuild_index_from_chats(uid: str) -> dict:
    """Build a Recents index from chat docs when chat_index is empty/missing."""
    import logging
    _log = logging.getLogger(__name__)

    start_time = time.time()
    idx = _empty_index()
    doc_count = 0

    for doc in userstore.list_docs(uid, "chats"):
        doc_count += 1
        if not isinstance(doc, dict):
            continue
        tid = str(doc.get("id") or "").strip()
        if not tid:
            continue
        if thread_is_ephemeral(doc):
            continue
        # Spill stubs still have id/title/updatedAt — include them in Recents.
        idx["threads"].append(
            {
                "id": tid,
                "title": doc.get("title") or "New chat",
                "model": doc.get("model") or "",
                "provider": doc.get("provider") or "",
                "createdAt": doc.get("createdAt") or 0,
                "updatedAt": doc.get("updatedAt") or 0,
            }
        )

    idx["threads"] = sorted(
        idx["threads"], key=lambda t: t.get("updatedAt") or 0, reverse=True
    )[:THREAD_CAP]

    elapsed = time.time() - start_time
    _log.info(
        "Rebuilt chat index for uid=%s: scanned %d docs, found %d threads in %.2fs",
        uid[:8] if uid else "?",
        doc_count,
        len(idx["threads"]),
        elapsed
    )

    return idx


def _load_index(uid: str) -> dict:
    import logging
    _log = logging.getLogger(__name__)

    # Check in-memory cache first (fastest)
    cached = _index_cache.get(uid)
    if cached is not None:
        return cached

    start_time = time.time()

    # Load from KV storage
    data = userstore.get_kv(uid, "chat_index")
    if not isinstance(data, dict) or not isinstance(data.get("threads"), list):
        data = _empty_index()

    threads = data.get("threads") or []

    # If index is empty, rebuild from chat documents (expensive!)
    if not threads:
        _log.info("Chat index empty for uid=%s, rebuilding from docs...", uid[:8] if uid else "?")
        repaired = _rebuild_index_from_chats(uid)
        if repaired["threads"]:
            data = repaired
            _index_cache[uid] = data
            _index_dirty.add(uid)
            flush_index(uid)

            elapsed = time.time() - start_time
            _log.info("Chat index rebuilt and cached in %.2fs", elapsed)
            return data

    # Scrub ephemeral threads
    scrubbed = [t for t in threads if isinstance(t, dict) and not thread_is_ephemeral(t)]
    if len(scrubbed) != len(threads):
        data = {**data, "threads": scrubbed}
        _index_cache[uid] = data
        _index_dirty.add(uid)
        flush_index(uid)

    # Cache the result
    _index_cache[uid] = data

    elapsed = time.time() - start_time
    if elapsed > 0.1:  # Log slow index loads
        _log.info(
            "Chat index loaded: %d threads, elapsed=%.2fs",
            len(data.get("threads", [])),
            elapsed
        )

    return data


def flush_index(uid: str) -> None:
    if uid not in _index_dirty:
        return
    idx = _index_cache.get(uid)
    if idx is None:
        _index_dirty.discard(uid)
        return
    userstore.set_kv(uid, "chat_index", idx)
    _index_dirty.discard(uid)


def _touch_index_mem(uid: str, thread: dict) -> None:
    idx = _load_index(uid)
    tid = thread.get("id")
    if thread_is_ephemeral(thread):
        idx["threads"] = [t for t in idx["threads"] if t.get("id") != tid]
        _index_cache[uid] = idx
        _index_dirty.add(uid)
        return
    found = False
    for t in idx["threads"]:
        if t.get("id") == tid:
            t["title"] = thread.get("title")
            t["model"] = thread.get("model")
            t["provider"] = thread.get("provider")
            t["updatedAt"] = thread.get("updatedAt")
            found = True
            break
    if not found:
        idx["threads"].append(
            {
                "id": tid,
                "title": thread.get("title"),
                "model": thread.get("model"),
                "provider": thread.get("provider"),
                "createdAt": thread.get("createdAt"),
                "updatedAt": thread.get("updatedAt"),
            }
        )
    idx["threads"] = sorted(
        idx["threads"], key=lambda t: t.get("updatedAt") or 0, reverse=True
    )[:THREAD_CAP]
    _index_cache[uid] = idx
    _index_dirty.add(uid)


def _repair_mojibake_inplace(thread: dict, *, max_messages: int = 20) -> None:
    """Repair mojibake encoding in message content. Only processes recent messages for performance.

    Args:
        thread: Thread dict with messages array
        max_messages: Max number of recent messages to repair (0 = all). Default 20 for performance.
    """
    messages = thread.get("messages") or []
    if not messages:
        return

    # Only repair recent messages for performance (most important for user)
    # Process last N messages (most recent conversation)
    start_idx = 0 if max_messages <= 0 else max(0, len(messages) - max_messages)

    for msg in messages[start_idx:]:
        content = msg.get("content")
        if isinstance(content, str):
            fixed = repair_mojibake(content)
            if fixed != content:
                msg["content"] = fixed


def mutate_thread(
    uid: str,
    thread_id: str,
    fn: Mutator,
    *,
    flush_index_now: bool = True,
) -> dict:
    """Apply fn(thread), persist once, update the in-memory index."""

    def _apply(current: dict) -> dict:
        thread = dict(current or {})
        if not thread.get("id"):
            thread["id"] = thread_id
        _repair_mojibake_inplace(thread)
        out = fn(thread)
        if out is None:
            out = thread
        out["id"] = thread_id
        out["updatedAt"] = time.time()
        msgs = out.get("messages")
        if isinstance(msgs, list) and len(msgs) > MESSAGE_CAP:
            out["messages"] = msgs[-MESSAGE_CAP:]
        return out

    thread = userstore.mutate_doc(uid, "chats", thread_id, _apply)
    _touch_index_mem(uid, thread)
    if flush_index_now:
        flush_index(uid)
    return thread


def _load_thread(uid: str, thread_id: str, *, raise_on_storage_error: bool = False) -> dict | None:
    """Load a chat thread by ID.

    Args:
        uid: User ID
        thread_id: Thread ID to load
        raise_on_storage_error: If True, raise StorageUnavailable when cloud
            storage is unreachable instead of returning None. This allows
            callers to distinguish "thread doesn't exist" from "couldn't check".

    Returns:
        Thread dict if found, None if not found.

    Raises:
        StorageUnavailable: If raise_on_storage_error=True and storage is down.
    """
    import logging
    _log = logging.getLogger(__name__)

    start_time = time.time()
    data = userstore.get_doc(uid, "chats", thread_id)

    if not isinstance(data, dict):
        # Check if this is a storage error vs genuine miss
        if raise_on_storage_error and not userstore.storage_is_healthy():
            error_detail = userstore.last_storage_error()
            raise StorageUnavailable(
                f"Cloud storage unavailable{': ' + error_detail if error_detail else ''}"
            )
        return None

    # Only repair recent messages for performance
    _repair_mojibake_inplace(data, max_messages=20)

    elapsed = time.time() - start_time
    msg_count = len(data.get("messages") or [])
    if elapsed > 0.5:  # Log slow loads
        _log.warning(
            "Slow chat load: thread=%s, messages=%d, elapsed=%.2fs",
            thread_id[:8],
            msg_count,
            elapsed
        )

    return data


def list_threads(uid: str) -> list[dict]:
    idx = _load_index(uid)
    threads = sorted(
        [
            t
            for t in idx["threads"]
            if not thread_is_ephemeral(t)
        ],
        key=lambda t: t.get("updatedAt") or 0,
        reverse=True,
    )
    return threads


def create_thread(
    uid: str,
    *,
    title: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    provider_models: dict | None = None,
    ephemeral: bool = False,
) -> dict:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    clean_title = (title or "New chat").strip()[:TITLE_MAX] or "New chat"
    ephemeral = bool(ephemeral) or is_ephemeral_title(clean_title)
    thread = {
        "id": tid,
        "title": clean_title,
        "model": model or "",
        "provider": provider or "",
        "providerModels": dict(provider_models or {}),
        "createdAt": now,
        "updatedAt": now,
        "messages": [],
        "pendingConfirm": None,
        "pendingReview": None,
        "pinterestBank": [],
        "pinterestShown": [],
        "plan": empty_plan(),
        "plans": [],
        "activePlanId": "",
        "activePlanChatId": "",
    }
    if ephemeral:
        thread["ephemeral"] = True
    userstore.set_doc(uid, "chats", tid, thread)
    _touch_index_mem(uid, thread)
    flush_index(uid)
    return thread


def get_thread(uid: str, thread_id: str) -> dict:
    # Do not collapse a D1/state-plane outage into a false "not found".  A
    # caller that has just created a chat otherwise receives a 404 even though
    # the document may have committed successfully.
    thread = _load_thread(uid, thread_id, raise_on_storage_error=True)
    if not thread:
        raise KeyError("Thread not found")
    # Plans are workspace-scoped. Do not run the retired chat-plan migration on
    # every chat open: it was both a Firestore-heavy read path and could revive
    # stale plans from historical tool messages.
    thread["plan"] = empty_plan()
    thread["plans"] = []
    thread["activePlanId"] = ""
    thread["activePlanChatId"] = ""
    return thread


def update_thread(
    uid: str,
    thread_id: str,
    *,
    title: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    provider_models: dict | None = None,
) -> dict:
    def _apply(thread: dict) -> dict:
        if title is not None:
            thread["title"] = (title or "").strip()[:TITLE_MAX] or thread["title"]
            if is_ephemeral_title(thread["title"]):
                thread["ephemeral"] = True
            for p in thread.get("plans") or []:
                if isinstance(p, dict):
                    _stamp_plan_chat(p, thread)
        if model is not None:
            thread["model"] = model
        if provider is not None:
            thread["provider"] = provider
        if provider_models is not None:
            current = thread.get("providerModels")
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(provider_models)
            thread["providerModels"] = merged
        return thread

    return mutate_thread(uid, thread_id, _apply)


def delete_thread(uid: str, thread_id: str) -> None:
    get_thread(uid, thread_id)
    userstore.delete_doc(uid, "chats", thread_id)
    idx = _load_index(uid)
    idx["threads"] = [t for t in idx["threads"] if t.get("id") != thread_id]
    _index_cache[uid] = idx
    _index_dirty.add(uid)
    flush_index(uid)


def _slim_pinterest_pins_for_store(pins: list) -> list[dict]:
    """Keep UI fields only so large search results stay valid JSON."""
    out: list[dict] = []
    for p in pins or []:
        if not isinstance(p, dict):
            continue
        url = (p.get("url") or p.get("pinUrl") or "").strip()
        if not url:
            continue
        title = str(p.get("title") or p.get("name") or "")[:160]
        out.append(
            {
                "pinId": p.get("pinId") or "",
                "pinUrl": url,
                "url": url,
                "title": title,
                "caption": str(p.get("caption") or title or "")[:200],
                "description": str(p.get("description") or "")[:200],
                "thumbnail": p.get("thumbnail") or p.get("thumbUrl") or "",
                "previewUrl": p.get("previewUrl") or "",
                "previewKind": p.get("previewKind") or "",
                "duration": p.get("duration") or 0,
                "width": p.get("width") or 0,
                "height": p.get("height") or 0,
            }
        )
    return out


def _merge_pin_lists(existing: list, incoming: list, *, limit: int = 200) -> list[dict]:
    by_url: dict[str, dict] = {}
    for p in _slim_pinterest_pins_for_store(list(existing or []) + list(incoming or [])):
        key = (p.get("pinUrl") or p.get("url") or "").rstrip("/").lower()
        if key:
            by_url[key] = p
    # Prefer newest last so UI order is stable; keep last `limit`
    vals = list(by_url.values())
    if len(vals) > limit:
        vals = vals[-limit:]
    return vals


def merge_pinterest_bank(uid: str, thread_id: str, pins: list) -> dict:
    slim = _slim_pinterest_pins_for_store(pins)

    def _apply(thread: dict) -> dict:
        thread["pinterestBank"] = _merge_pin_lists(
            thread.get("pinterestBank") or [], slim
        )
        return thread

    return mutate_thread(uid, thread_id, _apply, flush_index_now=False)


def set_pinterest_shown(
    uid: str, thread_id: str, pins: list, *, mode: str = "replace"
) -> dict:
    slim = _slim_pinterest_pins_for_store(pins)
    mode_l = (mode or "replace").strip().lower()

    def _apply(thread: dict) -> dict:
        if mode_l == "merge":
            thread["pinterestShown"] = _merge_pin_lists(
                thread.get("pinterestShown") or [], slim
            )
        else:
            thread["pinterestShown"] = slim
        # Always keep bank in sync with anything we show
        thread["pinterestBank"] = _merge_pin_lists(
            thread.get("pinterestBank") or [], slim
        )
        return thread

    return mutate_thread(uid, thread_id, _apply, flush_index_now=False)


def get_pinterest_pins(
    uid: str, thread_id: str, *, which: str = "shown"
) -> list[dict]:
    thread = get_thread(uid, thread_id)
    if which == "bank":
        return list(thread.get("pinterestBank") or [])
    if which == "shown":
        shown = list(thread.get("pinterestShown") or [])
        if shown:
            return shown
        return list(thread.get("pinterestBank") or [])
    return list(thread.get("pinterestBank") or [])


def _tool_row(name: str, call_id: str, result: dict) -> dict:
    payload = dict(result) if isinstance(result, dict) else {"ok": False, "error": "bad result"}
    payload.pop("_visionUrls", None)
    payload.pop("_visionPaths", None)
    if name in ("search_pinterest", "pinterest_set") and isinstance(
        payload.get("pins"), list
    ):
        payload["pins"] = _slim_pinterest_pins_for_store(payload["pins"])
        payload["count"] = len(payload["pins"])
    raw = json.dumps(payload, ensure_ascii=False)
    # Never mid-cut JSON — Pinterest batches need the full pin list for the chat grid.
    limit = 120_000 if name in ("search_pinterest", "pinterest_set") else 8000
    if len(raw) > limit and name in ("search_pinterest", "pinterest_set"):
        pins = list(payload.get("pins") or [])
        while len(raw) > limit and pins:
            pins.pop()
            payload["pins"] = pins
            payload["count"] = len(pins)
            payload["truncated"] = True
            raw = json.dumps(payload, ensure_ascii=False)
    elif len(raw) > limit:
        raw = raw[:limit]
    return {
        "role": "tool",
        "name": name,
        "tool_call_id": str(call_id or ""),
        "content": raw,
        "id": uuid.uuid4().hex[:10],
        "createdAt": time.time(),
    }


def _insert_tool_into_messages(msgs: list[dict], row: dict) -> list[dict]:
    cid = str(row.get("tool_call_id") or "")
    for m in msgs:
        if m.get("role") == "tool" and str(m.get("tool_call_id") or "") == cid:
            m["content"] = row["content"]
            m["name"] = row["name"]
            return msgs
    insert_at = None
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        ids = [str(tc.get("id") or "") for tc in (m.get("tool_calls") or [])]
        if cid in ids:
            insert_at = i + 1
            while insert_at < len(msgs) and msgs[insert_at].get("role") == "tool":
                insert_at += 1
            break
    if insert_at is None:
        msgs.append(row)
    else:
        msgs.insert(insert_at, row)
    return msgs[-MESSAGE_CAP:]


def place_tool_result(msgs: list[dict], name: str, call_id: str, result: dict) -> list[dict]:
    return _insert_tool_into_messages(msgs, _tool_row(name, call_id, result))


def insert_tool_result(
    uid: str,
    thread_id: str,
    *,
    name: str,
    call_id: str,
    result: dict,
    flush_index_now: bool = True,
) -> dict:
    """Write a tool_result next to its tool_use, or replace one already there."""
    row = _tool_row(name, call_id, result)

    def _apply(thread: dict) -> dict:
        msgs = list(thread.get("messages") or [])
        thread["messages"] = _insert_tool_into_messages(msgs, row)
        return thread

    return mutate_thread(uid, thread_id, _apply, flush_index_now=flush_index_now)


def insert_tool_results(
    uid: str,
    thread_id: str,
    rows: list[tuple[str, str, dict]],
) -> dict:
    """Insert many tool results in one persist (name, call_id, result)."""

    def _apply(thread: dict) -> dict:
        msgs = list(thread.get("messages") or [])
        for name, call_id, result in rows:
            msgs = _insert_tool_into_messages(msgs, _tool_row(name, call_id, result))
        thread["messages"] = msgs
        return thread

    return mutate_thread(uid, thread_id, _apply)


def append_message(uid: str, thread_id: str, message: dict[str, Any]) -> dict:
    def _apply(thread: dict) -> dict:
        msgs = list(thread.get("messages") or [])
        msg = dict(message)
        if isinstance(msg.get("content"), str):
            msg["content"] = repair_mojibake(msg["content"])
        msg.setdefault("id", uuid.uuid4().hex[:10])
        msg.setdefault("createdAt", time.time())
        msgs.append(msg)
        thread["messages"] = msgs
        if (thread.get("title") or "New chat") == "New chat":
            if msg.get("role") == "user" and (msg.get("content") or "").strip():
                text = " ".join(str(msg["content"]).split())
                thread["title"] = (text[:TITLE_MAX] + ("…" if len(text) > TITLE_MAX else ""))
        return thread

    return mutate_thread(uid, thread_id, _apply)


def set_pending_confirm(uid: str, thread_id: str, pending: dict | None) -> dict:
    return mutate_thread(uid, thread_id, lambda t: {**t, "pendingConfirm": pending})


def set_pending_review(uid: str, thread_id: str, pending: dict | None) -> dict:
    return mutate_thread(uid, thread_id, lambda t: {**t, "pendingReview": pending})


def replace_messages(uid: str, thread_id: str, messages: list[dict]) -> dict:
    return mutate_thread(
        uid, thread_id, lambda t: {**t, "messages": list(messages)[-MESSAGE_CAP:]}
    )


def _compare_content_for_selected(
    message: dict,
    *,
    model_id: str | None,
    provider: str | None,
) -> str:
    """Use the selected model's /all row as the prior assistant turn."""
    bots = message.get("bots") or []
    if not isinstance(bots, list) or not bots:
        return message.get("content") or ""
    picked = None
    if model_id:
        picked = next((b for b in bots if b.get("model") == model_id), None)
    if picked is None and provider:
        picked = next((b for b in bots if b.get("provider") == provider), None)
    if picked is None:
        return message.get("content") or ""
    text = (picked.get("content") or "").strip()
    if not text and picked.get("error"):
        text = str(picked.get("error") or "").strip()
    return text


def messages_for_model(
    thread: dict,
    *,
    model_id: str | None = None,
    provider: str | None = None,
) -> list[dict]:
    """Strip bulky tool payloads before sending to the LLM.

    After /all, pass the currently selected model so that model continues
    from its own row instead of a mashup of every bot.
    """
    out = []
    for m in thread.get("messages") or []:
        role = m.get("role")
        if role not in ("user", "assistant", "tool", "system"):
            continue
        if role == "assistant" and m.get("source") in ("compare", "multiple"):
            content = _compare_content_for_selected(
                m, model_id=model_id, provider=provider
            )
            if not content:
                content = "(No reply from this model on the previous multi-model turn.)"
            out.append({"role": "assistant", "content": content})
            continue
        entry = {"role": role, "content": m.get("content") or ""}
        if role == "user" and m.get("pinterestPrompt"):
            entry["content"] = m["pinterestPrompt"]
        if role == "user" and m.get("attachmentNote"):
            note = str(m.get("attachmentNote") or "").strip()
            if note:
                entry["content"] = (
                    f"{entry['content']}\n\n{note}".strip()
                    if entry["content"]
                    else note
                )
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("gemini_parts"):
            entry["gemini_parts"] = m["gemini_parts"]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            entry["name"] = m["name"]
        # Truncate huge tool results and long research dumps
        if role == "tool" and isinstance(entry["content"], str) and len(entry["content"]) > 4000:
            entry["content"] = entry["content"][:4000] + "…[truncated]"
        if (
            role == "assistant"
            and m.get("source") == "perplexity"
            and isinstance(entry["content"], str)
            and len(entry["content"]) > 20000
        ):
            entry["content"] = entry["content"][:20000] + "\n…[research truncated for the model]"
        out.append(entry)
    return ensure_tool_results(out)


def ensure_tool_results(messages: list[dict]) -> list[dict]:
    """Guarantee every assistant tool_use is followed by tool_result rows.

    Anthropic rejects a user/assistant turn after tool_use until each id has a
    result. Chat during an idea-review pause used to leave propose_ideas open.
    """
    out: list[dict] = []
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()

    def flush_missing() -> None:
        for cid, name in pending:
            if cid in seen:
                continue
            out.append(
                {
                    "role": "tool",
                    "name": name,
                    "tool_call_id": cid,
                    "content": json.dumps(
                        {
                            "ok": False,
                            "interrupted": True,
                            "error": "No tool result was recorded; the user continued in chat.",
                        }
                    ),
                }
            )
        pending.clear()
        seen.clear()

    for m in messages or []:
        role = m.get("role")
        calls = m.get("tool_calls") if role == "assistant" else None
        if role == "assistant" and calls:
            flush_missing()
            out.append(m)
            pending = [
                (str(tc.get("id") or ""), str(tc.get("name") or "tool"))
                for tc in calls
                if tc.get("id")
            ]
            seen = set()
            continue
        if pending and role == "tool":
            tid = str(m.get("tool_call_id") or "")
            if tid:
                out.append(m)
                seen.add(tid)
            continue
        if pending:
            flush_missing()
        out.append(m)
    flush_missing()
    return out


def repair_tool_pairing(uid: str, thread_id: str) -> dict:
    def _apply(thread: dict) -> dict:
        original = list(thread.get("messages") or [])
        fixed = ensure_tool_results(original)
        if len(fixed) != len(original):
            thread["messages"] = fixed[-MESSAGE_CAP:]
        return thread

    return mutate_thread(uid, thread_id, _apply)
