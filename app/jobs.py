"""Batch job engine: matrix/pair expansion, worker thread, progress, cancel,
resume/skip of already-rendered outputs.

Version C: batches belong to a user (owner_uid). Finished outputs are
mirrored to R2. Skip-existing and the render catalog live in Firestore
(users/{uid}/skipIndex and users/{uid}/renders) with a local _fs fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import job_runtime
from . import storage
from .config import MAX_BATCH_JOBS, user_output_dir
from .encode import BurnedCaptionError, EncodeError, burn_caption, crop_payload, encode_factor, probe, trim_payload
from .layout import normalize_settings
from .naming import batch_folder_name, build_name, make_batch_output_dir, resolve_collision

_log = logging.getLogger(__name__)
_manifest_lock = threading.Lock()


def _end_caption_payload(end_caption: dict | None) -> dict | None:
    if not end_caption:
        return None
    text = (end_caption.get("text") or "").strip()
    if not text:
        return None
    try:
        dur = float(end_caption.get("durationSeconds") or 3.0)
    except (TypeError, ValueError):
        dur = 3.0
    try:
        x_off = float(end_caption.get("xOffset") or 0.0)
    except (TypeError, ValueError):
        x_off = 0.0
    try:
        y_off = float(end_caption.get("yOffset") or 0.0)
    except (TypeError, ValueError):
        y_off = 0.0
    return {
        "text": text,
        "durationSeconds": max(2.0, min(5.0, dur)),
        "xOffset": max(-40.0, min(40.0, x_off)),
        "yOffset": max(-40.0, min(40.0, y_off)),
    }


def _job_key(
    video_hash: str,
    caption: str,
    settings: dict,
    end_caption: dict | None = None,
    crop: dict | None = None,
    trim: dict | None = None,
) -> str:
    s = normalize_settings(settings)
    relevant = {
        k: s.get(k)
        for k in (
            "fontWeight", "strokeRatio", "fontSizeRatio", "maxWidthRatio",
            "yRatio", "lineHeight", "maxLines", "forceLowercase",
            "preserveLineBreaks", "captionMode", "boxPadRatio", "boxCornerRatio",
        )
    }
    end = _end_caption_payload(end_caption)
    crop_key = crop_payload(crop)
    trim_key = trim_payload(trim)
    payload = json.dumps(
        [video_hash, caption, relevant, end, crop_key, trim_key], sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


@dataclass
class JobItem:
    video_path: str
    video_stem: str
    video_hash: str
    video_index: int
    caption: str
    caption_index: int
    end_caption: dict | None = None
    crop: dict | None = None
    trim: dict | None = None
    status: str = "pending"  # pending | encoding | done | skipped | failed
    output: str | None = None
    error: str | None = None
    progress: float = 0.0  # seconds encoded
    duration: float = 0.0
    source_id: str = ""
    source_name: str = ""
    source_platform: str = ""
    source_url: str = ""
    pin_url: str = ""
    source_width: int | float | str = ""
    source_height: int | float | str = ""
    source_duration_s: float | str = ""


@dataclass
class Batch:
    id: str
    owner_uid: str
    items: list[JobItem]
    settings: dict
    name_template: str | None
    output_dir: str
    date_label: str  # same string as {date} / folder time stamp
    status: str = "queued"  # queued | running | done | cancelled
    cancel_flag: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    created_via: str = "unknown"
    chat_thread_id: str = ""
    offer_id: str = ""
    offer_name: str = ""
    payout_type: str = "unknown"
    payout_amount: str | float = ""
    payout_share_percent: str | float = ""
    payout_currency: str = "USD"
    payout_note: str = ""
    batch_mode: str = "matrix"




def expand_jobs(videos: list[dict], captions: list[str], mode: str) -> list[tuple[dict, int, str, int]]:
    """Return list of (video, video_index, caption, caption_index)."""
    if mode == "custom":
        raise ValueError("Custom mode needs an explicit pair list")
    if mode == "pair":
        if len(videos) != len(captions):
            raise ValueError(
                f"Pair mode needs equal counts (got {len(videos)} videos, {len(captions)} captions)"
            )
        return [(v, i, captions[i], i) for i, v in enumerate(videos)]
    return [
        (v, vi, c, ci)
        for vi, v in enumerate(videos)
        for ci, c in enumerate(captions)
    ]


def estimate_seconds(
    videos: list[dict],
    captions: list[str],
    mode: str,
    pairs_override: list[tuple[dict, int, str, int]] | None = None,
) -> float:
    pairs = pairs_override if pairs_override is not None else expand_jobs(videos, captions, mode)
    factor = encode_factor()
    total = 0.0
    for v, _, _, _ in pairs:
        dur = float(v.get("duration") or 30.0)
        trim = trim_payload(v.get("trim"), dur)
        if trim:
            dur = max(0.1, float(trim["end"]) - float(trim["start"]))
        total += dur * factor
    return total


def _normalize_output_rel(prior_rel: str) -> str:
    rel = str(prior_rel or "").replace("\\", "/").lstrip("/")
    if rel.startswith("output/"):
        rel = rel[len("output/") :]
    return rel


def _resolve_prior_output_rel(
    uid: str,
    *,
    job_key: str,
    source_id: str,
    caption: str,
    skip: dict[str, str],
) -> str:
    """Find an existing output_rel for a pair.

    Prefer skipIndex by job_key (exact style match). Fall back to the render
    catalog by source+caption so Upload can reuse files when crop/style/hash
    drift would otherwise force a pointless re-encode.
    """
    from . import catalog

    prior_rel = skip.get(job_key) if job_key else None
    if prior_rel:
        return _normalize_output_rel(prior_rel)

    sid = str(source_id or "").strip()
    cap = str(caption or "").strip()
    if not sid or not cap:
        return ""
    row = catalog.find_render_by_pair(uid, sid, cap)
    if not row:
        return ""
    status = str(row.get("render_status") or "")
    if status and status not in ("done", "skipped"):
        return ""
    return _normalize_output_rel(str(row.get("output_rel") or ""))


def resolve_existing_outputs(
    uid: str,
    pairs: list[tuple[dict, int, str, int]],
    settings: dict,
) -> dict:
    """Split plan pairs into already-rendered outputs vs still needing encode.

    Returns paths relative to the user's output dir (same as /api/outputs).
    """
    from . import catalog

    skip = catalog.skip_map(uid)
    ready: list[dict] = []
    missing: list[dict] = []
    paths: list[str] = []
    seen_paths: set[str] = set()

    for v, _vi, caption, _ci in pairs:
        key = _job_key(
            str(v.get("hash") or ""),
            caption,
            settings,
            _end_caption_payload(v.get("endCaption")),
            crop_payload(v.get("crop")),
            trim_payload(v.get("trim"), float(v.get("duration") or 0)),
        )
        source_id = str(v.get("id") or "")
        row = {
            "sourceId": source_id,
            "caption": caption,
        }
        prior_rel = _resolve_prior_output_rel(
            uid,
            job_key=key,
            source_id=source_id,
            caption=caption,
            skip=skip,
        )
        rel = ""
        if prior_rel:
            prior = storage.ensure_output_local(uid, prior_rel)
            if prior and prior.is_file():
                rel = catalog.output_rel_of(uid, prior)
            else:
                # Still indexed — accept stored rel even if not local yet (R2).
                rel = prior_rel
        if rel:
            row["path"] = rel
            row["jobKey"] = key
            ready.append(row)
            if rel not in seen_paths:
                seen_paths.add(rel)
                paths.append(rel)
        else:
            missing.append(row)

    return {
        "ready": ready,
        "missing": missing,
        "paths": paths,
        "readyCount": len(ready),
        "missingCount": len(missing),
        "allReady": len(missing) == 0 and len(ready) > 0,
    }


def user_has_running_batch(uid: str) -> bool:
    return job_runtime.batches.has_running(uid)


def start_batch(
    uid: str,
    videos: list[dict],
    captions: list[str],
    mode: str,
    settings: dict,
    name_template: str | None,
    skip_existing: bool = True,
    pairs_override: list[tuple[dict, int, str, int]] | None = None,
    *,
    created_via: str = "unknown",
    chat_thread_id: str | None = None,
    offer_id: str | None = None,
    offer_name: str | None = None,
    payout_type: str | None = None,
    payout_amount: str | float | None = None,
    payout_share_percent: str | float | None = None,
    payout_currency: str | None = None,
    payout_note: str | None = None,
) -> Batch:
    pairs = pairs_override if pairs_override is not None else expand_jobs(videos, captions, mode)
    if len(pairs) > MAX_BATCH_JOBS:
        raise ValueError(f"Batch too large ({len(pairs)} jobs). Max is {MAX_BATCH_JOBS}.")
    if not pairs:
        raise ValueError("No jobs to render")
    blocked = sorted(
        {str(v.get("name") or v.get("id") or "video") for v, _, _, _ in pairs if v.get("burnedCaption")}
    )
    if blocked:
        raise ValueError(
            "Blocked: these clips already have a caption burned in and can't be "
            "used as a render base: " + ", ".join(blocked)
        )
    items = []
    for v, vi, caption, ci in pairs:
        dur = float(v.get("duration") or 0)
        trim = trim_payload(v.get("trim"), dur)
        clip_dur = dur
        if trim:
            clip_dur = max(0.1, float(trim["end"]) - float(trim["start"]))
        items.append(
            JobItem(
                video_path=v["path"],
                video_stem=v["stem"],
                video_hash=v["hash"],
                video_index=vi,
                caption=caption,
                caption_index=ci,
                end_caption=_end_caption_payload(v.get("endCaption")),
                crop=crop_payload(v.get("crop")),
                trim=trim,
                duration=clip_dur,
                source_id=str(v.get("id") or ""),
                source_name=str(v.get("name") or v.get("stem") or ""),
                source_platform=str(v.get("platform") or v.get("origin") or ""),
                source_url=str(v.get("url") or ""),
                pin_url=str(v.get("pinUrl") or v.get("pin_url") or ""),
                source_width=v.get("width") or "",
                source_height=v.get("height") or "",
                source_duration_s=dur or "",
            )
        )
    from datetime import datetime

    when = datetime.now()
    date_label = batch_folder_name(when)
    run_dir = make_batch_output_dir(user_output_dir(uid), when=when)
    via = (created_via or "unknown").strip().lower()
    if via not in ("create", "chat", "unknown"):
        via = "unknown"
    batch = Batch(
        id=uuid.uuid4().hex[:12],
        owner_uid=uid,
        items=items,
        settings=settings,
        name_template=name_template,
        output_dir=str(run_dir),
        date_label=date_label,
        created_via=via,
        chat_thread_id=(chat_thread_id or "").strip(),
        offer_id=(offer_id or "").strip(),
        offer_name=(offer_name or "").strip(),
        payout_type=(payout_type or "unknown").strip() or "unknown",
        payout_amount=payout_amount if payout_amount is not None else "",
        payout_share_percent=payout_share_percent if payout_share_percent is not None else "",
        payout_currency=(payout_currency or "USD").strip() or "USD",
        payout_note=(payout_note or "").strip(),
        batch_mode=mode or "matrix",
        status="queued",
        started_at=None,
    )
    return job_runtime.batches.submit(
        batch, _run_batch, args=(batch, skip_existing), name=f"batch-{batch.id}"
    )


def _usable_media(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _ensure_source_local(uid: str, item: JobItem) -> str:
    """Ensure the batch item's source video exists on local disk.

    Scratch under DATA_DIR is ephemeral (container restart / retention). Prefer
    R2 restore, then re-download from the original URL when available. Raises
    EncodeError with an actionable message instead of handing a missing path to
    ffprobe.
    """
    path = Path(item.video_path)
    if _usable_media(path):
        return str(path)

    rel: str | None = None
    try:
        rel = storage.rel_of(uid, path)
    except ValueError:
        rel = None
    if rel:
        restored = storage.ensure_local(uid, rel)
        if restored and _usable_media(restored):
            return str(restored)

    url = (item.source_url or "").strip()
    lib_src: dict | None = None
    if item.source_id:
        try:
            from . import library

            lib_src = library.state(uid)["sources"].get(item.source_id)
        except Exception:
            _log.debug("library lookup for source restore failed", exc_info=True)
            lib_src = None
    if lib_src:
        if not url:
            url = (lib_src.get("url") or "").strip()
        lib_path = Path(lib_src.get("path") or "")
        if _usable_media(lib_path):
            return str(lib_path)
        if lib_path.name and lib_path != path:
            try:
                lib_rel = storage.rel_of(uid, lib_path)
            except ValueError:
                lib_rel = None
            if lib_rel:
                got = storage.ensure_local(uid, lib_rel)
                if got and _usable_media(got):
                    return str(got)

    name = item.source_name or path.name or "video"
    if url:
        try:
            from .ingest import download_url

            new_path = download_url(uid, url)
        except Exception as exc:
            raise EncodeError(
                f"Source video missing and re-download failed for {name}: {exc}"
            ) from exc
        if not _usable_media(new_path):
            raise EncodeError(f"Re-downloaded source is empty or unreadable: {name}")
        if lib_src is not None and item.source_id:
            try:
                from . import library

                with library._registry_lock:
                    live = library.state(uid)["sources"].get(item.source_id)
                    if live is not None:
                        live["path"] = str(new_path)
                library.save_session(uid)
            except Exception:
                _log.debug("library path update after re-download failed", exc_info=True)
        try:
            storage.mirror_up(uid, new_path)
        except Exception as exc:
            _log.warning("R2 re-mirror failed for %s: %s", new_path, exc)
        return str(new_path)

    hint = (
        " Re-add the video from its URL, or ensure R2 storage is configured."
        if not storage.ENABLED
        else " Re-add the video (it was not found in R2 either)."
    )
    raise EncodeError(f"Source video missing and could not be restored: {name}.{hint}")


def _flag_burned_source(uid: str, item: JobItem) -> None:
    """Remember that this source is already captioned so it is never picked again."""
    if not item.source_id:
        return
    try:
        from . import library

        library.set_burned_caption(uid, item.source_id, True, reason="render-check")
    except Exception:
        _log.warning("could not flag captioned source %s", item.source_id, exc_info=True)


def _run_batch(batch: Batch, skip_existing: bool) -> None:
    from . import catalog

    uid = batch.owner_uid

    def _persist() -> None:
        try:
            batch_state(batch)
        except Exception:
            pass

    try:
        if batch.cancel_flag:
            batch.status = "cancelled"
            batch.finished_at = time.time()
            _persist()
            return

        run_dir = Path(batch.output_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        skip = catalog.skip_map(uid) if skip_existing else {}
        _persist()
        for item in batch.items:
            if batch.cancel_flag:
                item.status = "skipped"
                item.error = "batch cancelled"
                continue

            key = _job_key(
                item.video_hash,
                item.caption,
                batch.settings,
                item.end_caption,
                item.crop,
                item.trim,
            )
            if skip_existing:
                prior_rel = skip.get(key)
                if prior_rel:
                    prior = storage.ensure_output_local(uid, prior_rel)
                    if prior and prior.is_file():
                        item.status = "skipped"
                        item.output = str(prior)
                        try:
                            catalog.record_job_item(uid, batch, item, key)
                        except Exception:
                            _log.warning("catalog record skipped failed", exc_info=True)
                        _persist()
                        continue

            name = build_name(
                batch.name_template,
                item.video_stem,
                item.caption,
                item.caption_index,
                item.video_index,
                date_str=batch.date_label,
            )
            out_path = resolve_collision(run_dir, name)
            item.status = "encoding"
            _persist()
            try:
                item.video_path = _ensure_source_local(uid, item)
                if not item.duration:
                    item.duration = probe(item.video_path)["duration"]

                def on_progress(seconds: float, _item=item) -> None:
                    _item.progress = seconds

                seek_start = None
                duration_limit = None
                if item.trim:
                    seek_start = float(item.trim["start"])
                    duration_limit = max(
                        0.1, float(item.trim["end"]) - float(item.trim["start"])
                    )

                burn_caption(
                    item.video_path,
                    item.caption,
                    out_path,
                    batch.settings,
                    progress_cb=on_progress,
                    cancel_check=lambda: batch.cancel_flag,
                    end_caption=item.end_caption,
                    crop=item.crop,
                    seek_start=seek_start,
                    duration_limit=duration_limit,
                )
                if storage.ENABLED:
                    try:
                        storage.mirror_up(uid, out_path)
                    except Exception as exc:
                        item.status = "failed"
                        item.error = f"R2 upload failed: {exc}"
                        Path(out_path).unlink(missing_ok=True)
                    else:
                        item.status = "done"
                        item.output = str(out_path)
                        with _manifest_lock:
                            catalog.skip_set(uid, key, catalog.output_rel_of(uid, out_path))
                else:
                    item.status = "done"
                    item.output = str(out_path)
                    with _manifest_lock:
                        catalog.skip_set(uid, key, catalog.output_rel_of(uid, out_path))
            except BurnedCaptionError as exc:
                item.status = "failed"
                item.error = str(exc)
                _flag_burned_source(uid, item)
            except EncodeError as exc:
                item.status = "failed" if str(exc) != "cancelled" else "skipped"
                item.error = str(exc)
            except Exception as exc:  # keep batch alive on unexpected errors
                item.status = "failed"
                item.error = f"{type(exc).__name__}: {exc}"
            try:
                catalog.record_job_item(uid, batch, item, key)
            except Exception:
                _log.warning("catalog record failed", exc_info=True)
            _persist()

        batch.status = "cancelled" if batch.cancel_flag else "done"
        batch.finished_at = time.time()
        _persist()

        if batch.status == "done":
            try:
                from . import tiktok_upload

                tiktok_upload.maybe_auto_upload_batch(uid, batch)
            except Exception:
                # Never fail a finished batch because auto-upload errored.
                pass
    finally:
        if batch.status == "running":
            batch.status = "cancelled" if batch.cancel_flag else "done"
            batch.finished_at = batch.finished_at or time.time()
            _persist()
        try:
            job_runtime.batches.promote(uid)
        except Exception:
            _log.warning("batch promote failed", exc_info=True)


def get_batch(batch_id: str, uid: str | None = None) -> Batch | None:
    return job_runtime.batches.get(batch_id, uid)


def cancel_batch(batch_id: str, uid: str | None = None) -> bool:
    return job_runtime.batches.cancel(batch_id, uid)


def _batch_eta_seconds(batch: Batch) -> int | None:
    if batch.status != "running":
        return None
    factor = encode_factor()
    remaining = 0.0
    current_frac = 0.0
    encoded_done = 0
    for item in batch.items:
        if item.status == "done":
            encoded_done += 1
            continue
        if item.status in ("skipped", "failed"):
            continue
        dur = max(0.1, float(item.duration or 30.0))
        if item.status == "encoding":
            left = max(0.0, dur - float(item.progress or 0.0))
            remaining += left * factor
            current_frac = min(1.0, float(item.progress or 0.0) / dur)
        else:
            remaining += dur * factor
    total = max(1, len(batch.items))
    done = sum(1 for i in batch.items if i.status in ("done", "skipped", "failed"))
    elapsed = max(0.0, time.time() - (batch.started_at or time.time()))
    effective = done + current_frac
    if encoded_done >= 1 and effective > 0:
        remaining = (elapsed / effective) * max(0.0, total - effective)
    return int(round(max(0.0, remaining)))


def batch_state(batch: Batch) -> dict:
    # Durable snapshots (container sleep / new process) are dict-backed.
    if hasattr(batch, "as_dict") and not hasattr(batch, "items"):
        data = batch.as_dict()
        data.setdefault("ok", True)
        data.setdefault("kind", "batch")
        return data

    total = len(batch.items)
    done = sum(1 for i in batch.items if i.status in ("done", "skipped", "failed"))
    current = next((i for i in batch.items if i.status == "encoding"), None)
    out_dir = Path(batch.output_dir)
    out_root = user_output_dir(batch.owner_uid)
    qpos = job_runtime.batches.queue_position(batch)

    def _out_label(path_str: str | None) -> str | None:
        if not path_str:
            return None
        p = Path(path_str)
        try:
            return p.relative_to(out_root).as_posix()
        except ValueError:
            return p.name

    state = {
        "id": batch.id,
        "status": batch.status,
        "queuePosition": qpos,
        "queuedAhead": (qpos - 1) if qpos else 0,
        "total": total,
        "completed": done,
        "outputFolder": out_dir.name,
        "outputDir": batch.date_label,
        "startedAt": batch.started_at,
        "etaSeconds": _batch_eta_seconds(batch),
        "kind": "batch",
        "current": (
            {
                "video": Path(current.video_path).name,
                "caption": current.caption[:60],
                "progressSeconds": round(current.progress, 1),
                "durationSeconds": round(current.duration, 1),
            }
            if current
            else None
        ),
        "items": [
            {
                "video": Path(i.video_path).name,
                "caption": i.caption[:80],
                "status": i.status,
                "output": _out_label(i.output),
                "error": i.error,
            }
            for i in batch.items
        ],
    }
    try:
        job_runtime.batches.persist(batch, state=state)
    except Exception:
        pass
    return state
