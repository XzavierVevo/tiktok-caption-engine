"""In-process background job registry shared by batch, TikTok, and Facebook.

Supports a per-uid FIFO queue with a concurrency cap: jobs are registered as
``queued`` until a slot opens, then a daemon thread is started. Promote after
finish/cancel so the next queued job runs.

Snapshots are also written to userstore so Worker/MCP can poll after sleep.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

_log = logging.getLogger(__name__)


class _SnapshotJob:
    """Dict-backed stand-in when the live job object is gone (container restart)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)
        self.id = str(data.get("id") or "")
        self.owner_uid = str(data.get("ownerUid") or data.get("owner_uid") or "")
        self.status = str(data.get("status") or "")
        self.cancel_flag = bool(data.get("cancel_flag") or False)
        self.started_at = data.get("startedAt") or data.get("started_at")
        self.finished_at = data.get("finishedAt") or data.get("finished_at")
        self.message = data.get("message") or ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


def _load_snapshot(uid: str, collection: str, job_id: str) -> dict[str, Any] | None:
    try:
        from . import userstore

        data = userstore.get_doc(uid, collection, job_id)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class JobRuntime:
    def __init__(self, max_concurrent: int = 1, *, collection: str = "jobs") -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self._collection = collection
        self._jobs: dict[str, Any] = {}
        self._queues: dict[str, list[str]] = {}
        self._pending: dict[str, tuple[Callable, tuple, str | None]] = {}
        self._lock = threading.Lock()

    def register(self, job: Any) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def start(
        self,
        job: Any,
        target: Callable,
        *,
        args: tuple | None = None,
        name: str | None = None,
    ) -> Any:
        """Legacy immediate start (bypasses queue). Prefer ``submit``."""
        self.register(job)
        if getattr(job, "status", None) == "queued":
            job.status = "running"
        if not getattr(job, "started_at", None):
            try:
                job.started_at = time.time()
            except Exception:
                pass
        threading.Thread(
            target=target,
            args=args if args is not None else (job,),
            daemon=True,
            name=name,
        ).start()
        return job

    def submit(
        self,
        job: Any,
        target: Callable,
        *,
        args: tuple | None = None,
        name: str | None = None,
    ) -> Any:
        """Register job; start now if under cap, otherwise queue FIFO per uid."""
        uid = str(getattr(job, "owner_uid", "") or "")
        call_args = args if args is not None else (job,)
        start_now = False
        with self._lock:
            self._jobs[job.id] = job
            running = self._running_count_unlocked(uid)
            if running < self.max_concurrent:
                job.status = "running"
                if not getattr(job, "started_at", None):
                    job.started_at = time.time()
                start_now = True
            else:
                job.status = "queued"
                q = self._queues.setdefault(uid, [])
                if job.id not in q:
                    q.append(job.id)
                self._pending[job.id] = (target, call_args, name)
        self.persist(job)
        if start_now:
            threading.Thread(
                target=target,
                args=call_args,
                daemon=True,
                name=name,
            ).start()
        return job

    def promote(self, uid: str) -> Any | None:
        """Start the next queued job for uid if a slot is free. Returns job or None."""
        uid = str(uid or "")
        to_start: tuple[Any, Callable, tuple, str | None] | None = None
        with self._lock:
            if self._running_count_unlocked(uid) >= self.max_concurrent:
                return None
            q = self._queues.get(uid) or []
            while q:
                jid = q[0]
                job = self._jobs.get(jid)
                pending = self._pending.get(jid)
                if not job or getattr(job, "status", "") != "queued" or not pending:
                    q.pop(0)
                    self._pending.pop(jid, None)
                    continue
                q.pop(0)
                target, call_args, name = pending
                self._pending.pop(jid, None)
                job.status = "running"
                job.started_at = time.time()
                to_start = (job, target, call_args, name)
                break
            if not q and uid in self._queues:
                self._queues.pop(uid, None)
        if not to_start:
            return None
        job, target, call_args, name = to_start

        # Try to persist promoted job with rollback on failure
        try:
            self.persist(job)
        except Exception as persist_exc:
            _log.error("Failed to persist promoted job %s: %s", getattr(job, "id", "?"), persist_exc)

            # ROLLBACK: Restore job to queued state
            with self._lock:
                job.status = "queued"
                job.started_at = None

                # Put job back at front of queue
                if uid not in self._queues:
                    self._queues[uid] = []
                self._queues[uid].insert(0, getattr(job, "id", None))

                # Restore pending entry
                self._pending[getattr(job, "id", None)] = (target, call_args, name)

            # Try to persist rollback state
            try:
                self.persist(job)
            except Exception as rollback_exc:
                _log.critical(
                    "Failed to persist rollback for job %s: %s. Job may be in inconsistent state.",
                    getattr(job, "id", "?"),
                    rollback_exc
                )

            # Re-raise original persist exception
            raise persist_exc

        # Persist succeeded, start the thread
        threading.Thread(
            target=target,
            args=call_args,
            daemon=True,
            name=name,
        ).start()
        return job

    def get(self, job_id: str, uid: str | None = None) -> Any | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job and uid is not None and getattr(job, "owner_uid", None) != uid:
            return None
        if job is not None:
            return job
        if uid and job_id:
            snap = _load_snapshot(uid, self._collection, job_id)
            if snap:
                return _SnapshotJob(snap)
        return None

    def persist(self, job: Any, *, state: dict | None = None) -> None:
        """Write a durable snapshot so Worker/MCP can poll after container sleep.

        Always **merges** into any existing snapshot so a status-only write
        (submit/promote/cancel) cannot wipe ``items`` / ``cancel_flag`` / extras.

        ``cancel_flag`` is sticky: once set on disk (or on the live object), a
        stale runner ``_persist`` with ``cancel_flag=False`` must not revive the
        job as ``running`` (that caused Cancel → Failed to fetch races and
        zombie warm-ups after restart).
        """
        uid = str(getattr(job, "owner_uid", "") or "")
        jid = str(getattr(job, "id", "") or "")
        if not uid or not jid:
            return
        existing = _load_snapshot(uid, self._collection, jid) or {}
        if not isinstance(existing, dict):
            existing = {}
        if isinstance(job, _SnapshotJob):
            payload = {**existing, **job.as_dict()}
        elif state is not None:
            payload = {**existing, **dict(state)}
        else:
            # Sparse update: keep prior fields, only refresh live status bits.
            payload = dict(existing)
        payload.setdefault("id", jid)
        payload.setdefault("ownerUid", uid)
        payload["updatedAt"] = time.time()
        if getattr(job, "started_at", None):
            payload["startedAt"] = job.started_at
        if getattr(job, "finished_at", None):
            payload["finishedAt"] = job.finished_at

        existing_cancel = bool(existing.get("cancel_flag"))
        live_cancel = (
            bool(getattr(job, "cancel_flag", False))
            if hasattr(job, "cancel_flag")
            else False
        )
        # State dict from job_state() also carries cancel_flag — honor it.
        state_cancel = bool((state or {}).get("cancel_flag")) if state else False
        cancel = existing_cancel or live_cancel or state_cancel
        payload["cancel_flag"] = cancel
        if cancel:
            payload["status"] = "cancelled"
            items = payload.get("items")
            if isinstance(items, list):
                coerced = []
                for it in items:
                    if not isinstance(it, dict):
                        coerced.append(it)
                        continue
                    st = str(it.get("status") or "")
                    if st not in ("done", "failed", "cancelled"):
                        it = dict(it)
                        it["status"] = "cancelled"
                    coerced.append(it)
                payload["items"] = coerced
            if hasattr(job, "items"):
                for it in getattr(job, "items", None) or []:
                    st = str(getattr(it, "status", "") or "")
                    if st not in ("done", "failed", "cancelled"):
                        try:
                            it.status = "cancelled"
                        except Exception:
                            pass
            # Keep the cancel message; don't let a stale runner rewrite it.
            if existing_cancel and existing.get("message"):
                payload["message"] = existing.get("message")
            elif getattr(job, "message", None):
                payload["message"] = job.message
            else:
                payload["message"] = "cancelled"
            if hasattr(job, "cancel_flag"):
                try:
                    job.cancel_flag = True
                except Exception:
                    pass
            if hasattr(job, "status"):
                try:
                    if str(getattr(job, "status", "") or "") in (
                        "queued",
                        "running",
                        "pending",
                    ):
                        job.status = "cancelled"
                except Exception:
                    pass
            if not payload.get("finishedAt"):
                try:
                    finished = getattr(job, "finished_at", None) or time.time()
                    payload["finishedAt"] = finished
                    if hasattr(job, "finished_at") and not job.finished_at:
                        job.finished_at = finished
                except Exception:
                    payload["finishedAt"] = time.time()
        else:
            payload["status"] = getattr(job, "status", payload.get("status") or "")
            if getattr(job, "message", None) is not None:
                payload["message"] = job.message
        try:
            from . import userstore

            userstore.set_doc(uid, self._collection, jid, payload)
        except Exception:
            _log.warning(
                "job persist failed collection=%s id=%s",
                self._collection,
                jid,
                exc_info=True,
            )
            raise
        st = str(payload.get("status") or "")
        if st in ("done", "cancelled", "failed"):
            self._prune_finished()

    def cancel(self, job_id: str, uid: str | None = None) -> bool:
        job = self.get(job_id, uid)
        if not job:
            return False
        owner = str(getattr(job, "owner_uid", "") or "")
        status = getattr(job, "status", "")
        if status == "queued":
            with self._lock:
                q = self._queues.get(owner) or []
                if job_id in q:
                    q.remove(job_id)
                if not q:
                    self._queues.pop(owner, None)
                self._pending.pop(job_id, None)
                job.status = "cancelled"
                job.cancel_flag = True
                try:
                    job.finished_at = time.time()
                except Exception:
                    pass
            self.persist(job)
            self.promote(owner)
            return True
        job.cancel_flag = True
        self.persist(job)
        return True

    def latest(self, uid: str) -> Any | None:
        with self._lock:
            mine = [j for j in self._jobs.values() if getattr(j, "owner_uid", None) == uid]
        if not mine:
            return None
        return max(mine, key=lambda j: getattr(j, "started_at", 0.0) or 0.0)

    def running_for(self, uid: str) -> Any | None:
        with self._lock:
            for job in self._jobs.values():
                if getattr(job, "owner_uid", None) == uid and getattr(job, "status", "") == "running":
                    return job
        return None

    def has_running(self, uid: str) -> bool:
        return self.running_for(uid) is not None

    def running_count(self, uid: str) -> int:
        with self._lock:
            return self._running_count_unlocked(uid)

    def _running_count_unlocked(self, uid: str) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if getattr(j, "owner_uid", None) == uid and getattr(j, "status", "") == "running"
        )

    def queue_position(self, job: Any) -> int | None:
        """1-based position in uid queue, or None if not queued."""
        if getattr(job, "status", "") != "queued":
            return None
        uid = str(getattr(job, "owner_uid", "") or "")
        jid = getattr(job, "id", None)
        with self._lock:
            q = self._queues.get(uid) or []
            try:
                return q.index(jid) + 1
            except ValueError:
                return None

    def list_for(
        self,
        uid: str,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> list[Any]:
        want = statuses or ("running", "queued")
        with self._lock:
            mine = [
                j
                for j in self._jobs.values()
                if getattr(j, "owner_uid", None) == uid
                and getattr(j, "status", "") in want
            ]
        q = list(self._queues.get(uid) or [])
        q_index = {jid: i for i, jid in enumerate(q)}

        def _sort_key(j: Any) -> tuple:
            st = getattr(j, "status", "")
            if st == "running":
                return (0, getattr(j, "started_at", 0.0) or 0.0)
            if st == "queued":
                return (1, float(q_index.get(j.id, 9999)))
            return (2, -(getattr(j, "started_at", 0.0) or 0.0))

        return sorted(mine, key=_sort_key)

    def values(self) -> list[Any]:
        with self._lock:
            return list(self._jobs.values())

    def _prune_finished(self, keep: int = 40) -> None:
        with self._lock:
            finished = [
                j
                for j in self._jobs.values()
                if str(getattr(j, "status", "") or "") in ("done", "cancelled", "failed")
            ]
            if len(finished) <= keep:
                return
            finished.sort(key=lambda j: float(getattr(j, "finished_at", 0) or getattr(j, "started_at", 0) or 0))
            for j in finished[:-keep]:
                jid = getattr(j, "id", None)
                if jid:
                    self._jobs.pop(jid, None)
                    self._pending.pop(jid, None)

    def fail_orphaned(
        self,
        uids: list[str] | None = None,
        *,
        message: str = "interrupted by restart",
    ) -> int:
        """Mark durable running/queued snapshots failed when no live thread exists."""
        from . import userstore
        from .config import BASE_DIR

        found: list[str] = [str(u) for u in (uids or []) if str(u).strip()]
        if not found:
            try:
                from .provision.owner import owner_uid as resolve_owner

                ou = str(resolve_owner() or "").strip()
                if ou:
                    found.append(ou)
            except Exception:
                pass
            try:
                root = BASE_DIR / "data" / "users"
                if root.is_dir():
                    for path in root.iterdir():
                        if path.is_dir() and path.name not in found:
                            found.append(path.name)
            except Exception:
                pass
        marked = 0
        now = time.time()
        for uid in found:
            uid = str(uid or "").strip()
            if not uid or uid.startswith("_"):
                continue
            try:
                docs = userstore.list_docs(uid, self._collection) or []
            except Exception:
                continue
            for raw in docs:
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("status") or "") not in ("running", "queued"):
                    continue
                jid = str(raw.get("id") or "")
                if not jid:
                    continue
                with self._lock:
                    live = self._jobs.get(jid)
                if live is not None and str(getattr(live, "status", "") or "") in (
                    "running",
                    "queued",
                ):
                    continue
                snap = _SnapshotJob(raw)
                snap.status = "failed"
                snap.message = message
                snap.finished_at = now
                snap._data["status"] = "failed"
                snap._data["message"] = message
                snap._data["finishedAt"] = now
                self.persist(snap)
                marked += 1
        return marked


def _caps_from_config() -> tuple[int, int]:
    try:
        from .config import (
            MAX_CONCURRENT_BATCHES_PER_USER,
            MAX_CONCURRENT_UPLOADS_PER_USER,
        )

        return (
            int(MAX_CONCURRENT_BATCHES_PER_USER),
            int(MAX_CONCURRENT_UPLOADS_PER_USER),
        )
    except Exception:
        return (2, 1)


_batch_cap, _upload_cap = _caps_from_config()
batches = JobRuntime(max_concurrent=_batch_cap, collection="batch_jobs")
uploads = JobRuntime(max_concurrent=_upload_cap, collection="upload_jobs")
facebook = JobRuntime(max_concurrent=1, collection="facebook_jobs")
sparks = JobRuntime(max_concurrent=1, collection="spark_jobs")
# Job threads may wait on remote browser steps for a long time.  The separate
# AdsPower resource controls active browser work; this is only an API thread cap.
provisions = JobRuntime(max_concurrent=10, collection="provision_jobs")
