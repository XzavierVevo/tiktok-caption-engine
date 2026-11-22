"""Cloudflare D1 document adapter via the Worker's private state API."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from .state_crypto import (
    EncryptionConfigurationError,
    decrypt_document,
    encrypt_document,
    encryption_configured,
    is_sensitive,
    verification_hash,
)

_log = logging.getLogger(__name__)
CF_ERROR = object()
_last_ok: bool | None = None
_last_error = ""
_session = requests.Session()


class StateUnavailable(RuntimeError):
    pass


class VersionConflict(RuntimeError):
    pass


def enabled() -> bool:
    return bool(_base_url() and _key())


def _base_url() -> str:
    return (os.environ.get("CLOUDFLARE_STATE_BASE_URL") or os.environ.get("PUBLIC_UI_ORIGIN") or "").strip().rstrip("/")


def _key() -> str:
    return (os.environ.get("INTERNAL_API_KEY") or "").strip()


def _request(op: str, payload: dict[str, Any] | None = None, *, method: str = "POST") -> dict[str, Any]:
    global _last_ok, _last_error
    if not enabled():
        raise StateUnavailable("Cloudflare state API is not configured")
    url = f"{_base_url()}/_internal/state/{op}"
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = _session.request(
                method, url, json=payload if method != "GET" else None,
                headers={"x-internal-api-key": _key()}, timeout=(3.0, 8.0),
            )
            if response.status_code == 409:
                raise VersionConflict("D1 document version conflict")
            if response.status_code >= 500:
                raise StateUnavailable(f"state API returned {response.status_code}")
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise StateUnavailable("state API returned a non-object")
            _last_ok, _last_error = True, ""
            return value
        except VersionConflict:
            raise
        except (requests.RequestException, ValueError, StateUnavailable) as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.1 * (2**attempt))
    _last_ok, _last_error = False, type(last).__name__ if last else "unknown"
    raise StateUnavailable("Cloudflare state API unavailable") from last


def get(uid: str, collection: str, doc_id: str) -> dict[str, Any] | None | object:
    try:
        result = _request("get", {"uid": uid, "collection": collection, "docId": doc_id})
        if not result.get("found"):
            return None
        body = result.get("body")
        if body is None and result.get("storageRel"):
            body = {"id": doc_id, "storage": "r2", "rel": result["storageRel"]}
        return decrypt_document(uid, collection, doc_id, body)
    except StateUnavailable:
        return CF_ERROR


def list_docs(uid: str, collection: str) -> list[dict[str, Any]] | None:
    try:
        result = _request("list", {"uid": uid, "collection": collection})
        rows: list[dict[str, Any]] = []
        for row in result.get("rows") or []:
            if not isinstance(row, dict):
                continue
            doc_id = str(row.get("docId") or "")
            body = row.get("body")
            if body is None and row.get("storageRel"):
                body = {"id": doc_id, "storage": "r2", "rel": row["storageRel"]}
            data = decrypt_document(uid, collection, doc_id, body)
            data["_docId"] = doc_id
            data.setdefault("id", doc_id)
            rows.append(data)
        return rows
    except StateUnavailable:
        return None


def set_doc(uid: str, collection: str, doc_id: str, data: dict[str, Any], *, expected_version: int | None = None) -> int:
    if is_sensitive(collection, doc_id) and not encryption_configured():
        raise EncryptionConfigurationError(
            "D1 writes for sensitive documents require DATA_ENCRYPTION_KEY_V1"
        )
    protected = encrypt_document(uid, collection, doc_id, dict(data or {}))
    storage_rel = str(data.get("rel") or "") if data.get("storage") == "r2" else None
    result = _request("put", {
        "uid": uid, "collection": collection, "docId": doc_id,
        "body": protected,
        "bodySha256": verification_hash(uid, collection, doc_id, dict(data or {})),
        "storageRel": storage_rel, "expectedVersion": expected_version,
    })
    return int(result.get("version") or 0)


def delete(uid: str, collection: str, doc_id: str) -> bool:
    _request("delete", {"uid": uid, "collection": collection, "docId": doc_id})
    return True


def batch_set(items: list[tuple[str, str, str, dict[str, Any]]]) -> None:
    payloads: list[dict[str, Any]] = []
    for uid, collection, doc_id, data in items:
        if is_sensitive(collection, doc_id) and not encryption_configured():
            raise EncryptionConfigurationError(
                "D1 writes for sensitive documents require DATA_ENCRYPTION_KEY_V1"
            )
        protected = encrypt_document(uid, collection, doc_id, dict(data or {}))
        storage_rel = str(data.get("rel") or "") if data.get("storage") == "r2" else None
        payloads.append({
            "uid": uid, "collection": collection, "docId": doc_id,
            "body": protected,
            "bodySha256": verification_hash(uid, collection, doc_id, dict(data or {})),
            "storageRel": storage_rel,
        })
    _request("batch", {"items": payloads})


def health() -> dict[str, Any]:
    return {"enabled": enabled(), "lastOk": _last_ok, "lastError": _last_error, "backend": "d1"}
