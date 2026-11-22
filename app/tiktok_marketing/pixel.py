"""Dummy website Pixel so Ads Manager can use Website as the destination."""

from __future__ import annotations

from .client import TikTokApiError, tiktok_request

DUMMY_PIXEL_NAME = "website-dummy"


def list_pixels(token: str, advertiser_id: str) -> list[dict]:
    data = tiktok_request(
        "GET",
        "/pixel/list/",
        token,
        params={"advertiser_id": str(advertiser_id), "page_size": 100},
    )
    rows = data.get("pixels") or data.get("list") or []
    return [row for row in rows if isinstance(row, dict)]


def pick_dummy_pixel(rows: list[dict]) -> dict | None:
    """Prefer the shared website-dummy Pixel. Any other Pixel on the BC still unlocks Website."""
    pixels = [row for row in rows if isinstance(row, dict)]
    for row in pixels:
        name = str(row.get("pixel_name") or row.get("asset_name") or row.get("name") or "").lower()
        if DUMMY_PIXEL_NAME in name:
            return row
    return pixels[0] if pixels else None


def _pixel_id(row: dict | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("pixel_id") or row.get("asset_id") or row.get("id") or "")


def _pixel_code(row: dict | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("pixel_code") or row.get("pixelCode") or "")


def list_bc_pixels(token: str, bc_id: str) -> list[dict]:
    if not bc_id:
        return []
    data = tiktok_request(
        "GET",
        "/bc/asset/admin/get/",
        token,
        params={"bc_id": str(bc_id), "asset_type": "PIXEL", "page_size": 50},
    )
    return [row for row in (data.get("list") or []) if isinstance(row, dict)]


def link_pixel_to_advertiser(token: str, bc_id: str, row: dict, advertiser_id: str) -> bool:
    """Put an existing BC Pixel on this ad account so Website can be the destination."""
    pixel_id = _pixel_id(row)
    code = _pixel_code(row)
    linked = False
    if code:
        try:
            tiktok_request(
                "POST",
                "/bc/pixel/link/update/",
                token,
                json_body={
                    "bc_id": str(bc_id),
                    "pixel_code": code,
                    "advertiser_ids": [str(advertiser_id)],
                    "relation_status": "LINK",
                },
            )
            linked = True
        except TikTokApiError:
            pass
    if pixel_id:
        try:
            tiktok_request(
                "POST",
                "/bc/asset/advertiser/assign/",
                token,
                json_body={
                    "bc_id": str(bc_id),
                    "asset_type": "PIXEL",
                    "asset_id": pixel_id,
                    "advertiser_id": str(advertiser_id),
                },
            )
            linked = True
        except TikTokApiError:
            pass
    return linked


def ensure_website_pixel(token: str, advertiser_id: str, *, bc_id: str = "") -> dict:
    """Reuse a Business Center Pixel, or create website-dummy.

    The Pixel only exists so Ads Manager can use a website link. It is not an Instant Page.
    """
    advertiser_id = str(advertiser_id or "").strip()
    bc_id = str(bc_id or "").strip()
    if not advertiser_id:
        raise TikTokApiError("advertiser id required for a website Pixel")
    if not bc_id:
        try:
            info = tiktok_request(
                "GET",
                "/advertiser/info/",
                token,
                params={"advertiser_ids": [advertiser_id], "fields": ["advertiser_id", "owner_bc_id"]},
            )
            rows = info.get("list") or []
            if rows and isinstance(rows[0], dict):
                bc_id = str(rows[0].get("owner_bc_id") or "")
        except TikTokApiError:
            bc_id = ""
    chosen: dict | None = None
    try:
        chosen = pick_dummy_pixel(list_pixels(token, advertiser_id))
    except TikTokApiError:
        chosen = None
    created = False
    if chosen is None and bc_id:
        try:
            chosen = pick_dummy_pixel(list_bc_pixels(token, bc_id))
        except TikTokApiError:
            chosen = None
    if chosen is None:
        try:
            created_row = tiktok_request(
                "POST",
                "/pixel/create/",
                token,
                json_body={"advertiser_id": advertiser_id, "pixel_name": DUMMY_PIXEL_NAME},
            )
        except TikTokApiError as exc:
            if "duplicat" not in str(exc).lower():
                raise
            chosen = pick_dummy_pixel(list_bc_pixels(token, bc_id)) if bc_id else None
            if chosen is None:
                raise
        else:
            chosen = created_row if isinstance(created_row, dict) else {}
            created = True
    linked = False
    if bc_id and chosen:
        linked = link_pixel_to_advertiser(token, bc_id, chosen, advertiser_id)
    pixel_id = _pixel_id(chosen)
    if not pixel_id:
        raise TikTokApiError("Dummy Pixel was not created")
    return {
        "pixelId": pixel_id,
        "pixelCode": _pixel_code(chosen),
        "name": str((chosen or {}).get("pixel_name") or (chosen or {}).get("asset_name") or DUMMY_PIXEL_NAME),
        "created": created,
        "linked": linked,
        "bcId": bc_id,
        "advertiserId": advertiser_id,
    }


def ensure_dummy_pixel(token: str, advertiser_id: str, *, bc_id: str = "") -> str:
    """Reuse an existing dummy Pixel, or create one. Tracking quality does not matter."""
    return str(ensure_website_pixel(token, advertiser_id, bc_id=bc_id).get("pixelId") or "")
