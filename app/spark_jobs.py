"""Background jobs: fetch TikTok Spark codes via DuoPlus for plan rows."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import catalog
from . import chat_store
from . import duoplus as dp
from . import job_runtime
from .duoplus import DuoPlusError, format_duoplus_user_error

_log = logging.getLogger(__name__)


@dataclass
class SparkItem:
    source_id: str
    caption: str
    output_rel: str = ""
    post_url: str = ""
    account_name: str = ""
    phone_name: str = ""
    spark_code: str = ""
    status: str = "pending"  # pending | running | done | failed | skipped | cancelled
    error: str | None = None
    task_id: str = ""
    diagnostics: dict = field(default_factory=dict)
    spark_source: str = ""
    spark_generated: bool = False
    index: int = 0


@dataclass
class SparkJob:
    id: str
    owner_uid: str
    thread_id: str
    plan_id: str = ""
    items: list[SparkItem] = field(default_factory=list)
    status: str = "queued"  # queued | running | done | cancelled
    cancel_flag: bool = False
    started_at: float = 0.0
    finished_at: float | None = None
    message: str | None = None
    current_phone: str = ""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pair_key(source_id: str, caption: str) -> tuple[str, str]:
    return (str(source_id or "").strip(), str(caption or "").strip())


def _saved_catalog_spark_code(uid: str, item: dict) -> str:
    """Return a real code already committed for a durable job item, if any."""
    try:
        output_rel = str(item.get("outputRel") or "").strip()
        row = catalog.get_by_output_rel(uid, output_rel) if output_rel else None
        if not row:
            row = catalog.find_render_by_pair(
                uid,
                str(item.get("sourceId") or ""),
                str(item.get("caption") or ""),
                output_rel=output_rel,
            )
        code = str((row or {}).get("tiktok_spark_code") or "").strip()
        post_url = str(item.get("postUrl") or (row or {}).get("tiktok_post_url") or "")
        return code if dp.looks_like_spark_code(code, post_url=post_url) else ""
    except Exception:
        return ""


def _persist_item(job: SparkJob, item: SparkItem) -> None:
    from . import tiktok_post_url as tpu

    current_row: dict = {}
    if not item.output_rel:
        current_row = catalog.find_render_by_pair(job.owner_uid, item.source_id, item.caption) or {}
        item.output_rel = str(current_row.get("output_rel") or "")
    elif item.output_rel:
        current_row = catalog.find_render_by_pair(job.owner_uid, item.source_id, item.caption) or {}
    if item.post_url:
        item.post_url = tpu.canonical_post_url(item.post_url, item.account_name or "")
    if item.spark_code:
        other = catalog.find_by_spark_code(job.owner_uid, item.spark_code)
        other_rel = str((other or {}).get("output_rel") or "")
        same_row = bool(item.output_rel) and other_rel == item.output_rel
        if not same_row and other and item.post_url:
            same_row = tpu.video_id_of(str(other.get("tiktok_post_url") or "")) == tpu.video_id_of(
                item.post_url
            )
        if other and not same_row:
            item.status = "failed"
            item.error = "duplicate spark code (stale clipboard)"
            item.spark_code = ""
        else:
            for sibling in job.items:
                if sibling is item:
                    continue
                if (sibling.spark_code or "").strip() == item.spark_code.strip():
                    item.status = "failed"
                    item.error = "duplicate spark code (stale clipboard)"
                    item.spark_code = ""
                    break
    if dp.is_spark_fail_sentinel(item.spark_code) or not dp.looks_like_spark_code(
        item.spark_code, post_url=item.post_url
    ):
        if item.spark_code:
            item.status = "failed"
            item.error = item.error or "spark_output is still fail"
            item.spark_code = ""
    # A retry can finish before an older multi-row job writes its final failed
    # snapshot.  Never let that stale failure erase the valid code the retry
    # already persisted.
    existing_code = str(current_row.get("tiktok_spark_code") or "").strip()
    if (
        not item.spark_code
        and item.status in ("failed", "cancelled")
        and dp.looks_like_spark_code(existing_code, post_url=item.post_url)
    ):
        item.spark_code = existing_code
        item.spark_generated = True
        item.status = "done"
        item.error = ""
    spark_error = item.error or ""
    if item.spark_code and item.status in ("skipped", "done"):
        spark_error = ""
    patch: dict = {
        "tiktok_spark_status": item.status,
        "tiktok_spark_error": spark_error,
        "tiktok_spark_task_id": item.task_id or "",
        "tiktok_spark_diagnostics": item.diagnostics,
    }
    if item.post_url:
        patch["tiktok_post_url"] = item.post_url
    catalog_status = item.status
    if item.spark_code and dp.looks_like_spark_code(item.spark_code, post_url=item.post_url):
        patch["tiktok_spark_code"] = item.spark_code
        item.spark_generated = True
        patch["tiktok_spark_generated"] = True
        patch["tiktok_spark_generated_at"] = _iso_now()
        catalog_status = "done"
        spark_error = ""
        patch["tiktok_spark_error"] = ""
    else:
        item.spark_generated = False
        patch["tiktok_spark_generated"] = False
        if catalog_status in ("failed", "cancelled"):
            patch["tiktok_spark_code"] = ""
    patch["tiktok_spark_status"] = catalog_status
    if catalog_status == "done":
        patch["tiktok_spark_fetched_at"] = _iso_now()
    if item.output_rel:
        try:
            catalog.update_publish(job.owner_uid, item.output_rel, patch)
        except Exception:
            _log.warning("catalog spark update failed", exc_info=True)
    try:
        chat_store.patch_plan_pair_spark(
            job.owner_uid,
            job.thread_id,
            source_id=item.source_id,
            caption=item.caption,
            spark_code=item.spark_code,
            spark_status=catalog_status,
            spark_error=spark_error,
            spark_fetched_at=patch.get("tiktok_spark_fetched_at") or "",
            tiktok_post_url=item.post_url,
            spark_generated=bool(item.spark_code),
        )
    except Exception:
        _log.warning("plan spark sync failed", exc_info=True)


def _cookie_path_for_account(uid: str, account_name: str):
    from . import tiktok_accounts as accounts

    want = (account_name or "").strip().lstrip("@").lower()
    if not want:
        return None
    for acct in accounts.list_accounts(uid):
        name = str(acct.get("name") or "").strip().lstrip("@").lower()
        if name == want:
            return accounts.get_cookie_file(uid, str(acct.get("id") or ""))
    return None


def _ensure_item_post_url(uid: str, item: SparkItem, *, job_ids: set[str] | None = None) -> str:
    from . import tiktok_post_url as tpu

    if item.post_url:
        item.post_url = tpu.canonical_post_url(item.post_url, item.account_name or "")
        return item.post_url
    exclude = catalog.posted_video_ids(uid, item.account_name or "")
    if job_ids:
        exclude |= set(job_ids)
    cookies = _cookie_path_for_account(uid, item.account_name)
    url = tpu.recover_post_url(
        item.account_name or "",
        caption=item.caption,
        exclude_ids=exclude,
        cookies_path=cookies,
        require_match=True,
        attempts=2,
        delay_s=1.0,
    )
    if url:
        item.post_url = url
        if item.output_rel:
            try:
                catalog.update_publish(uid, item.output_rel, {"tiktok_post_url": url})
            except Exception:
                _log.warning("catalog post url recover failed", exc_info=True)
    return item.post_url


def start_plan_spark(
    uid: str,
    thread_id: str,
    *,
    plan_id: str | None = None,
    pairs: list[dict] | None = None,
) -> SparkJob:
    if not dp.configured():
        raise DuoPlusError(
            "Set DUOPLUS_API_KEY (Automation → API in the DuoPlus console)."
        )
    checked = [
        p
        for p in (pairs or [])
        if isinstance(p, dict) and p.get("sourceId") and str(p.get("caption") or "").strip()
    ]
    if not checked:
        raise DuoPlusError("Select at least one plan row.")

    plan = chat_store.get_thread_plan(uid, thread_id, plan_id)
    resolved_plan_id = str(plan.get("id") or plan_id or "")
    # Check what items are requested vs what's already being processed
    requested_keys = {_pair_key(p.get("sourceId"), p.get("caption")) for p in checked}
    active = [
        job
        for job in job_runtime.sparks.values()
        if isinstance(job, SparkJob)
        and job.owner_uid == uid
        and job.plan_id == resolved_plan_id
        and job.status in ("queued", "running")
    ]
    # Only reuse existing job if it already covers the exact same items
    if active:
        active.sort(key=lambda job: (job.status != "running", job.started_at or 0))
        existing_job = active[0]
        existing_keys = {_pair_key(i.source_id, i.caption) for i in existing_job.items}
        # If same items, return existing job (duplicate click protection)
        if requested_keys == existing_keys:
            return existing_job
        # If new items requested, we need to create a new queued job for them
        # Don't return - let it create a new job below
    plan_pairs = [p for p in (plan.get("renderPairs") or []) if isinstance(p, dict)]
    batch_id = str(plan.get("batchId") or "").strip()
    row_map = catalog.lookup_renders_for_pairs(uid, plan_pairs, batch_id=batch_id)
    wanted = {_pair_key(p.get("sourceId"), p.get("caption")) for p in checked}

    items: list[SparkItem] = []
    for i, pair in enumerate(plan_pairs):
        key = _pair_key(pair.get("sourceId"), pair.get("caption"))
        if key not in wanted:
            continue
        row = row_map.get(key) or {}
        chat_store._apply_catalog_tiktok_to_pair(pair, row)
        post_url = str(
            pair.get("tiktokPostUrl") or row.get("tiktok_post_url") or ""
        ).strip()
        account = str(
            pair.get("tiktokAccountName")
            or row.get("tiktok_account_name")
            or plan.get("tiktokAccountName")
            or ""
        ).strip()
        output_rel = str(pair.get("outputRel") or row.get("output_rel") or "").strip()
        existing = str(pair.get("sparkCode") or row.get("tiktok_spark_code") or "").strip()
        if dp.is_spark_fail_sentinel(existing) or not dp.looks_like_spark_code(
            existing, post_url=post_url
        ):
            existing = ""
        on_tt = catalog.is_on_tiktok(row) or str(pair.get("tiktokStatus") or "") == "done"
        item = SparkItem(
            source_id=key[0],
            caption=key[1],
            output_rel=output_rel,
            post_url=post_url,
            account_name=account,
            spark_code=existing,
            spark_generated=bool(existing),
            index=i,
        )
        if existing:
            item.status = "skipped"
            item.error = "already_has_spark"
            item.spark_generated = True
        # Don't skip "not_posted" items at creation time - they stay pending
        # and will be re-evaluated at run time to allow uploads to complete first
        items.append(item)

    if not items:
        raise DuoPlusError("None of the selected rows matched the plan.")

    job = SparkJob(
        id=uuid.uuid4().hex[:12],
        owner_uid=uid,
        thread_id=thread_id,
        plan_id=resolved_plan_id,
        items=items,
    )
    job_runtime.sparks.submit(job, _run_job, name=f"spark-{job.id}")
    return job


def job_state(job: SparkJob) -> dict:
    if hasattr(job, "as_dict") and not hasattr(job, "items"):
        data = job.as_dict()
        # A restart can only update the durable job envelope. Without this
        # repair its old item records still say ``running`` even though the job
        # is terminal, and the client displays Fetching forever.
        status = str(data.get("status") or "")
        if status in ("failed", "cancelled", "done"):
            terminal_item_status = "cancelled" if status == "cancelled" else "failed"
            changed = False
            items: list[dict] = []
            for raw in data.get("items") or []:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                if str(item.get("status") or "") in ("pending", "queued", "running"):
                    # A result can be committed to the catalog just before the
                    # container is recycled, while the job envelope still says
                    # running. Never turn that real code into a restart failure.
                    saved_code = _saved_catalog_spark_code(str(data.get("ownerUid") or ""), item)
                    if saved_code:
                        item["status"] = "done"
                        item["error"] = ""
                        item["sparkGenerated"] = True
                        item["sparkCode"] = saved_code
                    else:
                        item["status"] = terminal_item_status
                        item["error"] = item.get("error") or data.get("message") or (
                            "Spark fetch interrupted" if status == "failed" else "cancelled"
                        )
                        item["sparkGenerated"] = False
                        item["sparkCode"] = ""
                    changed = True
                items.append(item)
            if changed:
                data["items"] = items
                # Persist the repaired snapshot so every later status request
                # agrees with this one, including after another restart.
                job._data = data
                job_runtime.sparks.persist(job, state=data)
        data.setdefault("ok", True)
        return data
    total = len(job.items)
    done = sum(1 for i in job.items if i.status in ("done", "failed", "skipped", "cancelled"))
    fetched = sum(1 for i in job.items if i.status == "done" and i.spark_code)
    failed = sum(1 for i in job.items if i.status == "failed")
    skipped = sum(1 for i in job.items if i.status == "skipped")
    current = next((i for i in job.items if i.status == "running"), None)
    state = {
        "id": job.id,
        # Cancellation is durable immediately, while a currently blocking RPA
        # call may take a moment to return to its worker. Report it as terminal
        # now so the browser stops polling instead of showing Fetching.
        "status": "cancelled" if job.cancel_flag else job.status,
        "threadId": job.thread_id,
        "planId": job.plan_id,
        "total": total,
        "completed": done,
        "fetched": fetched,
        "failed": failed,
        "skipped": skipped,
        "message": job.message,
        "currentPhone": job.current_phone,
        "current": (
            {
                "sourceId": current.source_id,
                "caption": current.caption,
                "status": current.status,
                "phoneName": current.phone_name,
            }
            if current
            else None
        ),
        "items": [
            {
                "sourceId": i.source_id,
                "caption": i.caption,
                "outputRel": i.output_rel,
                "postUrl": i.post_url,
                "accountName": i.account_name,
                "phoneName": i.phone_name,
                "sparkCode": i.spark_code,
                "sparkGenerated": i.spark_generated,
                "status": i.status,
                "error": i.error,
                "taskId": i.task_id,
                "diagnostics": i.diagnostics,
                "sparkSource": i.spark_source,
                "index": i.index,
            }
            for i in job.items
        ],
    }
    try:
        job_runtime.sparks.persist(job, state=state)
    except Exception:
        pass
    return state


def latest_for_thread(
    uid: str, thread_id: str, plan_id: str | None = None
) -> SparkJob | None:
    """Return the newest live job for a thread, optionally for one plan.

    A thread can contain more than one plan.  Status polling for Plan A must
    not expose Plan B's job simply because it started later.
    """
    wanted_plan_id = str(plan_id or "").strip()
    jobs = [
        j
        for j in job_runtime.sparks.values()
        if isinstance(j, SparkJob)
        and j.owner_uid == uid
        and j.thread_id == thread_id
        and (not wanted_plan_id or j.plan_id == wanted_plan_id)
    ]
    if not jobs:
        return None
    jobs.sort(key=lambda j: float(j.started_at or 0), reverse=True)
    return jobs[0]


def repair_terminal_snapshots(uid: str) -> int:
    """Reconcile stranded catalog rows from terminal durable Spark jobs.

    Jobs survive a process restart, while their Python workers do not.  The
    runtime marks the job envelope failed at boot, but historic item/catalog
    records could remain ``running``. Workspace plans enrich from catalog, so
    those rows otherwise displayed Fetching forever.
    """
    from . import userstore

    repaired = 0
    try:
        snapshots = userstore.list_docs(uid, "spark_jobs") or []
    except Exception:
        return repaired
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        status = str(snapshot.get("status") or "")
        if status not in ("failed", "cancelled", "done"):
            continue
        final_status = "cancelled" if status == "cancelled" else "failed"
        message = str(snapshot.get("message") or "Spark fetch interrupted")
        for raw in snapshot.get("items") or []:
            if not isinstance(raw, dict):
                continue
            # A previous boot may already have normalized the item snapshot to
            # failed, while the catalog still says running. Do not touch rows
            # with a real result or an intentional skip.
            if (
                str(raw.get("status") or "") in ("done", "skipped")
                or dp.looks_like_spark_code(str(raw.get("sparkCode") or ""))
                or _saved_catalog_spark_code(uid, raw)
            ):
                continue
            output_rel = str(raw.get("outputRel") or "").strip()
            if not output_rel:
                continue
            try:
                catalog.update_publish(
                    uid,
                    output_rel,
                    {
                        "tiktok_spark_status": final_status,
                        "tiktok_spark_error": str(raw.get("error") or message),
                        "tiktok_spark_code": "",
                        "tiktok_spark_generated": False,
                    },
                )
                repaired += 1
            except Exception:
                _log.warning("orphaned spark catalog repair failed", exc_info=True)
    return repaired


def cancel_job(uid: str, job_id: str) -> SparkJob | None:
    job = job_runtime.sparks.get(job_id, uid)
    if not isinstance(job, SparkJob):
        return None
    # This removes queued work immediately and durably records cancellation for
    # a running worker. Setting only the flag left an old queued snapshot live.
    job_runtime.sparks.cancel(job_id, uid)
    return job


def _run_job(job: SparkJob) -> None:
    uid = job.owner_uid
    run_error: str | None = None
    try:
        _run_job_inner(job)
    except Exception as exc:
        run_error = str(exc) or exc.__class__.__name__
        _log.exception("spark job %s crashed: %s", job.id, run_error)
    finally:
        # Never leave plan/catalog rows stuck on "running" after the worker exits
        # (crash, kill, or settle interrupt) — UI would spin forever.
        terminal = {"done", "failed", "skipped", "cancelled"}
        for item in job.items:
            if item.status in terminal:
                continue
            if job.cancel_flag:
                item.status = "cancelled"
                item.error = item.error or "cancelled"
            else:
                item.status = "failed"
                item.error = item.error or run_error or "spark job ended without a result"
            try:
                _persist_item(job, item)
            except Exception:
                _log.warning("spark item finalize failed", exc_info=True)
        if job.status in ("queued", "running") or not job.status:
            job.status = "cancelled" if job.cancel_flag else ("failed" if run_error else "done")
            job.finished_at = job.finished_at or time.time()
            if run_error and not job.message:
                job.message = run_error
        try:
            job_runtime.sparks.persist(job, state=job_state(job))
        except Exception:
            _log.warning("spark job persist on exit failed", exc_info=True)
        try:
            job_runtime.sparks.promote(uid)
        except Exception:
            _log.warning("spark promote failed", exc_info=True)


def _settle_if_needed(uid: str, item: SparkItem) -> None:
    # TikTok often hides "Ad settings" for a few minutes after upload; default
    # settle avoids DuoPlus RPA failing on a share sheet without that row.
    raw = (os.environ.get("SPARK_POST_SETTLE_SECONDS") or "180").strip()
    try:
        settle = max(0.0, float(raw))
    except ValueError:
        settle = 180.0
    if settle <= 0:
        return
    try:
        row = catalog.get_by_output_rel(uid, item.output_rel) if item.output_rel else None
        uploaded = str((row or {}).get("tiktok_uploaded_at") or "")
        if not uploaded:
            # No upload timestamp — still pause once per group start (caller).
            time.sleep(min(settle, 60.0))
            return
        ts = datetime.fromisoformat(uploaded.replace("Z", "+00:00")).timestamp()
        wait = settle - (time.time() - ts)
        if wait > 0:
            _log.info(
                "spark settle %.0fs for %s (post age %.0fs, need %.0fs)",
                wait,
                (item.post_url or "")[-24:],
                time.time() - ts,
                settle,
            )
            time.sleep(wait)
    except Exception:
        pass


def _wait_for_uploads(uid: str, job: SparkJob, items: list[SparkItem], *, max_wait_s: float = 600) -> None:
    """Wait for any active upload jobs to complete before evaluating spark items.
    
    This allows spark jobs to be queued while uploads are still running, and
    they will wait for the uploads to finish before checking post status.
    """
    import time as _time
    deadline = _time.time() + max_wait_s
    poll_interval = 5.0
    
    while _time.time() < deadline:
        if job.cancel_flag:
            return
        # Check if there are any active upload jobs for this user
        active_uploads = [
            j for j in job_runtime.uploads.values()
            if getattr(j, 'owner_uid', '') == uid
            and getattr(j, 'status', '') in ('queued', 'running')
            and not getattr(j, 'cancel_flag', False)
        ]
        if not active_uploads:
            return
        # Update job message to show we're waiting
        job.message = f"Waiting for {len(active_uploads)} upload(s) to complete..."
        try:
            job_runtime.sparks.persist(job)
        except Exception:
            pass
        _time.sleep(poll_interval)
    _log.warning("spark job %s timed out waiting for uploads after %.0fs", job.id, max_wait_s)


def _run_job_inner(job: SparkJob) -> None:
    work = [i for i in job.items if i.status == "pending"]
    skipped = [i for i in job.items if i.status == "skipped"]
    for item in skipped:
        _persist_item(job, item)

    # Wait for any active upload jobs to complete before checking post status
    uid = job.owner_uid
    _wait_for_uploads(uid, job, work)

    # Re-evaluate pending items now that uploads may have completed
    still_pending = []
    for item in work:
        if job.cancel_flag:
            break
        # Refresh catalog data
        row = catalog.get_by_output_rel(uid, item.output_rel) if item.output_rel else None
        if not row:
            row = catalog.find_render_by_pair(uid, item.source_id, item.caption) or {}
        on_tt = catalog.is_on_tiktok(row) or False
        if not on_tt:
            # Try to recover post URL one more time
            if not item.post_url:
                _ensure_item_post_url(uid, item)
            if not item.post_url:
                item.status = "skipped"
                item.error = "not_posted"
                _persist_item(job, item)
                continue
        # Update post_url from catalog if we didn't have it
        if not item.post_url:
            item.post_url = str(row.get("tiktok_post_url") or "").strip()
            if not item.post_url:
                _ensure_item_post_url(uid, item)
        if not item.post_url:
            item.status = "skipped"
            item.error = "not_posted"
            _persist_item(job, item)
            continue
        still_pending.append(item)
    work = still_pending

    if not work:
        job.message = "Nothing to fetch (already have codes or not posted)."
        job.status = "done"
        job.finished_at = time.time()
        return

    groups: dict[str, list[SparkItem]] = {}
    order: list[str] = []
    for item in work:
        try:
            phone = dp.resolve_phone_for_account(item.account_name)
            pname = str(phone.get("name") or "")
            item.phone_name = pname
            key = pname or str(phone.get("id") or "")
        except DuoPlusError as exc:
            item.status = "failed"
            item.error = format_duoplus_user_error(exc)
            _persist_item(job, item)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    for pname in order:
        if job.cancel_flag:
            break
        group = groups[pname]
        job.current_phone = pname
        job.message = f"Powering on {pname}…"
        try:
            phone = dp.find_phone(name=pname)
        except DuoPlusError as exc:
            for item in group:
                item.status = "failed"
                item.error = format_duoplus_user_error(exc)
                _persist_item(job, item)
            continue

        # Keep the queue honest: exactly one video moves from pending to
        # running at a time. A result is persisted before the phone starts the
        # next video, so a slow first attempt cannot make the other nine look
        # active or hide which one failed.
        pending_group = [it for it in group if it.status == "pending"]
        if not pending_group:
            continue
        job.message = f"{pname}: fetching spark"
        _settle_if_needed(job.owner_uid, pending_group[0])
        payload = [
            {
                "sourceId": it.source_id,
                "caption": it.caption,
                "postUrl": it.post_url,
                "accountName": it.account_name,
                "taskName": f"spark-{job.id}-{it.index}-{int(time.time())}",
            }
            for it in pending_group
        ]

        def _cancel() -> bool:
            return bool(job.cancel_flag)

        def _started(row: dict) -> None:
            key = _pair_key(row.get("sourceId"), row.get("caption"))
            item = next(
                (candidate for candidate in pending_group if _pair_key(candidate.source_id, candidate.caption) == key),
                None,
            )
            if item is None or job.cancel_flag:
                return
            item.status = "running"
            item.error = None
            job.message = f"{pname}: fetching spark {item.index + 1}/{len(job.items)} (at least 2 retries)"
            _persist_item(job, item)
            job_runtime.sparks.persist(job)

        def _progress(row: dict) -> None:
            """Persist each phone result as it arrives, not after the group."""
            key = _pair_key(row.get("sourceId"), row.get("caption"))
            item = next(
                (candidate for candidate in pending_group if _pair_key(candidate.source_id, candidate.caption) == key),
                None,
            )
            if item is None:
                return
            job.message = f"{pname}: processed {item.index + 1}/{len(job.items)} sparks"
            item.task_id = str(row.get("taskId") or item.task_id or "")
            item.diagnostics = dict(row.get("diagnostics") or {})
            code = str(row.get("sparkCode") or "")
            if row.get("ok") and dp.looks_like_spark_code(code, post_url=item.post_url):
                item.status = "done"
                item.spark_code = code
                item.task_id = str(row.get("taskId") or "")
                item.spark_source = str(row.get("sparkSource") or "")
                item.error = None
                item.spark_generated = True
            else:
                item.status = "cancelled" if job.cancel_flag else str(row.get("status") or "failed")
                item.error = format_duoplus_user_error(str(row.get("error") or "Spark fetch failed"))
                item.spark_code = ""
                item.spark_generated = False
            _persist_item(job, item)
            job_runtime.sparks.persist(job)

        try:
            results = dp.run_spark_phone_group(
                payload,
                phone=phone,
                # Shutdown belongs to the queue's idle watcher, not this batch.
                power_off_after=False,
                cancel_check=_cancel,
                on_start=_started,
                on_result=_progress,
            )
        except DuoPlusError as exc:
            friendly = format_duoplus_user_error(exc)
            for it in group:
                if it.status not in ("done", "skipped"):
                    it.status = "cancelled" if job.cancel_flag else "failed"
                    it.error = friendly
                    it.task_id = str(exc.details.get("taskId") or it.task_id or "")
                    it.diagnostics = dict(exc.details)
                    _persist_item(job, it)
            # Group cleanup runs while phone ownership is still held.
            continue
        finally:
            from . import spark_phone_idle

            spark_phone_idle.schedule(str(phone["id"]))

        by_key = {_pair_key(r.get("sourceId"), r.get("caption")): r for r in results}
        for it in group:
            row = by_key.get(_pair_key(it.source_id, it.caption)) or {}
            it.task_id = str(row.get("taskId") or it.task_id or "")
            it.diagnostics = dict(row.get("diagnostics") or it.diagnostics or {})
            if it.status == "cancelled":
                continue
            if row.get("ok") and dp.looks_like_spark_code(
                str(row.get("sparkCode") or ""), post_url=it.post_url
            ):
                it.status = "done"
                it.spark_code = str(row.get("sparkCode") or "")
                it.task_id = str(row.get("taskId") or "")
                it.spark_source = str(row.get("sparkSource") or "")
                it.post_url = str(row.get("postUrl") or it.post_url)
                it.spark_generated = True
                it.error = None
            else:
                err = str(row.get("error") or "Spark fetch failed")
                if str(row.get("status") or "") == "skipped" and "already_has_spark" in err:
                    it.status = "skipped"
                    it.error = "already_has_spark"
                else:
                    it.status = "cancelled" if job.cancel_flag else "failed"
                    it.error = format_duoplus_user_error(err)
                    it.spark_code = ""
                    it.spark_generated = False
            _persist_item(job, it)

    # Phone cleanup belongs to run_spark_phone_group while it holds ownership.

    job.message = None
    job.status = "cancelled" if job.cancel_flag else "done"
    job.finished_at = time.time()
    job.current_phone = ""
