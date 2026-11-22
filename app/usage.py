"""Per-user token/cost ledger for chat models, Pinterest search, and research."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from . import pricing
from . import userstore

_log = logging.getLogger(__name__)
_locks_guard = threading.Lock()
_uid_locks: dict[str, threading.Lock] = {}
_stores: dict[str, dict[str, Any]] = {}
_dirty: set[str] = set()
_flush_timers: dict[str, threading.Timer] = {}
_loaded_at: dict[str, float] = {}
FLUSH_DELAY_S = 2.0
_LOAD_TTL_S = 30.0

EVENT_KEEP_SECONDS = 14 * 86400
DAY_KEEP = 45
THREAD_KEEP = 200
RangeName = Literal["session", "today", "week", "lifetime"]


def _empty_totals() -> dict[str, Any]:
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "costUsd": 0.0,
        "credits": 0,
        "calls": 0,
        "priced": True,
    }


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "lifetime": {},
        "days": {},
        "threads": {},
        "events": [],
    }


def _lock_for(uid: str) -> threading.Lock:
    with _locks_guard:
        lock = _uid_locks.get(uid)
        if lock is None:
            lock = threading.Lock()
            _uid_locks[uid] = lock
        return lock


def invalidate(uid: str) -> None:
    """Drop in-memory ledger so the next read reloads from UserStore/Firestore."""
    uid = str(uid or "")
    if not uid:
        return
    with _locks_guard:
        timer = _flush_timers.pop(uid, None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
    with _lock_for(uid):
        _stores.pop(uid, None)
        _loaded_at.pop(uid, None)
        _dirty.discard(uid)


def _load(uid: str) -> dict[str, Any]:
    # Trust in-memory copy for a short TTL so usage-chip polls do not hit
    # Firestore/disk every time. Dirty stores always win (unflushed writes).
    if uid in _dirty:
        cached = _stores.get(uid)
        if cached is not None:
            return cached
    cached = _stores.get(uid)
    loaded = _loaded_at.get(uid) or 0.0
    if cached is not None and (time.monotonic() - loaded) < _LOAD_TTL_S:
        return cached
    data = userstore.get_kv(uid, "usage")
    out = _empty_store()
    if isinstance(data, dict):
        for key in ("lifetime", "days", "threads"):
            val = data.get(key)
            if isinstance(val, dict):
                out[key] = val
        events = data.get("events")
        if isinstance(events, list):
            out["events"] = [e for e in events if isinstance(e, dict)]
    _stores[uid] = out
    _loaded_at[uid] = time.monotonic()
    return out


def flush_now(uid: str) -> None:
    """Write the in-memory ledger. Call at the end of a chat turn."""
    with _locks_guard:
        timer = _flush_timers.pop(uid, None)
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass
    with _lock_for(uid):
        if uid not in _dirty:
            return
        store = _stores.get(uid)
        if store is None:
            _dirty.discard(uid)
            return
        userstore.patch_kv(uid, "usage", lambda _cur: store)
        _dirty.discard(uid)


def _schedule_flush(uid: str) -> None:
    with _locks_guard:
        old = _flush_timers.pop(uid, None)
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        timer = threading.Timer(FLUSH_DELAY_S, flush_now, args=(uid,))
        timer.daemon = True
        _flush_timers[uid] = timer
        timer.start()


def _save(uid: str, data: dict[str, Any], *, immediate: bool = False) -> None:
    _stores[uid] = data
    _dirty.add(uid)
    if immediate:
        flush_now(uid)
    else:
        _schedule_flush(uid)


def _add_into(bucket: dict[str, Any], delta: dict[str, Any]) -> None:
    for key in ("inputTokens", "outputTokens", "cacheReadTokens", "credits", "calls"):
        bucket[key] = int(bucket.get(key) or 0) + int(delta.get(key) or 0)
    bucket["costUsd"] = float(bucket.get("costUsd") or 0) + float(delta.get("costUsd") or 0)
    if delta.get("priced") is False:
        bucket["priced"] = False
    elif "priced" not in bucket:
        bucket["priced"] = True


def _model_key(provider: str, model: str) -> str:
    return f"{(provider or '').strip()}:{(model or '').strip()}"


def _apply_event(store: dict[str, Any], event: dict[str, Any]) -> None:
    key = _model_key(str(event.get("provider") or ""), str(event.get("model") or ""))
    delta = {
        "inputTokens": int(event.get("inputTokens") or 0),
        "outputTokens": int(event.get("outputTokens") or 0),
        "cacheReadTokens": int(event.get("cacheReadTokens") or 0),
        "costUsd": float(event.get("costUsd") or 0),
        "credits": int(event.get("credits") or 0),
        "calls": int(event.get("calls") or 1),
        "priced": bool(event.get("priced", True)),
    }
    life = store.setdefault("lifetime", {})
    if key not in life:
        life[key] = _empty_totals()
    _add_into(life[key], delta)

    day = event.get("dayUtc") or ""
    if day:
        days = store.setdefault("days", {})
        if day not in days:
            days[day] = {}
        if key not in days[day]:
            days[day][key] = _empty_totals()
        _add_into(days[day][key], delta)

    tid = str(event.get("threadId") or "").strip()
    if tid:
        threads = store.setdefault("threads", {})
        row = threads.get(tid)
        if not isinstance(row, dict):
            row = {"updatedAt": 0.0, "byKey": {}}
            threads[tid] = row
        row["updatedAt"] = float(event.get("ts") or time.time())
        by_key = row.setdefault("byKey", {})
        if key not in by_key:
            by_key[key] = _empty_totals()
        _add_into(by_key[key], delta)


def _compact(store: dict[str, Any], now: float) -> None:
    cutoff = now - EVENT_KEEP_SECONDS
    events = [e for e in (store.get("events") or []) if float(e.get("ts") or 0) >= cutoff]
    store["events"] = events[-4000:]

    days = store.get("days") or {}
    if isinstance(days, dict) and len(days) > DAY_KEEP:
        keep = sorted(days.keys())[-DAY_KEEP:]
        store["days"] = {k: days[k] for k in keep}

    threads = store.get("threads") or {}
    if isinstance(threads, dict) and len(threads) > THREAD_KEEP:
        ranked = sorted(
            threads.items(),
            key=lambda item: float((item[1] or {}).get("updatedAt") or 0),
            reverse=True,
        )
        store["threads"] = dict(ranked[:THREAD_KEEP])


def _append(uid: str, event: dict[str, Any]) -> None:
    with _lock_for(uid):
        store = _load(uid)
        now = float(event.get("ts") or time.time())
        event["ts"] = now
        event["dayUtc"] = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        store.setdefault("events", []).append(event)
        _apply_event(store, event)
        _compact(store, now)
        _save(uid, store)


def record_llm(
    uid: str,
    *,
    thread_id: str = "",
    provider: str,
    model: str,
    usage: dict[str, Any] | None = None,
    kind: str = "chat",
) -> None:
    blob = usage if isinstance(usage, dict) else {}
    input_tokens = int(blob.get("input_tokens") or 0)
    output_tokens = int(blob.get("output_tokens") or 0)
    cache_read = int(blob.get("cache_read_tokens") or 0)
    cache_write = int(blob.get("cache_write_tokens") or 0)
    if input_tokens <= 0 and output_tokens <= 0 and cache_read <= 0 and cache_write <= 0:
        return
    cost, priced = pricing.cost_usd(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
    try:
        _append(
            uid,
            {
                "threadId": (thread_id or "").strip(),
                "provider": provider,
                "model": model,
                "kind": kind,
                "inputTokens": input_tokens + cache_read,
                "outputTokens": output_tokens,
                "cacheReadTokens": cache_read,
                "costUsd": cost,
                "credits": 0,
                "calls": 1,
                "priced": priced,
            },
        )
    except Exception:
        _log.exception("Failed to record LLM usage")


def record_pinterest(uid: str, *, thread_id: str = "", credits: int) -> None:
    pages = max(0, int(credits or 0))
    if pages <= 0:
        pages = 1
    try:
        _append(
            uid,
            {
                "threadId": (thread_id or "").strip(),
                "provider": "pinterest",
                "model": pricing.PINTEREST_MODEL,
                "kind": "pinterest",
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "costUsd": pricing.pinterest_cost_usd(pages),
                "credits": pages,
                "calls": 1,
                "priced": True,
            },
        )
    except Exception:
        _log.exception("Failed to record Pinterest usage")


def record_research(
    uid: str,
    *,
    thread_id: str = "",
    usage: dict[str, Any] | None = None,
) -> None:
    blob = usage if isinstance(usage, dict) else {}
    input_tokens = int(blob.get("input_tokens") or 0)
    output_tokens = int(blob.get("output_tokens") or 0)
    cache_read = int(blob.get("cache_read_tokens") or 0)
    cost, priced = pricing.research_cost_usd(blob)
    try:
        _append(
            uid,
            {
                "threadId": (thread_id or "").strip(),
                "provider": "perplexity",
                "model": pricing.RESEARCH_MODEL,
                "kind": "research",
                "inputTokens": input_tokens + cache_read,
                "outputTokens": output_tokens,
                "cacheReadTokens": cache_read,
                "costUsd": cost,
                "credits": 0,
                "calls": 1,
                "priced": priced,
            },
        )
    except Exception:
        _log.exception("Failed to record Perplexity usage")


def _zone(tz_name: str, utc_offset_min: int):
    name = (tz_name or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    if utc_offset_min:
        return timezone(timedelta(minutes=int(utc_offset_min)))
    return timezone.utc


def _local_day_start(now: datetime, tz) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(now: datetime, tz) -> datetime:
    start = _local_day_start(now, tz)
    return start - timedelta(days=start.weekday())


def _from_events(events: list[dict], start_ts: float, end_ts: float) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        ts = float(event.get("ts") or 0)
        if ts < start_ts or ts >= end_ts:
            continue
        key = _model_key(str(event.get("provider") or ""), str(event.get("model") or ""))
        if key not in out:
            out[key] = _empty_totals()
        _add_into(
            out[key],
            {
                "inputTokens": int(event.get("inputTokens") or 0),
                "outputTokens": int(event.get("outputTokens") or 0),
                "cacheReadTokens": int(event.get("cacheReadTokens") or 0),
                "costUsd": float(event.get("costUsd") or 0),
                "credits": int(event.get("credits") or 0),
                "calls": int(event.get("calls") or 1),
                "priced": bool(event.get("priced", True)),
            },
        )
    return out


def _labels() -> dict[str, tuple[str, str]]:
    """key -> (id, label)."""
    from . import chat_models

    out: dict[str, tuple[str, str]] = {}
    for m in chat_models.MODELS:
        out[_model_key(m["provider"], m["id"])] = (m["id"], m.get("label") or m["id"])
    out[_model_key("pinterest", pricing.PINTEREST_MODEL)] = (
        pricing.PINTEREST_MODEL,
        "Pinterest search",
    )
    out[_model_key("perplexity", pricing.RESEARCH_MODEL)] = (
        pricing.RESEARCH_MODEL,
        "Perplexity research",
    )
    return out


def summarize(
    uid: str,
    *,
    range_name: RangeName = "session",
    thread_id: str = "",
    tz_name: str = "",
    utc_offset_min: int = 0,
) -> dict[str, Any]:
    flush_now(uid)
    with _lock_for(uid):
        store = _load(uid)
    now = datetime.now(timezone.utc)
    tz = _zone(tz_name, utc_offset_min)

    if range_name == "session":
        tid = (thread_id or "").strip()
        row = (store.get("threads") or {}).get(tid) if tid else None
        by_key = (row or {}).get("byKey") if isinstance(row, dict) else {}
        by_key = by_key if isinstance(by_key, dict) else {}
    elif range_name == "today":
        start = _local_day_start(now, tz)
        by_key = _from_events(store.get("events") or [], start.timestamp(), (start + timedelta(days=1)).timestamp())
    elif range_name == "week":
        start = _week_start(now, tz)
        by_key = _from_events(store.get("events") or [], start.timestamp(), (start + timedelta(days=7)).timestamp())
    else:
        by_key = store.get("lifetime") if isinstance(store.get("lifetime"), dict) else {}

    labels = _labels()
    models: list[dict[str, Any]] = []
    totals = _empty_totals()
    for key, row in (by_key or {}).items():
        if not isinstance(row, dict):
            continue
        _add_into(totals, row)
        provider, _, model_id = key.partition(":")
        known = labels.get(key)
        models.append(
            {
                "id": known[0] if known else (model_id or key),
                "provider": provider,
                "label": known[1] if known else (model_id or key),
                "inputTokens": int(row.get("inputTokens") or 0),
                "outputTokens": int(row.get("outputTokens") or 0),
                "costUsd": round(float(row.get("costUsd") or 0), 6),
                "credits": int(row.get("credits") or 0),
                "calls": int(row.get("calls") or 0),
                "priced": bool(row.get("priced", True)),
            }
        )
    models.sort(key=lambda m: (-float(m.get("costUsd") or 0), -(m.get("calls") or 0), m.get("label") or ""))
    return {
        "range": range_name,
        "inputTokens": int(totals.get("inputTokens") or 0),
        "outputTokens": int(totals.get("outputTokens") or 0),
        "costUsd": round(float(totals.get("costUsd") or 0), 6),
        "credits": int(totals.get("credits") or 0),
        "calls": int(totals.get("calls") or 0),
        "models": models,
        "note": "Tracking started when this feature shipped.",
    }
