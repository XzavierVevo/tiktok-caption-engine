"""Spark code ingress: DuoPlus RPA can POST results here; pollers read the same store.

Uses files under DATA_DIR/spark_callbacks/ so the test script works without uvicorn.
"""

from __future__ import annotations

import json
import os
import re
import time
import ipaddress
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

from .config import DATA_DIR

_CALLBACK_DIR = DATA_DIR / "spark_callbacks"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _secret() -> str:
    return (os.environ.get("SPARK_CALLBACK_SECRET") or "").strip()


def callback_base_url() -> str:
    return (
        os.environ.get("SPARK_CALLBACK_BASE_URL")
        or os.environ.get("SPARK_CALLBACK_BASE")
        or os.environ.get("PUBLIC_UI_ORIGIN")
        or "http://localhost:8765"
    ).rstrip("/")


def callback_url(*, task_name: str = "") -> str:
    """URL for DuoPlus NET_REQUEST after GET_CLIPBOARD / OUTPUT_LOG FINALSPARK."""
    base = f"{callback_base_url()}/api/spark/callback"
    task = (task_name or "").strip()
    secret = _secret()
    params: list[str] = []
    if secret:
        params.append(f"token={quote(secret, safe='')}")
    if task:
        params.append(f"task={quote(task, safe='')}")
    if not params:
        return base
    return f"{base}?{'&'.join(params)}"


def redact_callback_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query=urlencode([
        (k, "REDACTED" if k == "token" else v) for k, v in parse_qsl(parts.query)
    ])))


def delivery_url(*, task_name: str) -> str:
    """Optional external delivery must be explicitly configured and reachable."""
    if os.environ.get("DUOPLUS_SPARK_CALLBACK_ENABLED", "0").lower() not in ("1", "true", "yes"):
        return ""
    url = callback_url(task_name=task_name)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    local = host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host
    try:
        local = local or not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if parts.scheme != "https" or not host or local or parts.username or parts.password or parts.fragment:
        raise ValueError("Spark callbacks require a public HTTPS SPARK_CALLBACK_BASE_URL")
    if not _secret():
        raise ValueError("Spark callbacks require SPARK_CALLBACK_SECRET")
    return url


def deliver_spark(url: str, *, task_name: str, post_url: str, spark_code: str) -> dict:
    """Host-side delivery for the verified driver; never regenerate on delivery failure."""
    if not url:
        return {"callbackDelivery": "disabled"}
    import requests
    for attempt in range(3):
        try:
            response = requests.post(url, json={"spark_code": spark_code, "post_url": post_url},
                                     timeout=(3, 8), allow_redirects=False)
            if response.status_code == 200 and response.json().get("ok") is True:
                return {"callbackDelivery": "delivered"}
            if response.status_code < 500 and response.status_code != 429:
                break
        except (requests.RequestException, ValueError, AttributeError):
            pass
        if attempt < 2:
            time.sleep(1 + attempt)
    return {"callbackDelivery": "failed", "callbackTaskName": task_name}


def _key(task_name: str, post_url: str = "") -> str:
    task = _SAFE.sub("_", (task_name or "").strip())[:160]
    if task:
        return task
    url = (post_url or "").strip()
    if url:
        return _SAFE.sub("_", url)[-180:]
    return "unknown"


def _path(task_name: str, post_url: str = "") -> Path:
    _CALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    return _CALLBACK_DIR / f"{_key(task_name, post_url)}.json"


def save_spark(
    *,
    spark_code: str,
    task_name: str = "",
    post_url: str = "",
    source: str = "callback",
    extra: dict[str, Any] | None = None,
) -> None:
    code = (spark_code or "").strip()
    if not code or code.lower() == "fail":
        return
    payload: dict[str, Any] = {
        "sparkCode": code,
        "taskName": (task_name or "").strip(),
        "postUrl": (post_url or "").strip(),
        "source": source,
        "receivedAt": time.time(),
    }
    if extra:
        payload.update(extra)
    path = _path(task_name, post_url)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        from . import userstore

        userstore.set_doc(
            "_system",
            "spark_callbacks",
            _key(task_name, post_url),
            dict(payload),
        )
    except Exception:
        pass


def read_spark(*, task_name: str = "", post_url: str = "") -> str:
    from . import spark_coordinator
    if task_name and spark_coordinator.configured():
        try:
            result = spark_coordinator.request("/api/spark/result", method="GET", params={"task": task_name}).get("result")
            if isinstance(result, dict) and result.get("taskName") == task_name and (not post_url or result.get("postUrl") == post_url):
                return str(result.get("sparkCode") or "")
        except Exception:
            # Logs remain usable during an ingress outage; do not restart RPA.
            pass
    for key in (_key(task_name, post_url), _key("", post_url)):
        path = _CALLBACK_DIR / f"{key}.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                code = str(data.get("sparkCode") or data.get("spark_code") or "").strip()
                if code:
                    return code
        # Durable fallback (Worker callback / container restart).
        try:
            from . import userstore

            remote = userstore.get_doc("_system", "spark_callbacks", key)
            if isinstance(remote, dict):
                code = str(remote.get("sparkCode") or remote.get("spark_code") or "").strip()
                if code:
                    return code
        except Exception:
            pass
    return ""


def clear_spark(*, task_name: str = "", post_url: str = "") -> None:
    for key in (_key(task_name, post_url), _key("", post_url)):
        path = _CALLBACK_DIR / f"{key}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def verify_callback_token(token: str) -> bool:
    secret = _secret()
    firebase = bool((os.environ.get("FIREBASE_PROJECT_ID") or "").strip())
    if not secret:
        return not firebase
    return (token or "").strip() == secret


def secret_required() -> bool:
    return bool(_secret())
