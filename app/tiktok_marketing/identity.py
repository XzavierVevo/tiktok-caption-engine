"""Creator identity. Approval is once on the hub; the ad account still gets assign."""

from __future__ import annotations

import time
from typing import Any

from .client import TikTokApiError, tiktok_request


def request_creator_authorization(token: str, bc_id: str) -> str:
    data = tiktok_request(
        "POST",
        "/bc/asset/account/authorization/",
        token,
        json_body={"bc_id": str(bc_id), "is_ads_only_mode": False},
    )
    return str(data.get("bc_auth_qr_code") or "")


def list_tt_assets(token: str, bc_id: str) -> list[dict[str, Any]]:
    data = tiktok_request(
        "GET",
        "/bc/asset/get/",
        token,
        params={"bc_id": str(bc_id), "asset_type": "TT_ACCOUNT", "page_size": 50},
    )
    return [row for row in (data.get("list") or []) if isinstance(row, dict)]


def list_advertiser_assets(token: str, bc_id: str) -> list[str]:
    """Ad accounts on this BC from GET /bc/asset/get/?asset_type=ADVERTISER."""
    if not bc_id:
        return []
    data = tiktok_request(
        "GET",
        "/bc/asset/get/",
        token,
        params={"bc_id": str(bc_id), "asset_type": "ADVERTISER", "page_size": 50},
    )
    ids: list[str] = []
    for row in data.get("list") or []:
        if not isinstance(row, dict):
            continue
        aid = str(row.get("asset_id") or row.get("advertiser_id") or "").strip()
        if aid and aid not in ids:
            ids.append(aid)
    return ids


def wait_for_tt_asset(token: str, bc_id: str, *, timeout_s: float = 20) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = list_tt_assets(token, bc_id)
        if rows:
            return rows[0]
        time.sleep(1)
    return None


def add_partner(token: str, bc_id: str, partner_id: str) -> dict[str, Any]:
    return tiktok_request(
        "POST",
        "/bc/partner/add/",
        token,
        json_body={"bc_id": str(bc_id), "partner_id": str(partner_id)},
    )


def share_identity_to_bc(token: str, bc_id: str, partner_id: str, asset_ids: list[str]) -> dict[str, Any]:
    """Share Clair onto a partner BC as a TT_ACCOUNT asset via POST /bc/partner/add/."""
    return tiktok_request(
        "POST",
        "/bc/partner/add/",
        token,
        json_body={
            "bc_id": str(bc_id),
            "partner_id": str(partner_id),
            "asset_type": "TT_ACCOUNT",
            "asset_ids": [str(item) for item in asset_ids if str(item).strip()],
        },
    )


def share_advertisers_to_partner(
    token: str,
    bc_id: str,
    partner_id: str,
    advertiser_ids: list[str],
    *,
    role: str = "ADMIN",
) -> dict[str, Any]:
    """POST /bc/partner/add/ with asset_type=ADVERTISER. Extra ad accounts as assets."""
    ids = [str(item).strip() for item in advertiser_ids if str(item).strip()]
    if not ids:
        return {}
    return tiktok_request(
        "POST",
        "/bc/partner/add/",
        token,
        json_body={
            "bc_id": str(bc_id),
            "partner_id": str(partner_id),
            "asset_type": "ADVERTISER",
            "asset_ids": ids,
            "advertiser_role": role,
        },
    )


def assign_to_advertiser(token: str, bc_id: str, asset_id: str, advertiser_id: str) -> dict[str, Any]:
    """POST /bc/asset/advertiser/assign/. Same Business Center only."""
    return tiktok_request(
        "POST",
        "/bc/asset/advertiser/assign/",
        token,
        json_body={
            "bc_id": str(bc_id),
            "asset_type": "TT_ACCOUNT",
            "asset_id": str(asset_id),
            "advertiser_id": str(advertiser_id),
        },
    )


def list_assigned_advertisers(token: str, bc_id: str, asset_id: str) -> list[dict[str, Any]]:
    """GET /bc/asset/advertiser/assigned/. Ad accounts already linked to Clair."""
    data = tiktok_request(
        "GET",
        "/bc/asset/advertiser/assigned/",
        token,
        params={
            "bc_id": str(bc_id),
            "asset_id": str(asset_id),
            "asset_type": "TT_ACCOUNT",
            "page": 1,
            "page_size": 50,
        },
    )
    return [row for row in (data.get("list") or []) if isinstance(row, dict)]


def _already_assigned(exc: TikTokApiError) -> bool:
    text = f"{exc} {exc.code or ''}".lower()
    return any(part in text for part in ("already", "duplicate", "exist"))


def assign_tt_account_to_advertisers(
    token: str,
    bc_id: str,
    asset_id: str,
    advertiser_ids: list[str],
) -> dict[str, str]:
    """Give every extra (and seed) ad account Clair as a TT_ACCOUNT asset.

    This is the dedicated Marketing API bind: one POST per advertiser on
    the Business Center that owns those accounts.
    """
    results: dict[str, str] = {}
    for raw in advertiser_ids:
        aid = str(raw or "").strip()
        if not aid or aid in results:
            continue
        try:
            assign_to_advertiser(token, bc_id, asset_id, aid)
            results[aid] = "assigned"
        except TikTokApiError as exc:
            results[aid] = "assigned" if _already_assigned(exc) else str(exc)
    return results


def list_identities(token: str, advertiser_id: str) -> list[dict[str, Any]]:
    data = tiktok_request(
        "GET",
        "/identity/get/",
        token,
        params={"advertiser_id": str(advertiser_id), "identity_type": "BC_AUTH_TT"},
    )
    return [row for row in (data.get("identity_list") or data.get("list") or []) if isinstance(row, dict)]


def identity_can_pull(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    return bool(row.get("can_pull_video")) and str(row.get("identity_type") or "") == "BC_AUTH_TT"


def pull_posts(token: str, advertiser_id: str, identity: dict[str, Any]) -> list[dict[str, Any]]:
    data = tiktok_request(
        "GET",
        "/identity/video/get/",
        token,
        params=_video_params(advertiser_id, identity),
    )
    return [row for row in (data.get("video_list") or []) if isinstance(row, dict)]


def ad_format_for_item_type(item_type: str) -> str:
    """Spark photo posts are CAROUSEL_ADS. Spark videos stay SINGLE_VIDEO."""
    kind = str(item_type or "").strip().upper()
    if "CAROUSEL" in kind or "PHOTO" in kind or kind in {"IMAGE", "MULTI_IMAGE"}:
        return "CAROUSEL_ADS"
    if "VIDEO" in kind:
        return "SINGLE_VIDEO"
    return ""


def _video_params(advertiser_id: str, identity: dict[str, Any], *, cursor: str = "", count: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {
        "advertiser_id": str(advertiser_id),
        "identity_id": str(identity.get("identity_id") or ""),
        "identity_type": str(identity.get("identity_type") or "BC_AUTH_TT"),
        "identity_authorized_bc_id": str(identity.get("identity_authorized_bc_id") or ""),
    }
    if count:
        params["count"] = int(count)
    if cursor:
        params["cursor"] = cursor
    return params


def item_ad_formats(
    token: str,
    advertiser_id: str,
    identity: dict[str, Any],
    item_ids: list[str],
) -> dict[str, str]:
    """Map TikTok item ids to SINGLE_VIDEO or CAROUSEL_ADS from the creator's posts."""
    wanted = list(dict.fromkeys(str(item).strip() for item in item_ids if str(item).strip()))
    if not wanted or not identity.get("identity_id"):
        return {}
    found: dict[str, str] = {}
    cursor = ""
    for page in range(15):
        params = _video_params(advertiser_id, identity, cursor=cursor, count=20)
        try:
            data = tiktok_request("GET", "/identity/video/get/", token, params=params)
        except TikTokApiError:
            if page:
                break
            data = tiktok_request(
                "GET",
                "/identity/video/get/",
                token,
                params=_video_params(advertiser_id, identity),
            )
        for row in data.get("video_list") or []:
            if not isinstance(row, dict):
                continue
            item = str(row.get("item_id") or "").strip()
            if not item:
                continue
            found[item] = ad_format_for_item_type(str(row.get("item_type") or row.get("itemType") or ""))
        if set(wanted).issubset(found):
            break
        cursor = str(data.get("cursor") or "")
        if not data.get("has_more") or not cursor:
            break
    return {item: found[item] for item in wanted if item in found}


def share_or_raise(token: str, bc_id: str, partner_id: str, asset_ids: list[str]) -> str:
    try:
        share_identity_to_bc(token, bc_id, partner_id, asset_ids)
        return "shared"
    except TikTokApiError as exc:
        return f"share_rejected:{exc}"
