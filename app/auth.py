"""Firebase ID-token verification for the API.

Tokens are verified against Google's public certs with the audience set to
the Firebase project id — no service-account key is needed. Verified tokens
are cached in-process (keyed by token, bounded by their expiry) because the
UI polls some endpoints every second.

Local development: set ALLOW_DEV_NO_AUTH=1 to run without Firebase; every
request is treated as the "dev" user.
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import HTTPException, Request

FIREBASE_PROJECT_ID = (os.environ.get("FIREBASE_PROJECT_ID") or "").strip()
ALLOW_DEV_NO_AUTH = os.environ.get("ALLOW_DEV_NO_AUTH") == "1"
# When ALLOW_DEV_NO_AUTH=1, optional override (env or X-Dev-Uid) for e2e against a real library.
DEV_UID = (os.environ.get("CAPTION_DEV_UID") or "dev").strip() or "dev"

# Public Firebase web-app config served to the frontend (safe to expose).
FIREBASE_WEB_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY") or "",
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN")
    or (f"{FIREBASE_PROJECT_ID}.firebaseapp.com" if FIREBASE_PROJECT_ID else ""),
    "projectId": FIREBASE_PROJECT_ID,
    "appId": os.environ.get("FIREBASE_APP_ID") or "",
}

_cache: dict[str, tuple[str, float]] = {}  # token -> (uid, expires_at)
_cache_lock = threading.Lock()
_MAX_CACHE = 5000

_google_request = None


def _verify(token: str) -> tuple[str, float]:
    """Return (uid, exp) for a valid Firebase ID token, else raise ValueError."""
    global _google_request
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    if _google_request is None:
        _google_request = google_requests.Request()
    claims = google_id_token.verify_firebase_token(
        token, _google_request, audience=FIREBASE_PROJECT_ID
    )
    uid = (claims or {}).get("sub") or ""
    if not uid:
        raise ValueError("Token has no subject")
    exp = float(claims.get("exp") or (time.time() + 300))
    return uid, exp


def _token_from_request(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # Query fallback for <img>/<video>/download links that cannot set headers.
    return (request.query_params.get("auth") or "").strip()


def get_current_uid(request: Request) -> str:
    if not FIREBASE_PROJECT_ID:
        if ALLOW_DEV_NO_AUTH:
            header_uid = (request.headers.get("x-dev-uid") or "").strip()
            allow_header = os.environ.get("CAPTION_ALLOW_X_DEV_UID") == "1"
            if header_uid and allow_header:
                return header_uid
            return DEV_UID
        raise HTTPException(503, "Authentication is not configured on the server")

    token = _token_from_request(request)
    if not token:
        raise HTTPException(401, "Missing authentication token")

    now = time.time()
    with _cache_lock:
        hit = _cache.get(token)
        if hit and hit[1] > now:
            return hit[0]

    try:
        uid, exp = _verify(token)
    except Exception as exc:
        raise HTTPException(401, f"Invalid authentication token: {exc}") from exc

    with _cache_lock:
        if len(_cache) > _MAX_CACHE:
            _cache.clear()
        _cache[token] = (uid, min(exp, now + 600))
    return uid
