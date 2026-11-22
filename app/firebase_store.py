"""Per-user Firestore document store with a local/R2 fallback.

When FIREBASE_SERVICE_ACCOUNT_JSON (or GOOGLE_APPLICATION_CREDENTIALS) is
set, users/{uid}/... is the backend of record. Otherwise documents live under
data/users/{uid}/_fs/ and are mirrored to R2 like other user files.

The browser never writes these documents; the Python backend is the mutator.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from typing import Any

from .config import user_root

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_client = None
_client_failed = False
_client_retry_after = 0.0
_last_write_ok: bool | None = None
_last_write_error: str = ""
# touch_user once per process — every set used to pay an extra round-trip.
_touched_users: set[str] = set()
# The Firestore streaming RPC is not safe to stampede from request threads.
# Cache small collection listings briefly and serialize the actual stream.
_list_lock = threading.Lock()
_list_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_list_fail_until: dict[tuple[str, str], float] = {}

DOC_BYTES_MAX = 900_000
_SAFE_ID = re.compile(r"[^A-Za-z0-9._~-]+")
# Distinct from a true miss (None). Callers must not negative-cache this.
FS_ERROR = object()
_FS_TIMEOUT_S = 20.0
_FS_LIST_TIMEOUT_S = 30.0
_FS_WRITE_ATTEMPTS = 3
# Firestore forbids arrays nested inside arrays; wrap them for round-trip.
# Do not use a `__…__` key — those field names are reserved by Firestore.
_NESTED_ARRAY_KEY = "_ctNestedArray"
_FS_AUTH_RETRY_S = 30.0
# A 429 is not a transient transport blip: retrying it immediately just spends
# more quota and makes a cold container wait behind gRPC backoff. Give the
# project enough time to leave Firestore's rate-limit window before probing it.
_FS_QUOTA_RETRY_S = 120.0


def _mark_client_unavailable(exc: BaseException) -> None:
    """Open a short circuit after auth/transport failures.

    Without this, every local request starts another gRPC auth plugin call while
    Google's token endpoint is unreachable, producing a traceback and another
    long wait before userstore can use its disk mirror.
    """
    global _client, _client_failed, _client_retry_after
    _client = None
    _client_failed = True
    is_quota = _is_quota_error(exc)
    retry_after = _FS_QUOTA_RETRY_S if is_quota else _FS_AUTH_RETRY_S
    _client_retry_after = time.monotonic() + retry_after
    _log.warning(
        "Firestore temporarily unavailable; using local storage for %.0fs: %s",
        retry_after,
        exc,
    )


def _transient_firestore_error(exc: BaseException) -> bool:
    """True for deadline / unavailable blips worth a short retry."""
    text = str(exc or "").lower()
    markers = (
        "deadline exceeded",
        "504",
        "503",
        "unavailable",
        "timeout",
        "temporarily",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "transporterror",
        "goaway",
        "429",
        "quota exceeded",
        "resource exhausted",
    )
    return any(m in text for m in markers)


def _is_quota_error(exc: BaseException) -> bool:
    """Return true for Firestore RESOURCE_EXHAUSTED / HTTP 429 responses."""
    text = str(exc or "").lower()
    return "429" in text or "quota exceeded" in text or "resource exhausted" in text


def _retryable_list_error(exc: BaseException) -> bool:
    """Only retry transport failures; never retry quota exhaustion in-stream."""
    return _transient_firestore_error(exc) and not _is_quota_error(exc)


def enabled() -> bool:
    # Firestore is retained only as code-level rollback scaffolding. Production
    # state traffic must never initialize a Google client after the D1 cutover.
    return (
        (os.environ.get("USERSTORE_FIRESTORE_ROLLBACK") or "").strip() == "1"
        and _credentials() is not None
    )


def write_status() -> dict[str, Any]:
    """Health/debug snapshot for /healthz (no secrets)."""
    creds = _credentials() if enabled() else None
    client_ok = False
    if creds is not None and not _client_failed:
        client_ok = client() is not None
    return {
        "enabled": creds is not None,
        "clientOk": client_ok,
        "lastWriteOk": _last_write_ok,
        "lastWriteError": _last_write_error or "",
    }


def sanitize_for_firestore(value: Any, *, _in_array: bool = False) -> Any:
    """Convert a JSON-like value into something Firestore accepts.

    Nested arrays become ``{"_ctNestedArray": [...]}`` so reads can reverse
    the transform. Non-JSON types fall back to strings.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        if set(value.keys()) == {_NESTED_ARRAY_KEY} and isinstance(
            value.get(_NESTED_ARRAY_KEY), list
        ):
            return {
                _NESTED_ARRAY_KEY: [
                    sanitize_for_firestore(x, _in_array=True)
                    for x in value[_NESTED_ARRAY_KEY]
                ]
            }
        return {
            str(k): sanitize_for_firestore(v, _in_array=False)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [sanitize_for_firestore(x, _in_array=True) for x in value]
        if _in_array:
            return {_NESTED_ARRAY_KEY: items}
        return items
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return str(value)


def desanitize_from_firestore(value: Any) -> Any:
    """Reverse :func:`sanitize_for_firestore` nested-array wrappers."""
    if isinstance(value, dict):
        if set(value.keys()) == {_NESTED_ARRAY_KEY} and isinstance(
            value.get(_NESTED_ARRAY_KEY), list
        ):
            return [desanitize_from_firestore(x) for x in value[_NESTED_ARRAY_KEY]]
        return {k: desanitize_from_firestore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [desanitize_from_firestore(x) for x in value]
    return value


def _credentials() -> dict | str | None:
    raw = (os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON") or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            _log.warning("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON")
    path = (
        (os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE") or "").strip()
        or (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    )
    if path and os.path.isfile(path):
        return path
    return None


def client():
    """Firestore client or None when no service account is configured."""
    global _client, _client_failed, _client_retry_after
    # Application state is now authoritative in Cloudflare D1.  Keep this
    # adapter dormant unless an operator explicitly enables the rollback path.
    if (os.environ.get("USERSTORE_FIRESTORE_ROLLBACK") or "").strip() != "1":
        return None
    if _client_failed:
        if time.monotonic() < _client_retry_after:
            return None
        _client_failed = False
    creds = _credentials()
    if creds is None:
        return None
    if _client is not None:
        return _client
    with _lock:
        if _client is not None or _client_failed:
            return _client
        try:
            from google.cloud import firestore
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            project = (os.environ.get("FIREBASE_PROJECT_ID") or "").strip() or None
            if isinstance(creds, dict):
                credentials = service_account.Credentials.from_service_account_info(
                    creds,
                    scopes=["https://www.googleapis.com/auth/datastore"],
                )
            else:
                credentials = service_account.Credentials.from_service_account_file(
                    creds,
                    scopes=["https://www.googleapis.com/auth/datastore"],
                )
            # Refresh before constructing the gRPC channel.  A token endpoint
            # outage is then a normal caught exception instead of an uncaught
            # AuthMetadataPluginCallback traceback from a gRPC worker thread.
            base_request = Request()

            def _bounded_auth_request(*args, **kwargs):
                requested = kwargs.get("timeout")
                try:
                    requested_s = float(requested) if requested is not None else 8.0
                except (TypeError, ValueError):
                    requested_s = 8.0
                kwargs["timeout"] = min(8.0, max(1.0, requested_s))
                return base_request(*args, **kwargs)

            credentials.refresh(_bounded_auth_request)
            _client = firestore.Client(
                project=project or getattr(credentials, "project_id", None),
                credentials=credentials,
            )
            _client_failed = False
            _client_retry_after = 0.0
        except Exception as exc:
            _mark_client_unavailable(exc)
            return None
    return _client


def safe_id(doc_id: str) -> str:
    cleaned = _SAFE_ID.sub("_", (doc_id or "").strip())[:128]
    return cleaned or "doc"


def target_for_rel(rel: str) -> tuple[str, str] | None:
    """Map a user-root relative JSON path to (collection, doc_id)."""
    from . import userstore

    rel = (rel or "").replace("\\", "/").lstrip("/")
    hit = userstore.PATH_KV.get(rel)
    if hit:
        return hit
    if rel.startswith("chats/") and rel.endswith(".json") and rel != "chats/index.json":
        return "chats", safe_id(rel[len("chats/") : -len(".json")])
    return None


def _user_ref(uid: str):
    db = client()
    if db is None:
        return None
    return db.collection("users").document(uid)


def touch_user(uid: str) -> bool:
    uid = str(uid or "")
    if not uid:
        return False
    if uid in _touched_users:
        return True
    ref = _user_ref(uid)
    if ref is None:
        return False
    try:
        ref.set({"uid": uid}, merge=True, timeout=_FS_TIMEOUT_S)
        _touched_users.add(uid)
        return True
    except Exception as exc:
        if _transient_firestore_error(exc):
            _mark_client_unavailable(exc)
        _log.error("Firestore touch_user failed: %s", exc)
        return False


def _doc_ref(uid: str, collection: str, doc_id: str):
    uref = _user_ref(uid)
    if uref is None:
        return None
    return uref.collection(safe_id(collection)).document(safe_id(doc_id))


def firestore_get(uid: str, collection: str, doc_id: str) -> dict | None:
    """Network-only get. None when Firestore is off or the doc is missing."""
    ref = _doc_ref(uid, collection, doc_id)
    if ref is None:
        return None
    last_exc: BaseException | None = None
    for attempt in range(_FS_WRITE_ATTEMPTS):
        try:
            snap = ref.get(timeout=_FS_TIMEOUT_S)
            if snap.exists:
                raw = snap.to_dict() or {}
                data = desanitize_from_firestore(raw)
                return data if isinstance(data, dict) else {}
            return None
        except Exception as exc:
            last_exc = exc
            if (
                _transient_firestore_error(exc)
                and not _is_quota_error(exc)
                and attempt + 1 < _FS_WRITE_ATTEMPTS
            ):
                time.sleep(0.1 * (2**attempt))
                continue
            if _transient_firestore_error(exc):
                _mark_client_unavailable(exc)
            break
    _log.error("Firestore get %s/%s failed: %s", collection, doc_id, last_exc)
    return FS_ERROR


def firestore_set(uid: str, collection: str, doc_id: str, data: dict) -> bool:
    """Network-only set. Returns False when Firestore is off or the write fails."""
    global _last_write_ok, _last_write_error
    ref = _doc_ref(uid, collection, doc_id)
    if ref is None:
        return False
    payload = sanitize_for_firestore(dict(data or {}))
    if not isinstance(payload, dict):
        raise TypeError("sanitized Firestore payload must be a dict")
    last_exc: BaseException | None = None
    for attempt in range(_FS_WRITE_ATTEMPTS):
        try:
            touch_user(uid)
            ref.set(payload, timeout=_FS_TIMEOUT_S)
            _list_cache.pop((str(uid or ""), safe_id(collection)), None)
            _last_write_ok = True
            _last_write_error = ""
            return True
        except Exception as exc:
            last_exc = exc
            if (
                _transient_firestore_error(exc)
                and not _is_quota_error(exc)
                and attempt + 1 < _FS_WRITE_ATTEMPTS
            ):
                time.sleep(0.1 * (2**attempt))
                continue
            if _transient_firestore_error(exc):
                _mark_client_unavailable(exc)
            break
    _last_write_ok = False
    _last_write_error = f"{collection}/{doc_id}: {last_exc}"
    _log.error("Firestore set %s/%s failed: %s", collection, doc_id, last_exc)
    return False


def firestore_delete(uid: str, collection: str, doc_id: str) -> bool:
    ref = _doc_ref(uid, collection, doc_id)
    if ref is None:
        return False
    try:
        ref.delete(timeout=_FS_TIMEOUT_S)
        _list_cache.pop((str(uid or ""), safe_id(collection)), None)
        return True
    except Exception as exc:
        if _transient_firestore_error(exc):
            _mark_client_unavailable(exc)
        _log.error("Firestore delete %s/%s failed: %s", collection, doc_id, exc)
        return False


def firestore_list(uid: str, collection: str) -> list[dict] | None:
    """Network-only list. None when Firestore is off (caller uses local _fs)."""
    key = (str(uid or ""), safe_id(collection))
    now = time.monotonic()
    cached = _list_cache.get(key)
    if cached and now - cached[0] < 5.0:
        return [dict(row) for row in cached[1]]
    # During an outage, return the last good snapshot rather than launching
    # another 30s stream for every heartbeat and chat navigation.
    if now < _list_fail_until.get(key, 0):
        return [dict(row) for row in cached[1]] if cached else None
    ref = _user_ref(uid)
    if ref is None:
        return None
    with _list_lock:
        now = time.monotonic()
        cached = _list_cache.get(key)
        if cached and now - cached[0] < 5.0:
            return [dict(row) for row in cached[1]]
        if now < _list_fail_until.get(key, 0):
            return [dict(row) for row in cached[1]] if cached else None
        try:
            # Pass an explicit Retry object.  google-cloud-firestore 2.23 can
            # otherwise inspect ``transport.run_query._retry`` after a stream
            # error, but grpcio exposes that transport member as a raw
            # _UnaryStreamMultiCallable with no _retry attribute.
            from google.api_core.retry import Retry

            out: list[dict] = []
            for snap in ref.collection(safe_id(collection)).stream(
                retry=Retry(predicate=_retryable_list_error, deadline=_FS_LIST_TIMEOUT_S),
                timeout=_FS_LIST_TIMEOUT_S,
            ):
                raw = snap.to_dict() or {}
                data = desanitize_from_firestore(raw)
                if not isinstance(data, dict):
                    data = {}
                data.setdefault("id", snap.id)
                out.append(data)
            _list_cache[key] = (time.monotonic(), out)
            _list_fail_until.pop(key, None)
            return [dict(row) for row in out]
        except Exception as exc:
            if _transient_firestore_error(exc):
                _mark_client_unavailable(exc)
            _list_fail_until[key] = time.monotonic() + 20.0
            _log.error("Firestore list %s failed: %s", collection, exc)
            return [dict(row) for row in cached[1]] if cached else None


def spill_to_r2(uid: str, collection: str, doc_id: str, data: dict) -> dict:
    from . import storage
    from .config import user_root as root
    from .state_crypto import encrypt_document

    rel = f"_fs/_blobs/{safe_id(collection)}_{safe_id(doc_id)}.json"
    path = root(uid) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    storage.atomic_write_json(
        path, encrypt_document(uid, collection, doc_id, data), indent=None
    )
    storage.mirror_up(uid, path)
    return {
        "id": data.get("id") or doc_id,
        "storage": "r2",
        "rel": rel,
        "title": data.get("title") or "",
        "updatedAt": data.get("updatedAt") or 0,
    }


def load_r2_blob(
    uid: str, rel: str, collection: str = "", doc_id: str = ""
) -> dict | None:
    from . import storage
    from .state_crypto import decrypt_document

    local = storage.ensure_local(uid, rel)
    if not local or not local.is_file():
        from .config import user_root as root

        local = root(uid) / rel
        if not local.is_file():
            return None
    try:
        data = json.loads(local.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("_enc") == "A256GCM":
        try:
            return decrypt_document(uid, collection, doc_id, data)
        except (ValueError, RuntimeError):
            _log.error("unable to decrypt R2 blob %s", rel)
            return None
    return data
