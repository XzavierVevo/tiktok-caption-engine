"""Instant Page read and editor token. Creation itself is InstantPage.start."""

from __future__ import annotations

from typing import Any

from .client import tiktok_request


def mint_tip_token(access_token: str, advertiser_id: str) -> str:
    data = tiktok_request(
        "POST",
        "/oauth2/access_token/tip_sdk/create/",
        access_token,
        json_body={"advertiser_id": str(advertiser_id)},
    )
    return str(data.get("tip_sdk_access_token") or "")


def list_pages(
    token: str,
    advertiser_id: str,
    *,
    business_type: str,
    title: str = "",
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "advertiser_id": str(advertiser_id),
        "business_type": business_type,
        "page_size": 100,
    }
    if title:
        params["title"] = title
    data = tiktok_request("GET", "/page/get/", token, params=params)
    return [row for row in (data.get("list") or []) if isinstance(row, dict)]


def find_page(token: str, advertiser_id: str, *, business_type: str, title: str) -> dict[str, Any] | None:
    for row in list_pages(token, advertiser_id, business_type=business_type, title=title):
        if str(row.get("title") or "") == title and str(row.get("status") or "") == "PUBLISHED":
            return row
    return None


def editor_page_html(
    *,
    advertiser_id: str,
    app_id: str,
    tip_token: str,
    business_type: int,
    page_id: str = "",
) -> str:
    """Page that opens InstantPage.start. The popup is filled by the driver."""
    action = "edit" if page_id else "create"
    page_line = f"\n    pageId: {page_id!r}," if page_id else ""
    return f"""<!doctype html>
<html><head><title>Instant page</title></head><body>
<script>
window.InstantPageAsyncInit = function () {{
  InstantPage.init({{ appId: {app_id!r}, version: "v1.0" }});
}};
window.__pageResult = null;
function start() {{
  if (!window.InstantPage) return setTimeout(start, 200);
  InstantPage.start({{
    accountId: {advertiser_id!r},
    appId: {app_id!r},
    ixAccessToken: {tip_token!r},
    businessType: {int(business_type)},
    actionType: {action!r},{page_line}
    display: "popup",
    language: "en"
  }}, function (response) {{ window.__pageResult = response || null; }});
}}
</script>
<script async defer crossorigin="anonymous" src="https://sf-unpkg.tiktokcdn.com/obj/unpkg-va/latest/byted/instant_page_sdk/dist/index.js"></script>
<script>start();</script>
</body></html>
"""
