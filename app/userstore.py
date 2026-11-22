"""Single JSON document store: memory cache + one durable write.

Cloudflare D1 is the source of truth for all application documents (including
BC / provision_*). Local ``data/users/{uid}/_fs/...`` is a post-commit mirror
for cold starts and degraded reads. R2 retains large JSON spills and media.

Legacy ``user_root/*.json`` files are read once on miss and copied into the
canonical slot. They are not deleted.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock

from . import firebase_store as fstore
from . import cloudflare_store as cfstore
from . import storage
from .state_crypto import decrypt_document, encrypt_document
from .config import (
    user_accounts_path,
    user_chat_index_path,
    user_chat_thread_path,
    user_facebook_path,
    user_hooks_history_path,
    user_hooks_offers_path,
    user_manifest_path,
    user_root,
    user_session_path,
    user_settings_path,
    user_usage_path,
    user_workspace_path,
)

_log = logging.getLogger(__name__)

Mutator = Callable[[dict[str, Any]], dict[str, Any] | None]

_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
_cache_lock = threading.Lock()
# Negative lookups (Firestore/R2 miss) — without this, every BC inventory build
# re-pays a multi-second network round-trip for missing KV docs.
_neg_cache: dict[tuple[str, str, str], float] = {}
_NEG_TTL = 60.0
_CACHE_MAX = 400
_LOCKS_MAX = 400
_locks: dict[tuple[str, str, str], threading.RLock] = {}
_lock_used: dict[tuple[str, str, str], float] = {}
_locks_guard = threading.Lock()
_migrated: set[tuple[str, str, str]] = set()
_list_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_LIST_TTL = 8.0
_bg_refresh: set[tuple[str, str, str]] = set()
_bg_refresh_lock = threading.Lock()
_CORRUPT = object()
# Coalesce background Firestore writes: one in-flight per doc, latest payload wins.
_fs_pending: dict[tuple[str, str, str], dict[str, Any]] = {}
_fs_inflight: set[tuple[str, str, str]] = set()
_fs_failed: dict[tuple[str, str, str], dict[str, Any]] = {}
_fs_sched_lock = threading.Lock()
_cf_failed: dict[tuple[str, str, str], dict[str, Any]] = {}


def _primary() -> str:
    value = (os.environ.get("USERSTORE_PRIMARY") or "firestore").strip().lower()
    return value if value in {"firestore", "d1"} else "firestore"


def _dual_write() -> bool:
    return (os.environ.get("USERSTORE_DUAL_WRITE") or "0").strip() == "1"


def _shadow_read() -> bool:
    return (os.environ.get("USERSTORE_SHADOW_READ") or "0").strip() == "1"


def store_status() -> dict[str, Any]:
    return {
        "primary": _primary(),
        "dualWrite": _dual_write(),
        "shadowRead": _shadow_read(),
        "cloudflare": cfstore.health(),
        "replicationBacklog": len(_cf_failed),
    }


def storage_is_healthy() -> bool:
    """Return whether the selected persistence path can serve reads."""
    if _primary() == "d1":
        # An intentionally unconfigured cloud backend means this process is
        # using the durable local mirror.  It is not a cloud outage.
        if not cfstore.enabled():
            return True
        health = cfstore.health()
        # lastOk is None if no call has been made, True if last call succeeded
        return health.get("lastOk") is not False
    # Firestore is rollback-only after the D1 cutover.  Local development
    # commonly leaves it disabled and must still be able to create/read docs.
    return True


def last_storage_error() -> str:
    """Return the last storage error message, or empty string if none."""
    if _primary() == "d1":
        return cfstore.health().get("lastError") or ""
    return ""


LEGACY_KV: dict[str, Callable[[str], Path]] = {
    "session": user_session_path,
    "workspace": user_workspace_path,
    "usage": user_usage_path,
    "tiktok_accounts": user_accounts_path,
    "tiktok_settings": user_settings_path,
    "facebook_accounts": user_facebook_path,
    "chat_index": user_chat_index_path,
    "hooks_offers": user_hooks_offers_path,
    "hooks_history": user_hooks_history_path,
    "rendered_manifest": user_manifest_path,
}

# Derived from LEGACY_KV so the path table cannot drift from the migrators.
_KV_REL_OVERRIDE = {
    "chat_index": "chats/index.json",
    "rendered_manifest": "rendered.json",
}
PATH_KV: dict[str, tuple[str, str]] = {
    _KV_REL_OVERRIDE.get(doc_id, f"{doc_id}.json"): ("kv", doc_id)
    for doc_id in LEGACY_KV
}


def _ckey(uid: str, collection: str, doc_id: str) -> tuple[str, str, str]:
    return (uid, fstore.safe_id(collection), fstore.safe_id(doc_id))


def _lock_for(uid: str, collection: str, doc_id: str) -> threading.RLock:
    key = _ckey(uid, collection, doc_id)
    now = time.monotonic()
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        _lock_used[key] = now
        extra = len(_locks) - _LOCKS_MAX
        if extra > 0:
            idle = sorted(
                (k for k in _locks if k != key),
                key=lambda k: _lock_used.get(k, 0.0),
            )
            for dead in idle[:extra]:
                old = _locks.get(dead)
                if old is None:
                    continue
                if old.acquire(blocking=False):
                    _locks.pop(dead, None)
                    _lock_used.pop(dead, None)
                    old.release()
        return lock


def local_path(uid: str, collection: str, doc_id: str) -> Path:
    p = (
        user_root(uid)
        / "_fs"
        / fstore.safe_id(collection)
        / f"{fstore.safe_id(doc_id)}.json"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cache_get(uid: str, collection: str, doc_id: str) -> dict[str, Any] | None:
    with _cache_lock:
        hit = _cache.get(_ckey(uid, collection, doc_id))
        return dict(hit) if isinstance(hit, dict) else None


def _cache_put(uid: str, collection: str, doc_id: str, data: dict[str, Any]) -> None:
    key = _ckey(uid, collection, doc_id)
    with _cache_lock:
        _cache[key] = dict(data)
        _neg_cache.pop(key, None)
        extra = len(_cache) - _CACHE_MAX
        if extra > 0:
            for dead in list(_cache.keys())[:extra]:
                if dead != key:
                    _cache.pop(dead, None)


def _cache_pop(uid: str, collection: str, doc_id: str) -> None:
    key = _ckey(uid, collection, doc_id)
    with _cache_lock:
        _cache.pop(key, None)
        _neg_cache.pop(key, None)


def _neg_hit(uid: str, collection: str, doc_id: str) -> bool:
    key = _ckey(uid, collection, doc_id)
    with _cache_lock:
        exp = _neg_cache.get(key)
        if exp is None:
            return False
        if time.monotonic() >= exp:
            _neg_cache.pop(key, None)
            return False
        return True


def _neg_put(uid: str, collection: str, doc_id: str) -> None:
    key = _ckey(uid, collection, doc_id)
    with _cache_lock:
        _neg_cache[key] = time.monotonic() + _NEG_TTL


def _read_json_file(path: Path) -> dict[str, Any] | object | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError:
        _log.error("corrupt json %s", path)
        return _CORRUPT
    return data if isinstance(data, dict) else _CORRUPT


def _write_local(uid: str, collection: str, doc_id: str, data: dict[str, Any]) -> Path:
    path = local_path(uid, collection, doc_id)
    protected = encrypt_document(uid, collection, doc_id, data)
    storage.atomic_write_json(path, protected, indent=2)
    storage.mirror_up_async(uid, path)
    return path


def _spill_blob_path(uid: str, collection: str, doc_id: str) -> Path:
    return (
        user_root(uid)
        / "_fs"
        / "_blobs"
        / f"{fstore.safe_id(collection)}_{fstore.safe_id(doc_id)}.json"
    )


def _cleanup_spill_blob(uid: str, collection: str, doc_id: str) -> None:
    path = _spill_blob_path(uid, collection, doc_id)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
    try:
        storage.mirror_delete(uid, storage.rel_of(uid, path))
    except Exception:
        pass


def _legacy_path(uid: str, collection: str, doc_id: str) -> Path | None:
    if collection == "kv":
        fn = LEGACY_KV.get(doc_id)
        return fn(uid) if fn else None
    if collection == "chats":
        return user_chat_thread_path(uid, doc_id)
    return None


def _migrate_legacy(uid: str, collection: str, doc_id: str) -> dict[str, Any] | None:
    key = _ckey(uid, collection, doc_id)
    if key in _migrated:
        return None
    _migrated.add(key)
    path = _legacy_path(uid, collection, doc_id)
    if path is None:
        return None
    if not path.is_file() and storage.ENABLED:
        try:
            storage.ensure_local(uid, storage.rel_of(uid, path))
        except ValueError:
            pass
    data = _read_json_file(path)
    if data is _CORRUPT or not isinstance(data, dict):
        return None
    _write_local(uid, collection, doc_id, data)
    if fstore.enabled():
        _schedule_firestore_set(uid, collection, doc_id, data)
    _cache_put(uid, collection, doc_id, data)
    return dict(data)


def _schedule_firestore_refresh(uid: str, collection: str, doc_id: str) -> None:
    """Best-effort background refresh so local-first reads stay eventually consistent."""
    key = _ckey(uid, collection, doc_id)
    with _bg_refresh_lock:
        if key in _bg_refresh:
            return
        _bg_refresh.add(key)

    def _run() -> None:
        try:
            remote = fstore.firestore_get(uid, collection, doc_id)
            if remote is fstore.FS_ERROR or not isinstance(remote, dict):
                return
            data = remote
            if remote.get("storage") == "r2" and remote.get("rel"):
                blob = fstore.load_r2_blob(uid, str(remote["rel"]), collection, doc_id)
                if isinstance(blob, dict):
                    data = blob
            remote_cards_empty = (
                collection == "kv"
                and doc_id == "provision_finance"
                and not _finance_cards_complete(remote.get("cards"))
            )
            with _lock_for(uid, collection, doc_id):
                local = _read_local_doc(uid, collection, doc_id)
                data = _merge_doc(
                    collection,
                    doc_id,
                    local if isinstance(local, dict) else None,
                    data,
                )
                local2 = _read_local_doc(uid, collection, doc_id)
                if isinstance(local2, dict):
                    data = _merge_doc(collection, doc_id, local2, data)
                _cache_put(uid, collection, doc_id, data)
                try:
                    _write_local(uid, collection, doc_id, data)
                except Exception:
                    pass
            if (
                remote_cards_empty
                and isinstance(data, dict)
                and _finance_cards_complete(data.get("cards"))
            ):
                _schedule_firestore_set(uid, collection, doc_id, data)
        except Exception as exc:
            _log.debug("bg firestore refresh %s/%s failed: %s", collection, doc_id, exc)
        finally:
            with _bg_refresh_lock:
                _bg_refresh.discard(key)

    threading.Thread(target=_run, name="us-refresh", daemon=True).start()


def _row_updated_at(row: dict[str, Any]) -> str:
    """Newest timestamp on a byId row.

    Profile rows use ``updatedAt``. Worker steps often only have
    ``createdAt`` / ``claimedAt`` / ``finishedAt`` — treating those as empty
    made every stale full-document write win (``"" >= ""``) and drop a
    just-enqueued sibling step from the claim queue.
    """
    return str(
        row.get("updatedAt")
        or row.get("finishedAt")
        or row.get("claimedAt")
        or row.get("createdAt")
        or ""
    )


def _merge_doc(
    collection: str,
    doc_id: str,
    base: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    if collection == "kv" and doc_id == "images":
        return merge_images_kv(base, incoming)
    if collection == "kv" and doc_id == "provision_finance":
        return merge_finance_kv(base, incoming)
    return merge_byid_updated_at(base, incoming)


def _finance_cards_complete(raw: Any) -> list[dict[str, Any]]:
    """Rows in provision_finance.cards that still have PAN/exp/cvv."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        number = "".join(ch for ch in str(row.get("number") or "") if ch.isdigit())
        exp = str(row.get("exp") or "").strip()
        cvv = str(row.get("cvv") or "").strip()
        if number and exp and cvv:
            out.append(dict(row))
    return out


def merge_finance_kv(
    base: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    """Cloud wins for most finance fields, but never drop a complete local card.

    Stale Firestore ``provision_finance`` docs (cardName only, empty cards[])
    used to clobber a just-migrated ``billing_card.json`` promotion on bg refresh.
    """
    if not isinstance(incoming, dict):
        return incoming if isinstance(incoming, dict) else {}
    if not isinstance(base, dict):
        return dict(incoming)
    out = dict(incoming)
    remote_cards = _finance_cards_complete(incoming.get("cards"))
    local_cards = _finance_cards_complete(base.get("cards"))
    if local_cards and not remote_cards:
        out["cards"] = local_cards
        for key in ("cardId", "cardNumber", "cardExp", "cardCvv", "cardName"):
            if base.get(key) not in (None, ""):
                out[key] = base.get(key)
        # Push the repaired doc back so cloud converges.
        return out
    if local_cards and remote_cards:
        # Union by PAN so neither side silently drops a saved card.
        by_number: dict[str, dict[str, Any]] = {}
        for row in remote_cards + local_cards:
            number = "".join(ch for ch in str(row.get("number") or "") if ch.isdigit())
            if number:
                by_number[number] = dict(row)
        out["cards"] = list(by_number.values())
    return out


def merge_images_kv(
    base: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    """Union image library rows by id so an empty/stale cloud doc cannot wipe stills."""
    if not isinstance(incoming, dict):
        return incoming if isinstance(incoming, dict) else {}
    if not isinstance(base, dict):
        return dict(incoming)
    out = dict(incoming)
    base_imgs = base.get("images")
    inc_imgs = incoming.get("images")
    if not isinstance(inc_imgs, list):
        inc_imgs = []
    if not isinstance(base_imgs, list):
        base_imgs = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in base_imgs + inc_imgs:
        if not isinstance(row, dict):
            continue
        iid = str(row.get("id") or "").strip()
        if not iid:
            continue
        prev = by_id.get(iid)
        if prev is None or float(row.get("addedAt") or 0) >= float(
            prev.get("addedAt") or 0
        ):
            by_id[iid] = dict(row)
    # If cloud sent an empty list but disk still has rows, keep disk.
    if not by_id and base_imgs:
        for row in base_imgs:
            if isinstance(row, dict) and str(row.get("id") or "").strip():
                by_id[str(row.get("id")).strip()] = dict(row)
    out["images"] = list(by_id.values())
    seen: set[str] = set()
    order: list[str] = []
    for sid in list(incoming.get("order") or []) + list(base.get("order") or []):
        key = str(sid or "").strip()
        if not key or key in seen or key not in by_id:
            continue
        seen.add(key)
        order.append(key)
    for key in by_id:
        if key not in seen:
            order.append(key)
            seen.add(key)
    out["order"] = order
    return out


def merge_byid_updated_at(
    base: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    """Keep the newest ``byId`` row per id so a stale full-document write cannot
    clobber a sibling profile that was marked on disk (or in another process).
    """
    if not isinstance(incoming, dict):
        return incoming
    out = dict(incoming)
    if not isinstance(base, dict):
        return out
    base_by = base.get("byId")
    inc_by = incoming.get("byId")
    if not isinstance(inc_by, dict):
        # Do not let an R2 stub replace a byId inventory document.
        if isinstance(base_by, dict):
            return dict(base)
        return out
    if not isinstance(base_by, dict):
        return out
    merged_by: dict[str, Any] = {
        key: dict(val) if isinstance(val, dict) else val
        for key, val in base_by.items()
    }
    for pid, row in inc_by.items():
        if not isinstance(row, dict):
            merged_by[pid] = row
            continue
        existing = merged_by.get(pid)
        if not isinstance(existing, dict) or _row_updated_at(row) >= _row_updated_at(
            existing
        ):
            merged_by[pid] = dict(row)
    out["byId"] = merged_by
    # Incoming ``order`` is a full snapshot; a stale writer drops ids that
    # only exist on disk. Keep those so claim_next_step can still find them.
    base_order = base.get("order")
    inc_order = incoming.get("order")
    if isinstance(base_order, list) or isinstance(inc_order, list):
        seen: set[str] = set()
        merged_order: list[str] = []
        for sid in list(inc_order or []) + list(base_order or []):
            key = str(sid or "")
            if not key or key in seen:
                continue
            seen.add(key)
            merged_order.append(key)
        out["order"] = merged_order[-500:]
    return out


def _read_local_doc(uid: str, collection: str, doc_id: str) -> dict[str, Any] | object | None:
    path = local_path(uid, collection, doc_id)
    if not path.is_file() and storage.ENABLED:
        try:
            storage.ensure_local(uid, storage.rel_of(uid, path))
        except ValueError:
            pass
    local = _read_json_file(path)
    if local is _CORRUPT:
        cached = _cache_get(uid, collection, doc_id)
        return cached if cached is not None else _CORRUPT
    if not isinstance(local, dict):
        return None
    if local.get("storage") == "r2" and local.get("rel"):
        blob = fstore.load_r2_blob(uid, str(local["rel"]), collection, doc_id)
        if isinstance(blob, dict):
            return blob
    try:
        return decrypt_document(uid, collection, doc_id, local)
    except (ValueError, RuntimeError):
        _log.error("unable to decrypt local document %s/%s", collection, doc_id)
        return _CORRUPT


def _resolve_r2_stub(
    uid: str, data: dict[str, Any], collection: str = "", doc_id: str = ""
) -> dict[str, Any]:
    """If ``data`` is an R2 spill stub, load the blob; otherwise return ``data``."""
    if data.get("storage") == "r2" and data.get("rel"):
        blob = fstore.load_r2_blob(uid, str(data["rel"]), collection, doc_id)
        if isinstance(blob, dict):
            return blob
    return data


def get_doc(uid: str, collection: str, doc_id: str) -> dict[str, Any] | None:
    """Return a document: cache → Firestore (+ local merge) → local → legacy.

    When Firestore is enabled, cloud is canonical: a cache miss fetches remote,
    merges with local ``byId`` rows, mirrors the result to ``_fs``, and returns
    that. Network errors fall back to local. A local-only doc is pushed to
    Firestore so orphan scratch cannot diverge from prod. Missing docs are
    negatively cached briefly so list UIs do not re-hit the network on every open.
    """
    cached = _cache_get(uid, collection, doc_id)
    if cached is not None:
        return dict(_resolve_r2_stub(uid, cached, collection, doc_id))
    if _neg_hit(uid, collection, doc_id):
        return None

    local = _read_local_doc(uid, collection, doc_id)
    corrupt = local is _CORRUPT
    local_dict: dict[str, Any] | None = (
        local if isinstance(local, dict) else None
    )

    cloud_enabled = cfstore.enabled() if _primary() == "d1" else fstore.enabled()
    if cloud_enabled:
        remote = (
            cfstore.get(uid, collection, doc_id)
            if _primary() == "d1"
            else fstore.firestore_get(uid, collection, doc_id)
        )
        if remote is fstore.FS_ERROR or remote is cfstore.CF_ERROR:
            if isinstance(local_dict, dict):
                _cache_put(uid, collection, doc_id, local_dict)
                return dict(local_dict)
            if corrupt:
                return None
        elif isinstance(remote, dict):
            remote = _resolve_r2_stub(uid, remote, collection, doc_id)
            if collection == "kv" and doc_id == "images":
                merged = merge_images_kv(local_dict, remote)
            elif collection == "kv" and doc_id == "provision_finance":
                merged = merge_finance_kv(local_dict, remote)
                # If local still holds complete cards the cloud lacked, repair cloud.
                if local_dict and _finance_cards_complete(
                    local_dict.get("cards")
                ) and not _finance_cards_complete(remote.get("cards")):
                    _schedule_primary_repair(uid, collection, doc_id, merged)
            else:
                merged = merge_byid_updated_at(local_dict, remote)
            # Production containers can keep a dirty R2/_fs mirror that still
            # contains worker-step rows. Union-merge would re-inflate the BC
            # table over a clean Firestore doc — scrub before caching.
            if collection == "kv" and doc_id == "provision_profiles":
                try:
                    from .provision import inventory as inv

                    merged, n = inv._scrub_leaked_steps_from_profiles(merged)
                    if n:
                        _log.warning(
                            "scrubbed %s leaked worker-step row(s) on cloud read uid=%s",
                            n,
                            str(uid or "")[:8],
                        )
                except Exception:
                    _log.debug(
                        "provision_profiles scrub on get_doc failed",
                        exc_info=True,
                    )
            _cache_put(uid, collection, doc_id, merged)
            _shadow_compare(uid, collection, doc_id, merged)
            try:
                _write_local(uid, collection, doc_id, merged)
            except Exception:
                _log.warning(
                    "local write of cloud doc failed %s/%s", collection, doc_id
                )
            return dict(merged)
        else:
            # Remote miss: keep local and repair cloud if we have scratch data.
            if isinstance(local_dict, dict):
                _cache_put(uid, collection, doc_id, local_dict)
                _schedule_primary_repair(uid, collection, doc_id, local_dict)
                return dict(local_dict)
            migrated = _migrate_legacy(uid, collection, doc_id)
            if isinstance(migrated, dict):
                return migrated
            _neg_put(uid, collection, doc_id)
            return None

    # Firestore off — local scratch only.
    if corrupt:
        return None
    if isinstance(local_dict, dict):
        _cache_put(uid, collection, doc_id, local_dict)
        return dict(local_dict)

    migrated = _migrate_legacy(uid, collection, doc_id)
    if isinstance(migrated, dict):
        return migrated
    _neg_put(uid, collection, doc_id)
    return None


def _payload_fingerprint(data: dict[str, Any] | None) -> str:
    """Stable JSON fingerprint so no-op mutators skip disk + Firestore spam."""
    if not isinstance(data, dict):
        return ""
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""


def _prepare_payload(
    uid: str, collection: str, doc_id: str, data: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Merge without persistence. Caller holds the document lock."""
    existing = _read_local_doc(uid, collection, doc_id)
    if existing is _CORRUPT:
        existing = _cache_get(uid, collection, doc_id)
    if not isinstance(existing, dict):
        existing = _cache_get(uid, collection, doc_id)
    payload = merge_byid_updated_at(existing if isinstance(existing, dict) else None, data)
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > fstore.DOC_BYTES_MAX:
        return payload, True
    return payload, False


def _commit_local_mirror(
    uid: str, collection: str, doc_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Persist a local/R2 mirror only after the authoritative write commits."""
    if collection == "kv" and doc_id in ("provision_profiles", "provision_steps"):
        # The API and embedded worker run in different processes. Their
        # threading locks do not coordinate, so a stale whole-document write
        # could erase a newer profile state or turn a completed step back into
        # pending. Merge rows by updatedAt under a shared file lock at commit.
        path = local_path(uid, collection, doc_id)
        with _file_lock(path):
            current = _read_local_doc(uid, collection, doc_id)
            if isinstance(current, dict):
                payload = merge_byid_updated_at(current, payload)
            _write_local(uid, collection, doc_id, payload)
            _cache_put(uid, collection, doc_id, payload)
            _list_cache.pop((uid, fstore.safe_id(collection)), None)
        return payload
    _write_local(uid, collection, doc_id, payload)
    _cache_put(uid, collection, doc_id, payload)
    _list_cache.pop((uid, fstore.safe_id(collection)), None)
    return payload


def set_doc(uid: str, collection: str, doc_id: str, data: dict[str, Any]) -> None:
    """Write the primary store, or the local mirror in cloudless dev mode."""
    payload = dict(data or {})
    need_spill = False
    unchanged = False
    with _lock_for(uid, collection, doc_id):
        latest = _cache_get(uid, collection, doc_id)
        if latest is None:
            local = _read_local_doc(uid, collection, doc_id)
            latest = local if isinstance(local, dict) else None
        # Image library writes are full snapshots — do not union-merge or
        # purged stills resurrect from disk. Cloud/local union happens on read.
        if collection == "kv" and doc_id == "images":
            merged = dict(payload)
        else:
            merged = merge_byid_updated_at(
                latest if isinstance(latest, dict) else None, payload
            )
        if _payload_fingerprint(merged) == _payload_fingerprint(
            latest if isinstance(latest, dict) else None
        ):
            unchanged = True
            payload = merged if isinstance(merged, dict) else payload
        else:
            payload = merged
            encoded = json.dumps(payload, ensure_ascii=False)
            need_spill = len(encoded.encode("utf-8")) > fstore.DOC_BYTES_MAX
    if unchanged:
        key = _ckey(uid, collection, doc_id)
        with _fs_sched_lock:
            failed = _fs_failed.get(key)
        if failed is not None and fstore.enabled():
            _schedule_firestore_set(uid, collection, doc_id, failed)
        failed_cf = _cf_failed.get(key)
        if failed_cf is not None and cfstore.enabled():
            _schedule_cloudflare_set(uid, collection, doc_id, failed_cf)
        return
    if need_spill:
        stub = fstore.spill_to_r2(uid, collection, doc_id, payload)
        payload = stub
    else:
        _cleanup_spill_blob(uid, collection, doc_id)
    if _primary() == "d1":
        # D1 is authoritative. Never acknowledge a local-only mutation.
        cfstore.set_doc(uid, collection, doc_id, payload)
        with _lock_for(uid, collection, doc_id):
            _commit_local_mirror(uid, collection, doc_id, payload)
        if _dual_write() and fstore.enabled():
            _schedule_firestore_set(uid, collection, doc_id, payload)
    elif fstore.enabled():
        with _lock_for(uid, collection, doc_id):
            _commit_local_mirror(uid, collection, doc_id, payload)
        _schedule_firestore_set(uid, collection, doc_id, payload)
        if _dual_write() and cfstore.enabled():
            _schedule_cloudflare_set(uid, collection, doc_id, payload)
    else:
        # Local/dev mode: the selected cloud backend is intentionally absent.
        # Previously this branch did nothing, so callers received success but
        # the next request could not find the document (notably New chat).
        with _lock_for(uid, collection, doc_id):
            _commit_local_mirror(uid, collection, doc_id, payload)


def delete_doc(uid: str, collection: str, doc_id: str) -> None:
    if _primary() == "d1" and cfstore.enabled():
        cfstore.delete(uid, collection, doc_id)
    rel = ""
    with _lock_for(uid, collection, doc_id):
        _cache_pop(uid, collection, doc_id)
        path = local_path(uid, collection, doc_id)
        try:
            rel = storage.rel_of(uid, path)
        except ValueError:
            rel = ""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        _list_cache.pop((uid, fstore.safe_id(collection)), None)
    if rel:
        try:
            storage.mirror_delete(uid, rel)
        except Exception:
            pass
    if _primary() == "firestore" or _dual_write():
        fstore.firestore_delete(uid, collection, doc_id)
    if _primary() == "firestore" and _dual_write() and cfstore.enabled():
        cfstore.delete(uid, collection, doc_id)
    _cleanup_spill_blob(uid, collection, doc_id)


def list_docs(
    uid: str,
    collection: str,
    *,
    resolve_r2: bool = False,
    allow_network: bool = True,
) -> list[dict[str, Any]]:
    """List documents with a short TTL cache.

    By default R2-spilled stubs are left as stubs (list views only need ids /
    metadata). Pass ``resolve_r2=True`` when callers need full bodies.
    ``allow_network=False`` skips Firestore (hot UI polls should use local _fs).
    """
    ckey = (uid, fstore.safe_id(collection))
    now = time.monotonic()
    hit = _list_cache.get(ckey)
    if hit and (now - hit[0]) < _LIST_TTL:
        return [dict(r) for r in hit[1]]

    out: list[dict[str, Any]] = []
    remote = None
    if allow_network:
        remote = (
            cfstore.list_docs(uid, collection)
            if _primary() == "d1" and cfstore.enabled()
            else fstore.firestore_list(uid, collection)
        )
    if remote is not None:
        for row in remote:
            if not isinstance(row, dict):
                continue
            data = row
            if resolve_r2 and data.get("storage") == "r2" and data.get("rel"):
                blob = fstore.load_r2_blob(
                    uid, str(data["rel"]), collection, str(data.get("id") or "")
                )
                if isinstance(blob, dict):
                    blob.setdefault("id", data.get("id"))
                    data = blob
            doc_id = str(data.get("id") or "")
            if doc_id:
                cached = _cache_get(uid, collection, doc_id)
                if cached is not None:
                    data = dict(cached)
                    data.setdefault("id", doc_id)
                else:
                    _cache_put(uid, collection, doc_id, data)
            out.append(data)
    else:
        folder = user_root(uid) / "_fs" / fstore.safe_id(collection)
        if not folder.is_dir() and storage.ENABLED and allow_network:
            try:
                for obj in storage.list_remote(uid, f"_fs/{fstore.safe_id(collection)}"):
                    storage.ensure_local(uid, obj["rel"])
            except Exception:
                pass
        if folder.is_dir():
            for path in folder.glob("*.json"):
                data = _read_local_doc(uid, collection, path.stem)
                if data is _CORRUPT or not data:
                    continue
                if not isinstance(data, dict):
                    continue
                data.setdefault("id", path.stem)
                cached = _cache_get(uid, collection, path.stem)
                if cached is not None:
                    data = dict(cached)
                    data.setdefault("id", path.stem)
                out.append(data)

    _list_cache[ckey] = (now, out)
    extra = len(_list_cache) - 80
    if extra > 0:
        for dead in list(_list_cache.keys())[:extra]:
            if dead != ckey:
                _list_cache.pop(dead, None)
    return [dict(r) for r in out]


_file_locks: dict[str, FileLock] = {}
_file_locks_guard = threading.Lock()


def _file_lock(path: Path) -> FileLock:
    """One FileLock object per path: reentrant in this process, exclusive across
    processes (the API server and the embedded worker share the local store)."""
    key = str(path) + ".lock"
    with _file_locks_guard:
        lock = _file_locks.get(key)
        if lock is None:
            lock = FileLock(key, timeout=30)
            _file_locks[key] = lock
        return lock


def mutate_doc(
    uid: str, collection: str, doc_id: str, mutator: Mutator
) -> dict[str, Any]:
    if collection != "kv":
        return _mutate_doc(uid, collection, doc_id, mutator, fresh_disk=False)
    # KV docs are written by both the API process and the worker process. The
    # in-process cache and threading locks cannot see the other process, so a
    # mutator running on a cached copy can silently undo the other's write
    # (e.g. a monitor check erasing a manual "white-hat approved"). Hold one
    # cross-process lock across read → mutate → commit and read the disk copy.
    with _file_lock(local_path(uid, collection, doc_id)):
        return _mutate_doc(uid, collection, doc_id, mutator, fresh_disk=True)


def _mutate_doc(
    uid: str, collection: str, doc_id: str, mutator: Mutator, *, fresh_disk: bool
) -> dict[str, Any]:
    current = get_doc(uid, collection, doc_id) or {}
    need_spill = False
    payload: dict[str, Any] = {}
    unchanged = False
    with _lock_for(uid, collection, doc_id):
        latest = None if fresh_disk else _cache_get(uid, collection, doc_id)
        if latest is None:
            local = _read_local_doc(uid, collection, doc_id)
            latest = local if isinstance(local, dict) else current
        if isinstance(latest, dict):
            latest = _resolve_r2_stub(uid, latest, collection, doc_id)
        out = mutator(dict(latest or {}))
        if out is None:
            out = latest or {}
        if not isinstance(out, dict):
            raise TypeError("mutator must return a dict")
        # Idle /worker/next polls used to rewrite identical provision_steps every
        # few seconds and stampede Firestore into 504 Deadline Exceeded.
        if _payload_fingerprint(out) == _payload_fingerprint(
            latest if isinstance(latest, dict) else None
        ):
            unchanged = True
            payload = out
        else:
            payload = out
            encoded = json.dumps(payload, ensure_ascii=False)
            need_spill = len(encoded.encode("utf-8")) > fstore.DOC_BYTES_MAX
    if unchanged:
        # If a prior cloud push failed, keep retrying the last payload on no-ops
        # so idle polls eventually heal Firestore without rewriting local disk.
        key = _ckey(uid, collection, doc_id)
        with _fs_sched_lock:
            failed = _fs_failed.get(key)
        if failed is not None and fstore.enabled():
            _schedule_firestore_set(uid, collection, doc_id, failed)
        return dict(payload)
    if need_spill:
        stub = fstore.spill_to_r2(uid, collection, doc_id, payload)
        payload = stub
    else:
        _cleanup_spill_blob(uid, collection, doc_id)
    # Always commit to local disk first - this is the source of truth for local dev
    with _lock_for(uid, collection, doc_id):
        payload = _commit_local_mirror(uid, collection, doc_id, payload)
    # Then sync to cloud backends
    if _primary() == "d1":
        cfstore.set_doc(uid, collection, doc_id, payload)
        if _dual_write() and fstore.enabled():
            _schedule_firestore_set(uid, collection, doc_id, payload)
    elif fstore.enabled():
        _schedule_firestore_set(uid, collection, doc_id, payload)
        if _dual_write() and cfstore.enabled():
            _schedule_cloudflare_set(uid, collection, doc_id, payload)
    return dict(payload)


def _schedule_primary_repair(
    uid: str, collection: str, doc_id: str, payload: dict[str, Any]
) -> None:
    if _primary() == "d1":
        _schedule_cloudflare_set(uid, collection, doc_id, payload)
    elif fstore.enabled():
        _schedule_firestore_set(uid, collection, doc_id, payload)


def _shadow_compare(
    uid: str, collection: str, doc_id: str, primary: dict[str, Any]
) -> None:
    if not _shadow_read():
        return
    try:
        shadow = (
            fstore.firestore_get(uid, collection, doc_id)
            if _primary() == "d1"
            else cfstore.get(uid, collection, doc_id)
        )
        if shadow is fstore.FS_ERROR or shadow is cfstore.CF_ERROR:
            return
        if not isinstance(shadow, dict) or _payload_fingerprint(
            shadow
        ) != _payload_fingerprint(primary):
            _log.warning(
                "userstore shadow mismatch %s/%s uid=%s",
                collection,
                doc_id,
                str(uid)[:8],
            )
    except Exception:
        _log.warning("userstore shadow compare failed %s/%s", collection, doc_id)


def _schedule_cloudflare_set(
    uid: str, collection: str, doc_id: str, payload: dict[str, Any]
) -> None:
    key = _ckey(uid, collection, doc_id)

    def _run() -> None:
        try:
            cfstore.set_doc(uid, collection, doc_id, payload)
            _cf_failed.pop(key, None)
        except Exception as exc:
            _cf_failed[key] = dict(payload)
            _log.error(
                "D1 persist failed for %s/%s/%s: %s",
                uid,
                collection,
                doc_id,
                type(exc).__name__,
            )

    threading.Thread(
        target=_run, name=f"us-d1-set-{collection[:12]}", daemon=True
    ).start()


def _schedule_firestore_set(
    uid: str, collection: str, doc_id: str, payload: dict[str, Any]
) -> None:
    """Queue Firestore persist so Google auth blips cannot hang API handlers.

    Coalesces concurrent schedules for the same doc: only one write is in flight,
    and any newer payload queued while it runs is flushed afterward.
    """
    key = _ckey(uid, collection, doc_id)
    with _fs_sched_lock:
        _fs_pending[key] = dict(payload or {})
        if key in _fs_inflight:
            return
        _fs_inflight.add(key)

    def _run() -> None:
        while True:
            with _fs_sched_lock:
                data = _fs_pending.pop(key, None)
                if data is None:
                    _fs_inflight.discard(key)
                    return
            try:
                ok = fstore.firestore_set(uid, collection, doc_id, data)
                if ok:
                    with _fs_sched_lock:
                        _fs_failed.pop(key, None)
                else:
                    with _fs_sched_lock:
                        _fs_failed[key] = data
                    _log.error(
                        "Firestore persist failed for %s/%s/%s (local+_fs/R2 still written)",
                        uid,
                        collection,
                        doc_id,
                    )
            except Exception as exc:
                with _fs_sched_lock:
                    _fs_failed[key] = data
                _log.error(
                    "Firestore persist crashed for %s/%s/%s: %s",
                    uid,
                    collection,
                    doc_id,
                    exc,
                )

    threading.Thread(
        target=_run, name=f"us-fs-set-{collection[:12]}", daemon=True
    ).start()


def invalidate_uid(uid: str) -> None:
    """Drop in-memory cache entries for a uid (Worker may have written Firestore)."""
    uid = str(uid or "")
    if not uid:
        return
    with _cache_lock:
        dead = [k for k in _cache if k[0] == uid]
        for k in dead:
            _cache.pop(k, None)
        dead_neg = [k for k in _neg_cache if k[0] == uid]
        for k in dead_neg:
            _neg_cache.pop(k, None)
    dead_lists = [k for k in _list_cache if k[0] == uid]
    for k in dead_lists:
        _list_cache.pop(k, None)
    try:
        from . import chat_store

        chat_store.invalidate_index(uid)
    except Exception:
        pass
    try:
        from . import usage as usage_mod

        usage_mod.invalidate(uid)
    except Exception:
        pass


def get_kv(uid: str, key: str) -> dict[str, Any] | None:
    return get_doc(uid, "kv", key)


def get_kv_local_first(
    uid: str, key: str, *, refresh: bool = True, fallback_to_remote: bool = True
) -> dict[str, Any] | None:
    """Return cached/disk KV immediately and refresh the primary store in background.

    If there is no local snapshot (for example a fresh cloud container), the
    default falls back to the canonical D1 read. Bootstrap callers can
    disable that fallback to avoid speculative remote reads during an outage.
    """
    cached = _cache_get(uid, "kv", key)
    if cached is not None:
        if refresh and _primary() == "firestore" and fstore.enabled():
            _schedule_firestore_refresh(uid, "kv", key)
        return dict(_resolve_r2_stub(uid, cached, "kv", key))
    local = _read_local_doc(uid, "kv", key)
    if isinstance(local, dict):
        data = dict(_resolve_r2_stub(uid, local, "kv", key))
        _cache_put(uid, "kv", key, data)
        if refresh and _primary() == "firestore" and fstore.enabled():
            _schedule_firestore_refresh(uid, "kv", key)
        return data
    return get_kv(uid, key) if fallback_to_remote else None


def peek_local_kv(uid: str, key: str) -> dict[str, Any] | None:
    """Disk snapshot, bypassing the in-process cache."""
    return _read_local_doc(uid, "kv", key)


def set_kv(uid: str, key: str, data: dict[str, Any]) -> None:
    set_doc(uid, "kv", key, data)


def patch_kv(uid: str, key: str, mutator: Mutator) -> dict[str, Any]:
    return mutate_doc(uid, "kv", key, mutator)


def maps_path(uid: str, path: Path) -> tuple[str, str] | None:
    try:
        rel = storage.rel_of(uid, path)
    except ValueError:
        return None
    return fstore.target_for_rel(rel)


def save_legacy_path(uid: str, path: Path, data: dict[str, Any]) -> bool:
    """Route a legacy user-root JSON path through UserStore. False if unmapped."""
    target = maps_path(uid, path)
    if not target:
        return False
    set_doc(uid, target[0], target[1], data)
    return True


def load_legacy_path(uid: str, path: Path) -> dict[str, Any] | None:
    target = maps_path(uid, path)
    if not target:
        return None
    return get_doc(uid, target[0], target[1])


def delete_legacy_path(uid: str, path: Path) -> bool:
    target = maps_path(uid, path)
    if not target:
        return False
    delete_doc(uid, target[0], target[1])
    return True
