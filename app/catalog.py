"""Render catalog: one document per output, CSV export, skip-index.

Firestore (or local _fs fallback) is the source of truth. CSV is generated
on download — it is not a second database.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage
from . import userstore
from .config import user_manifest_path, user_output_dir

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_skip_maps: dict[str, dict[str, str]] = {}
_flag_cache: dict[tuple[str, str], bool] = {}
_renders_list_cache: dict[str, tuple[float, list[dict]]] = {}
_RENDERS_LIST_TTL = 8.0

ADS_METRICS = (
    "spend",
    "impressions",
    "cpm",
    "clicks",
    "ctr",
    "cpc",
    "reach",
    "results",
    "result_type",
    "cost_per_result",
    "insights_synced_at",
)

COLUMNS: tuple[str, ...] = (
    "render_id",
    "created_at",
    "created_via",
    "chat_thread_id",
    "batch_id",
    "batch_mode",
    "job_key",
    "offer_id",
    "offer_name",
    "payout_type",
    "payout_amount",
    "payout_share_percent",
    "payout_currency",
    "payout_note",
    "source_id",
    "source_name",
    "source_stem",
    "source_platform",
    "source_url",
    "pin_url",
    "source_hash",
    "source_duration_s",
    "source_width",
    "source_height",
    "hook",
    "end_caption_text",
    "end_caption_seconds",
    "crop",
    "trim",
    "caption_mode",
    "font_weight",
    "font_size_ratio",
    "stroke_ratio",
    "y_ratio",
    "max_width_ratio",
    "preserve_breaks",
    "force_lower",
    "render_status",
    "error",
    "output_rel",
    "output_filename",
    "output_folder",
    "file_size_bytes",
    "destination",
    "on_tiktok",
    "on_facebook",
    "tiktok_status",
    "tiktok_account_id",
    "tiktok_account_name",
    "tiktok_description",
    "tiktok_uploaded_at",
    "tiktok_post_url",
    "tiktok_error",
    "tiktok_spark_code",
    "tiktok_spark_status",
    "tiktok_spark_error",
    "tiktok_spark_fetched_at",
    "tiktok_spark_task_id",
    "tiktok_spark_diagnostics",
    "tiktok_spark_generated",
    "tiktok_spark_generated_at",
    "tiktok_advertiser_id",
    "tiktok_campaign_id",
    "tiktok_adgroup_id",
    "tiktok_ad_id",
    "facebook_status",
    "facebook_ad_account_id",
    "facebook_campaign_id",
    "facebook_campaign_name",
    "facebook_adset_id",
    "facebook_ad_id",
    "facebook_video_id",
    "facebook_creative_id",
    "facebook_landing_url",
    "facebook_created_at",
    "facebook_error",
    *tuple(f"fb_{k}" for k in ADS_METRICS),
    *tuple(f"tt_{k}" for k in ADS_METRICS),
    "click_count",
    "last_click_at",
    "post_kind",
    "slide_ids",
    "slide_count",
)


def empty_row() -> dict[str, Any]:
    row = {k: "" for k in COLUMNS}
    row["created_via"] = "unknown"
    row["destination"] = "library"
    row["on_tiktok"] = False
    row["on_facebook"] = False
    row["payout_type"] = "unknown"
    row["payout_currency"] = "USD"
    row["click_count"] = 0
    return row


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _cell(value: Any) -> str:
    if value is None or value is False:
        return ""
    if value is True:
        return "true"
    return str(value)


def destination_of(on_tiktok: bool, on_facebook: bool) -> str:
    if on_tiktok and on_facebook:
        return "both"
    if on_tiktok:
        return "tiktok"
    if on_facebook:
        return "facebook"
    return "library"


def is_on_tiktok(row: dict | None) -> bool:
    if not row:
        return False
    return _truthy(row.get("on_tiktok")) or str(row.get("tiktok_status") or "") == "done"


def is_on_tiktok_rel(uid: str, output_rel: str) -> bool:
    if not output_rel:
        return False
    return is_on_tiktok(get_by_output_rel(uid, output_rel))


def recompute_destination(row: dict) -> dict:
    on_tt = is_on_tiktok(row)
    on_fb = _truthy(row.get("on_facebook")) or (row.get("facebook_status") == "done")
    row["on_tiktok"] = bool(on_tt)
    row["on_facebook"] = bool(on_fb)
    row["destination"] = destination_of(bool(on_tt), bool(on_fb))
    return row


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v or "").strip().lower() in ("1", "true", "yes")


def output_rel_of(uid: str, path: str | Path | None) -> str:
    if not path:
        return ""
    p = Path(path)
    root = user_output_dir(uid)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        try:
            rel = storage.rel_of(uid, p)
        except ValueError:
            return p.name
        if rel.startswith("output/"):
            return rel[len("output/") :]
        return rel


def _output_index_id(output_rel: str) -> str:
    return hashlib.sha256((output_rel or "").encode("utf-8")).hexdigest()[:24]


def _flag_done(uid: str, key: str) -> bool:
    cached = _flag_cache.get((uid, key))
    if cached:
        return True
    data = userstore.get_kv(uid, key)
    done = bool(data and data.get("done"))
    if done:
        _flag_cache[(uid, key)] = True
    return done


def _set_flag(uid: str, key: str, data: dict) -> None:
    userstore.set_kv(uid, key, data)
    if data.get("done"):
        _flag_cache[(uid, key)] = True


def skip_map(uid: str) -> dict[str, str]:
    """job_key -> output_rel, loaded once per process then kept in memory."""
    _migrate_manifest(uid)
    cached = _skip_maps.get(uid)
    if cached is not None:
        return cached
    out: dict[str, str] = {}
    for row in userstore.list_docs(uid, "skipIndex"):
        key = str(row.get("job_key") or row.get("id") or "")
        rel = str(row.get("output_rel") or row.get("rel") or "")
        if key and rel:
            out[key] = rel
    _skip_maps[uid] = out
    return out


def skip_get(uid: str, job_key: str) -> str | None:
    if not job_key:
        return None
    rel = skip_map(uid).get(job_key)
    return rel or None


def skip_set(uid: str, job_key: str, output_rel: str) -> None:
    if not job_key or not output_rel:
        return
    userstore.set_doc(
        uid, "skipIndex", job_key, {"output_rel": output_rel, "job_key": job_key}
    )
    _skip_maps.setdefault(uid, {})[job_key] = output_rel


def _migrate_manifest(uid: str) -> None:
    """One-time copy of rendered.json into skipIndex docs."""
    if _flag_done(uid, "_migrated_manifest"):
        return
    data = storage.load_json(uid, user_manifest_path(uid), skip_firestore=True) or {}
    if isinstance(data, dict):
        for key, rel in data.items():
            if isinstance(key, str) and isinstance(rel, str) and key and rel:
                if userstore.get_doc(uid, "skipIndex", key) is None:
                    userstore.set_doc(
                        uid, "skipIndex", key, {"output_rel": rel, "job_key": key}
                    )
    _set_flag(uid, "_migrated_manifest", {"done": True})


def get_by_output_rel(uid: str, output_rel: str) -> dict | None:
    if not output_rel:
        return None
    idx = userstore.get_doc(uid, "outputIndex", _output_index_id(output_rel))
    if idx and idx.get("render_id"):
        return userstore.get_doc(uid, "renders", str(idx["render_id"]))
    return None


def _pair_key(source_id: str | None, caption: str | None) -> tuple[str, str]:
    return (str(source_id or "").strip(), str(caption or "").strip())


def invalidate_renders_list(uid: str) -> None:
    _renders_list_cache.pop(uid, None)


def _list_render_rows(uid: str) -> list[dict]:
    now = time.monotonic()
    hit = _renders_list_cache.get(uid)
    if hit and (now - hit[0]) < _RENDERS_LIST_TTL:
        return hit[1]

    start_time = time.time()
    rows = userstore.list_docs(uid, "renders")

    elapsed = time.time() - start_time
    if elapsed > 0.5:  # Log slow catalog scans
        _log.warning(
            "Slow render catalog scan: uid=%s, rows=%d, elapsed=%.2fs",
            uid[:8] if uid else "?",
            len(rows),
            elapsed
        )

    _renders_list_cache[uid] = (now, rows)
    return rows


def _better_pair_row(cur: dict | None, row: dict, *, batch_id: str = "") -> dict:
    if cur is None:
        return row
    bid = str(batch_id or "").strip()

    def rank(r: dict) -> tuple[int, int, int, int]:
        spark = 1 if str(r.get("tiktok_spark_code") or "").strip() else 0
        url = 1 if str(r.get("tiktok_post_url") or "").strip() else 0
        on = 1 if is_on_tiktok(r) else 0
        batch = 1 if bid and str(r.get("batch_id") or "") == bid else 0
        return (spark, url, on, batch)

    if rank(row) > rank(cur):
        return row
    return cur


def lookup_renders_for_pairs(
    uid: str,
    pairs: list[dict],
    *,
    batch_id: str = "",
) -> dict[tuple[str, str], dict]:
    """Best catalog row per (source_id, caption). Lists render docs once."""
    needed: set[tuple[str, str]] = set()
    by_rel: dict[str, tuple[str, str]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        key = _pair_key(
            pair.get("sourceId") or pair.get("source_id"),
            pair.get("caption") or pair.get("hook"),
        )
        if not key[0] or not key[1]:
            continue
        needed.add(key)
        rel = str(
            pair.get("outputRel") or pair.get("output_rel") or ""
        ).replace("\\", "/").lstrip("/")
        if rel:
            by_rel[rel] = key
    if not needed:
        return {}
    bid = str(batch_id or "").strip()
    best: dict[tuple[str, str], dict] = {}
    for rel, key in by_rel.items():
        row = get_by_output_rel(uid, rel)
        if row:
            best[key] = _better_pair_row(best.get(key), row, batch_id=bid)
    missing = needed - set(best)
    if not missing:
        return best
    for row in _list_render_rows(uid):
        key = _pair_key(row.get("source_id"), row.get("hook"))
        if key not in needed:
            continue
        best[key] = _better_pair_row(best.get(key), row, batch_id=bid)
    return best


def find_render_by_pair(
    uid: str,
    source_id: str,
    caption: str,
    *,
    output_rel: str = "",
    batch_id: str = "",
) -> dict | None:
    """Best catalog row for a plan pair; prefers rows already on TikTok."""
    pair = {
        "sourceId": source_id,
        "caption": caption,
        "outputRel": output_rel,
    }
    hit = lookup_renders_for_pairs(uid, [pair], batch_id=batch_id)
    return hit.get(_pair_key(source_id, caption))


def posted_video_ids(uid: str, account_name: str = "") -> set[str]:
    """Numeric TikTok item ids already stored on catalog rows."""
    from . import tiktok_post_url as tpu

    handle = (account_name or "").strip().lstrip("@").lower()
    ids: set[str] = set()
    for row in _list_render_rows(uid):
        url = str(row.get("tiktok_post_url") or "")
        vid = tpu.video_id_of(url)
        if not vid:
            continue
        if handle:
            row_acct = str(row.get("tiktok_account_name") or "").strip().lstrip("@").lower()
            if row_acct and row_acct != handle:
                continue
        ids.add(vid)
    return ids


def find_by_spark_code(uid: str, spark_code: str) -> dict | None:
    from .duoplus import looks_like_spark_code

    code = (spark_code or "").strip()
    if not looks_like_spark_code(code):
        return None
    for row in _list_render_rows(uid):
        if str(row.get("tiktok_spark_code") or "").strip() == code:
            return row
    return None


def find_by_post_url(uid: str, post_url: str) -> dict | None:
    from . import tiktok_post_url as tpu

    vid = tpu.video_id_of(post_url)
    if not vid:
        return None
    for row in _list_render_rows(uid):
        if tpu.video_id_of(str(row.get("tiktok_post_url") or "")) == vid:
            return row
    return None


def spark_is_locked(
    uid: str,
    *,
    post_url: str = "",
    output_rel: str = "",
) -> bool:
    """True if this render/video already has a real Spark code (not the fail sentinel)."""
    from .duoplus import looks_like_spark_code

    rel = (output_rel or "").strip()
    if rel:
        row = get_by_output_rel(uid, rel)
        if row and looks_like_spark_code(str(row.get("tiktok_spark_code") or "")):
            return True
    row = find_by_post_url(uid, post_url) if post_url else None
    if row and looks_like_spark_code(str(row.get("tiktok_spark_code") or "")):
        return True
    return False


def _index_output(uid: str, render_id: str, output_rel: str) -> None:
    if not output_rel or not render_id:
        return
    userstore.set_doc(
        uid,
        "outputIndex",
        _output_index_id(output_rel),
        {"render_id": render_id, "output_rel": output_rel},
    )


def upsert_render(uid: str, fields: dict[str, Any]) -> dict:
    with _lock:
        return _upsert_render(uid, fields)


def _upsert_render(uid: str, fields: dict[str, Any]) -> dict:
    output_rel = str(fields.get("output_rel") or "")
    existing = None
    if output_rel:
        existing = get_by_output_rel(uid, output_rel)
    row = empty_row()
    if existing:
        row.update({k: existing[k] for k in COLUMNS if k in existing})
        row["render_id"] = existing.get("render_id") or existing.get("id") or row.get("render_id")
    else:
        row["render_id"] = str(fields.get("render_id") or uuid.uuid4().hex[:16])
        row["created_at"] = fields.get("created_at") or _iso(time.time())
    for key in COLUMNS:
        if key in fields and fields[key] is not None:
            row[key] = fields[key]
    recompute_destination(row)
    rid = str(row["render_id"])
    userstore.set_doc(uid, "renders", rid, row)
    invalidate_renders_list(uid)
    if output_rel:
        _index_output(uid, rid, output_rel)
    job_key = str(row.get("job_key") or "")
    if job_key and output_rel:
        skip_set(uid, job_key, output_rel)
    return row


def update_publish(uid: str, output_rel: str, patch: dict[str, Any]) -> dict | None:
    if not output_rel:
        return None
    with _lock:
        row = get_by_output_rel(uid, output_rel)
        if not row:
            row = empty_row()
            row["render_id"] = uuid.uuid4().hex[:16]
            row["created_at"] = _iso(time.time())
            row["created_via"] = "unknown"
            row["output_rel"] = output_rel
            row["output_filename"] = Path(output_rel).name
            row["output_folder"] = str(Path(output_rel).parent).replace("\\", "/")
            if row["output_folder"] == ".":
                row["output_folder"] = ""
            row["render_status"] = "done"
        merged = dict(row)
        merged.update({k: v for k, v in patch.items() if k in COLUMNS and v is not None})
        merged["output_rel"] = output_rel
        return _upsert_render(uid, merged)


def record_job_item(uid: str, batch: Any, item: Any, job_key: str) -> dict:
    """Write a catalog row after a JobItem finishes (done / skipped / failed)."""
    settings = getattr(batch, "settings", None) or {}
    output_rel = output_rel_of(uid, getattr(item, "output", None))
    size = 0
    if item.output:
        p = Path(item.output)
        try:
            size = p.stat().st_size if p.is_file() else 0
        except OSError:
            size = 0
    end = item.end_caption or {}
    crop = item.crop or {}
    trim = item.trim or {}
    crop_s = ""
    if crop.get("enabled"):
        crop_s = ",".join(
            str(crop.get(k, "")) for k in ("x", "y", "w", "h")
        )
    trim_s = ""
    if trim.get("start") is not None and trim.get("end") is not None:
        trim_s = f"{trim.get('start')}-{trim.get('end')}"
    folder = ""
    filename = ""
    if output_rel:
        filename = Path(output_rel).name
        folder = str(Path(output_rel).parent).replace("\\", "/")
        if folder == ".":
            folder = ""
    fields = {
        "created_via": getattr(batch, "created_via", None) or "unknown",
        "chat_thread_id": getattr(batch, "chat_thread_id", None) or "",
        "batch_id": getattr(batch, "id", "") or "",
        "batch_mode": getattr(batch, "batch_mode", None) or "",
        "job_key": job_key,
        "offer_id": getattr(batch, "offer_id", None) or "",
        "offer_name": getattr(batch, "offer_name", None) or "",
        "payout_type": getattr(batch, "payout_type", None) or "unknown",
        "payout_amount": getattr(batch, "payout_amount", None) or "",
        "payout_share_percent": getattr(batch, "payout_share_percent", None) or "",
        "payout_currency": getattr(batch, "payout_currency", None) or "USD",
        "payout_note": getattr(batch, "payout_note", None) or "",
        "source_id": getattr(item, "source_id", None) or "",
        "source_name": getattr(item, "source_name", None) or "",
        "source_stem": item.video_stem or "",
        "source_platform": getattr(item, "source_platform", None) or "",
        "source_url": getattr(item, "source_url", None) or "",
        "pin_url": getattr(item, "pin_url", None) or "",
        "source_hash": item.video_hash or "",
        "source_duration_s": getattr(item, "source_duration_s", None) or item.duration or "",
        "source_width": getattr(item, "source_width", None) or "",
        "source_height": getattr(item, "source_height", None) or "",
        "hook": item.caption or "",
        "end_caption_text": end.get("text") or "",
        "end_caption_seconds": end.get("durationSeconds") or "",
        "crop": crop_s,
        "trim": trim_s,
        "caption_mode": settings.get("captionMode") or "",
        "font_weight": settings.get("fontWeight") or "",
        "font_size_ratio": settings.get("fontSizeRatio") or "",
        "stroke_ratio": settings.get("strokeRatio") or "",
        "y_ratio": settings.get("yRatio") or "",
        "max_width_ratio": settings.get("maxWidthRatio") or "",
        "preserve_breaks": settings.get("preserveLineBreaks", ""),
        "force_lower": settings.get("forceLowercase", ""),
        "render_status": item.status or "",
        "error": item.error or "",
        "output_rel": output_rel,
        "output_filename": filename,
        "output_folder": folder,
        "file_size_bytes": size or "",
    }
    if item.status == "skipped" and output_rel:
        existing = get_by_output_rel(uid, output_rel)
        if existing:
            # Don't duplicate a prior encode; keep origin if it was unknown.
            if (existing.get("created_via") or "unknown") == "unknown" and fields["created_via"] != "unknown":
                existing = dict(existing)
                existing["created_via"] = fields["created_via"]
                existing["chat_thread_id"] = fields["chat_thread_id"] or existing.get("chat_thread_id")
                existing["batch_id"] = fields["batch_id"] or existing.get("batch_id")
                return upsert_render(uid, existing)
            skip_set(uid, job_key, output_rel)
            return existing
    return upsert_render(uid, fields)


def record_carousel(
    uid: str,
    *,
    carousel_id: str,
    slides: list[str],
    caption: str,
    chat_thread_id: str = "",
    offer_id: str = "",
    created_via: str = "chat",
) -> dict:
    """Catalog row for a photo carousel (no encoded video file)."""
    cid = str(carousel_id or "").strip()
    if not cid:
        raise ValueError("carousel_id required")
    slide_ids = [str(s).strip() for s in (slides or []) if str(s).strip()][:5]
    output_rel = f"carousels/{cid}"
    return upsert_render(
        uid,
        {
            "created_via": created_via or "chat",
            "chat_thread_id": chat_thread_id or "",
            "offer_id": offer_id or "",
            "source_id": cid,
            "source_name": f"{len(slide_ids)}-slide carousel",
            "hook": str(caption or "").strip(),
            "render_status": "ready",
            "output_rel": output_rel,
            "output_filename": cid,
            "output_folder": "carousels",
            "post_kind": "carousel",
            "slide_ids": ",".join(slide_ids),
            "slide_count": len(slide_ids),
        },
    )


_outputs_backfill_started: set[str] = set()
_outputs_backfill_lock = threading.Lock()


def _schedule_outputs_backfill(uid: str) -> None:
    if _flag_done(uid, "_backfilled_outputs"):
        return
    with _outputs_backfill_lock:
        if uid in _outputs_backfill_started:
            return
        _outputs_backfill_started.add(uid)

    def _run() -> None:
        try:
            backfill_outputs(uid)
        except Exception:
            _log.exception("Background outputs backfill failed for %s", uid)
        finally:
            with _outputs_backfill_lock:
                _outputs_backfill_started.discard(uid)

    threading.Thread(target=_run, name="out-backfill", daemon=True).start()


def list_renders(
    uid: str,
    *,
    created_via: str | None = None,
    destination: str | None = None,
    on_tiktok: bool | None = None,
    on_facebook: bool | None = None,
    offer_id: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "-created_at",
    limit: int = 500,
) -> list[dict]:
    # Use the TTL-backed list path — do not re-stream Firestore every call.
    # Never block GET on disk backfill.
    _schedule_outputs_backfill(uid)
    rows = [recompute_destination(dict(r)) for r in _list_render_rows(uid)]
    if created_via:
        rows = [r for r in rows if (r.get("created_via") or "") == created_via]
    if destination:
        rows = [r for r in rows if (r.get("destination") or "") == destination]
    if on_tiktok is not None:
        rows = [r for r in rows if _truthy(r.get("on_tiktok")) is on_tiktok]
    if on_facebook is not None:
        rows = [r for r in rows if _truthy(r.get("on_facebook")) is on_facebook]
    if offer_id:
        rows = [r for r in rows if (r.get("offer_id") or "") == offer_id]
    if status:
        rows = [r for r in rows if (r.get("render_status") or "") == status]
    if date_from:
        rows = [r for r in rows if str(r.get("created_at") or "") >= date_from]
    if date_to:
        rows = [r for r in rows if str(r.get("created_at") or "") <= date_to]
    reverse = sort.startswith("-")
    key = sort.lstrip("-") or "created_at"
    if key not in COLUMNS:
        key = "created_at"
    rows.sort(key=lambda r: str(r.get(key) or ""), reverse=reverse)
    return rows[: max(1, min(int(limit or 500), 2000))]


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in COLUMNS})
    text = buf.getvalue()
    return ("\ufeff" + text).encode("utf-8")


def list_output_items(uid: str, *, limit: int = 200) -> list[dict]:
    """Library/outputs tab: catalog render docs, not a live R2 listing."""
    _schedule_outputs_backfill(uid)
    limit = max(1, min(500, int(limit)))
    items: list[dict] = []
    for row in _list_render_rows(uid):
        rel = str(row.get("output_rel") or "").replace("\\", "/").lstrip("/")
        if rel.startswith("output/"):
            rel = rel[len("output/") :]
        if not rel:
            continue
        if str(row.get("post_kind") or "") == "carousel" or rel.startswith("carousels/"):
            continue
        status = str(row.get("render_status") or "done")
        if status not in ("done", "skipped", ""):
            continue
        filename = str(row.get("output_filename") or Path(rel).name)
        folder = str(row.get("output_folder") or "")
        try:
            size = int(float(row.get("file_size_bytes") or 0))
        except (TypeError, ValueError):
            size = 0
        created = str(row.get("created_at") or "")
        mtime = 0.0
        if created:
            try:
                mtime = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                mtime = 0.0
        items.append(
            {
                "path": rel,
                "filename": filename,
                "folder": folder,
                "sizeBytes": size,
                "mtime": mtime,
                "label": f"{folder + '/' if folder else ''}{filename}",
                "hook": str(row.get("hook") or ""),
                "onTiktok": _truthy(row.get("on_tiktok")),
                "tiktokStatus": str(row.get("tiktok_status") or ""),
                "tiktokAccountName": str(row.get("tiktok_account_name") or ""),
                "tiktokError": str(row.get("tiktok_error") or ""),
                "tiktokUploadedAt": str(row.get("tiktok_uploaded_at") or ""),
                "tiktokDescription": str(row.get("tiktok_description") or ""),
                "tiktokPostUrl": str(row.get("tiktok_post_url") or ""),
                "tiktokSparkCode": str(row.get("tiktok_spark_code") or ""),
                "tiktokSparkStatus": str(row.get("tiktok_spark_status") or ""),
                "tiktokSparkError": str(row.get("tiktok_spark_error") or ""),
                "tiktokSparkFetchedAt": str(row.get("tiktok_spark_fetched_at") or ""),
                "tiktokSparkGenerated": _truthy(row.get("tiktok_spark_generated")),
            }
        )
    items.sort(key=lambda it: (it["mtime"], it["label"]), reverse=True)
    return items[:limit]


def backfill_outputs(uid: str) -> int:
    """Index existing output/ files that have no catalog row yet."""
    if _flag_done(uid, "_backfilled_outputs"):
        return 0
    added = 0
    try:
        from . import tiktok_upload

        items = tiktok_upload.list_rendered_videos(uid, limit=2000)
    except Exception:
        items = []
    for it in items:
        rel = it.get("path") or ""
        if not rel:
            continue
        if get_by_output_rel(uid, rel):
            continue
        upsert_render(
            uid,
            {
                "created_via": "unknown",
                "created_at": _iso(it.get("mtime")),
                "render_status": "done",
                "output_rel": rel,
                "output_filename": it.get("filename") or Path(rel).name,
                "output_folder": it.get("folder") or "",
                "file_size_bytes": it.get("sizeBytes") or "",
            },
        )
        added += 1
    _set_flag(uid, "_backfilled_outputs", {"done": True, "added": added})
    return added
