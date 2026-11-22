"""Long-lived MCP API keys (hashed) stored per uid in userstore.

Keys are shown once at creation: ``ctk_<keyId>_<secret>``.
Verification is constant-time against sha256(secret).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from typing import Any

from . import userstore

_KV = "mcp_keys"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _store(uid: str) -> dict[str, Any]:
    data = userstore.get_kv(uid, _KV) or {}
    if not isinstance(data, dict):
        data = {}
    keys = data.get("keys")
    if not isinstance(keys, list):
        keys = []
    data["keys"] = keys
    return data


def _save(uid: str, data: dict[str, Any]) -> None:
    userstore.set_kv(uid, _KV, data)


def list_keys(uid: str) -> list[dict[str, Any]]:
    """Public metadata only (never hashes or secrets)."""
    out = []
    for row in _store(uid).get("keys") or []:
        if not isinstance(row, dict) or row.get("revoked"):
            continue
        out.append(
            {
                "id": row.get("id"),
                "label": row.get("label") or "",
                "mode": row.get("mode") or "confirm",
                "createdAt": row.get("createdAt"),
                "lastUsedAt": row.get("lastUsedAt"),
                "prefix": row.get("prefix") or "",
            }
        )
    return out


def create_key(
    uid: str,
    *,
    label: str = "",
    mode: str = "trusted",
) -> dict[str, Any]:
    mode = "trusted" if mode == "trusted" else "confirm"
    kid = uuid.uuid4().hex[:12]
    secret = secrets.token_urlsafe(32)
    raw = f"ctk_{kid}_{secret}"
    row = {
        "id": kid,
        "label": (label or "").strip() or "MCP key",
        "mode": mode,
        "hash": _hash(secret),
        "prefix": raw[:16],
        "createdAt": time.time(),
        "lastUsedAt": None,
        "revoked": False,
    }
    data = _store(uid)
    data["keys"].append(row)
    _save(uid, data)
    return {
        "id": kid,
        "label": row["label"],
        "mode": mode,
        "key": raw,  # shown once
        "prefix": row["prefix"],
        "createdAt": row["createdAt"],
    }


def revoke_key(uid: str, key_id: str) -> bool:
    data = _store(uid)
    found = False
    for row in data["keys"]:
        if isinstance(row, dict) and row.get("id") == key_id:
            row["revoked"] = True
            found = True
    if found:
        _save(uid, data)
    return found


def verify_key(raw: str) -> dict[str, Any] | None:
    """Return {uid, keyId, mode} or None. Scans all users via Firestore is
    expensive — instead keys embed kid and we look up via a reverse index
    document ``mcp_key_index/{kid}`` → uid.
    """
    token = (raw or "").strip()
    if not token.startswith("ctk_"):
        return None
    parts = token.split("_", 2)
    if len(parts) != 3:
        return None
    _, kid, secret = parts
    if not kid or not secret:
        return None

    idx = userstore.get_doc("_system", "mcp_key_index", kid)
    uid = ""
    if isinstance(idx, dict):
        uid = str(idx.get("uid") or "")
    if not uid:
        # Fallback: try common single-tenant / restore from any cached write
        return None

    data = _store(uid)
    for row in data.get("keys") or []:
        if not isinstance(row, dict) or row.get("id") != kid:
            continue
        if row.get("revoked"):
            return None
        if not hmac.compare_digest(str(row.get("hash") or ""), _hash(secret)):
            return None
        row["lastUsedAt"] = time.time()
        _save(uid, data)
        return {
            "uid": uid,
            "keyId": kid,
            "mode": "trusted" if row.get("mode") == "trusted" else "confirm",
        }
    return None


def register_index(uid: str, key_id: str) -> None:
    userstore.set_doc("_system", "mcp_key_index", key_id, {"uid": uid, "at": time.time()})


def create_key_indexed(uid: str, *, label: str = "", mode: str = "trusted") -> dict[str, Any]:
    created = create_key(uid, label=label, mode=mode)
    register_index(uid, created["id"])
    return created
