"""HTTP client for business-api.tiktok.com. Success is code == 0."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode

import requests

API_BASE = "https://business-api.tiktok.com/open_api/v1.3"


class TikTokApiError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, request_id: str = "", path: str = ""):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.path = path


def tiktok_request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    retries: int = 2,
) -> dict[str, Any]:
    """Call one Marketing API endpoint. Raises TikTokApiError when code != 0."""
    rel = "/" + str(path or "").lstrip("/")
    url = API_BASE + rel
    headers = {"Access-Token": str(token or "").strip()}
    if not files:
        headers["Content-Type"] = "application/json"
    query = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        query[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
    if query:
        url = url + "?" + urlencode(query)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            kwargs: dict[str, Any] = {"timeout": 60}
            if files:
                kwargs["data"] = form or {}
                kwargs["files"] = files
            elif json_body is not None:
                kwargs["json"] = json_body
            response = requests.request(
                method.upper(),
                url,
                headers=headers,
                **kwargs,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise TikTokApiError(str(exc), path=rel) from exc
        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(0.4 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError as exc:
            raise TikTokApiError(
                f"TikTok returned a non-JSON body ({response.status_code})",
                path=rel,
            ) from exc
        if not isinstance(body, dict):
            raise TikTokApiError("TikTok returned an unexpected body", path=rel)
        code = body.get("code")
        if response.status_code >= 400 or code not in (0, "0", None):
            if code in (None,) and response.status_code < 400:
                return body
            raise TikTokApiError(
                str(body.get("message") or "TikTok API error"),
                code=int(code) if str(code or "").lstrip("-").isdigit() else None,
                request_id=str(body.get("request_id") or ""),
                path=rel,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {"data": data}
    if last_exc:
        raise TikTokApiError(str(last_exc), path=rel)
    raise TikTokApiError("TikTok request failed", path=rel)
