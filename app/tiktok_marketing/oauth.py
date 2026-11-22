"""Advertiser OAuth. The auth code is single-use and lives one hour."""

from __future__ import annotations

import os
import secrets
from typing import Any
from urllib.parse import urlencode

from .client import tiktok_request

DEFAULT_REDIRECT = "YOUR_VALUE"


def app_id() -> str:
    return (os.environ.get("TIKTOK_APP_ID") or "").strip()


def app_secret() -> str:
    return (os.environ.get("TIKTOK_APP_SECRET") or "").strip()


def redirect_uri() -> str:
    return (os.environ.get("TIKTOK_REDIRECT_URI") or DEFAULT_REDIRECT).strip()


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_auth_url(state: str) -> str:
    query = urlencode(
        {
            "app_id": app_id(),
            "state": state,
            "redirect_uri": redirect_uri(),
        }
    )
    return "https://business-api.tiktok.com/portal/auth?" + query


def exchange_auth_code(auth_code: str) -> dict[str, Any]:
    """Exchange a one-time auth_code for a long-lived access token."""
    data = tiktok_request(
        "POST",
        "/oauth2/access_token/",
        "",
        json_body={
            "app_id": app_id(),
            "secret": app_secret(),
            "auth_code": str(auth_code or "").strip(),
        },
    )
    return {
        "accessToken": str(data.get("access_token") or ""),
        "advertiserIds": [str(x) for x in (data.get("advertiser_ids") or [])],
        "scope": data.get("scope") or [],
    }


def list_authorized_advertisers(token: str) -> list[dict[str, Any]]:
    data = tiktok_request(
        "GET",
        "/oauth2/advertiser/get/",
        token,
        params={"app_id": app_id(), "secret": app_secret()},
    )
    rows = data.get("list") or []
    return [row for row in rows if isinstance(row, dict)]
