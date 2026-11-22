"""Campaign, ad group, and ad bodies. Sales Instant Page only, or lead form only."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

US_LOCATION_ID = "6252001"
AGE_GROUPS = ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"]


def operation_status(publish: bool) -> str:
    return "ENABLE" if publish else "DISABLE"


def request_id() -> str:
    return str(int(time.time() * 1000))


def schedule_start(minutes: int = 10) -> str:
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def sales_campaign_body(
    *,
    advertiser_id: str,
    name: str,
    publish: bool,
) -> dict[str, Any]:
    return {
        "advertiser_id": str(advertiser_id),
        "request_id": request_id(),
        "campaign_name": name[:512],
        "objective_type": "WEB_CONVERSIONS",
        "sales_destination": "WEBSITE",
        "budget_optimize_on": False,
        "operation_status": operation_status(publish),
    }


def lead_campaign_body(*, advertiser_id: str, name: str, publish: bool) -> dict[str, Any]:
    return {
        "advertiser_id": str(advertiser_id),
        "request_id": request_id(),
        "campaign_name": name[:512],
        "objective_type": "LEAD_GENERATION",
        "budget_optimize_on": False,
        "operation_status": operation_status(publish),
    }


def _targeting(device_models: list[str] | None) -> dict[str, Any]:
    targeting: dict[str, Any] = {
        "location_ids": [US_LOCATION_ID],
        "age_groups": AGE_GROUPS,
        "spc_audience_age": "OVER_EIGHTEEN",
    }
    models = [str(item) for item in (device_models or []) if str(item).strip()]
    if models:
        targeting["device_model_ids"] = models
    return targeting


def sales_adgroup_body(
    *,
    advertiser_id: str,
    campaign_id: str,
    name: str,
    budget: float,
    bid: float,
    pixel_id: str = "",
    device_models: list[str] | None = None,
) -> dict[str, Any]:
    """Instant Page completion (outbound click on the page), never ad click.

    TikTok Instant Page does not take a website pixel. CONVERT + BUTTON is the
    Instant Page CTA / outbound click, billed as USD/Conversion.
    """
    del pixel_id  # Instant Page forbids pixel_id; conversion is the native page CTA.
    return {
        "advertiser_id": str(advertiser_id),
        "campaign_id": str(campaign_id),
        "request_id": request_id(),
        "adgroup_name": name[:512],
        "promotion_type": "WEBSITE",
        "promotion_website_type": "TIKTOK_NATIVE_PAGE",
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],
        "video_download_disabled": True,
        "share_disabled": True,
        "targeting_spec": _targeting(device_models),
        "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
        "budget": float(budget),
        "operation_status": "ENABLE",
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": schedule_start(),
        "bid_type": "BID_TYPE_CUSTOM",
        "billing_event": "OCPM",
        "pacing": "PACING_MODE_SMOOTH",
        "optimization_goal": "CONVERT",
        "optimization_event": "BUTTON",
        "conversion_bid_price": float(bid),
    }


def website_click(payload: dict[str, Any] | None) -> bool:
    text = str((payload or {}).get("bidScope") or (payload or {}).get("destination") or "").lower()
    return any(token in text for token in ("website", "cpc", "click"))


# TikTok device-model catalog, brand level. iOS means the Apple brand.
# Android means the active Android brands, not the operating-system menu.
APPLE_DEVICE_MODEL_IDS = ["202"]
ANDROID_DEVICE_MODEL_IDS = [
    "107",  # OPPO
    "109",  # Samsung
    "110",  # HUAWEI
    "111",  # Honor
    "112",  # Xiaomi
    "114",  # TECNO
    "125",  # Zen Admire Unity
    "130",  # ZTE
    "145",  # Motorola
    "148",  # OnePlus
    "156",  # Infinix
    "177",  # Google
    "181",  # RealMe
    "191",  # Vivo
    "203",  # ITEL
    "298",  # Discovery
]


def _device_text(payload: dict[str, Any] | None) -> str:
    return " ".join(
        str((payload or {}).get(key) or "")
        for key in ("bidScope", "os", "targeting", "device")
    ).lower()


def ios_only(payload: dict[str, Any] | None) -> bool:
    text = _device_text(payload)
    return any(token in text for token in ("ios", "iphone", "apple"))


def android_only(payload: dict[str, Any] | None) -> bool:
    return "android" in _device_text(payload)


def device_model_ids(payload: dict[str, Any] | None) -> list[str]:
    """Device models for an iOS or Android ask. Empty when neither was asked."""
    if ios_only(payload):
        return list(APPLE_DEVICE_MODEL_IDS)
    if android_only(payload):
        return list(ANDROID_DEVICE_MODEL_IDS)
    return []


def website_adgroup_body(
    *,
    advertiser_id: str,
    campaign_id: str,
    name: str,
    budget: float,
    bid: float,
    pixel_id: str = "",
    ios: bool = False,
    device_models: list[str] | None = None,
) -> dict[str, Any]:
    """Website click. Opens the lander. No Instant Page.

    iOS and Android are device-model picks (Apple, or the Android brands),
    not the operating-system version menu.
    """
    targeting: dict[str, Any] = {
        "location_ids": [US_LOCATION_ID],
        "age_groups": AGE_GROUPS,
        "spc_audience_age": "OVER_EIGHTEEN",
    }
    models = [str(item) for item in (device_models or []) if str(item).strip()]
    if not models and ios:
        models = list(APPLE_DEVICE_MODEL_IDS)
    if models:
        targeting["device_model_ids"] = models
    body = {
        "advertiser_id": str(advertiser_id),
        "campaign_id": str(campaign_id),
        "request_id": request_id(),
        "adgroup_name": name[:512],
        "promotion_type": "WEBSITE",
        "promotion_website_type": "UNSET",
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],
        "video_download_disabled": True,
        "share_disabled": True,
        "targeting_spec": targeting,
        "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
        "budget": float(budget),
        "operation_status": "ENABLE",
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": schedule_start(),
        "bid_type": "BID_TYPE_CUSTOM",
        "billing_event": "CPC",
        "pacing": "PACING_MODE_SMOOTH",
        "optimization_goal": "CLICK",
        "bid_price": float(bid),
        "targeting_optimization_mode": "MANUAL",
    }
    if str(pixel_id or "").strip():
        body["pixel_id"] = str(pixel_id)
    return body


def lead_adgroup_body(
    *,
    advertiser_id: str,
    campaign_id: str,
    name: str,
    budget: float,
    bid: float,
    device_models: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "advertiser_id": str(advertiser_id),
        "campaign_id": str(campaign_id),
        "request_id": request_id(),
        "adgroup_name": name[:512],
        "promotion_type": "LEAD_GENERATION",
        "promotion_target_type": "INSTANT_PAGE",
        "optimization_goal": "LEAD_GENERATION",
        "placement_type": "PLACEMENT_TYPE_NORMAL",
        "placements": ["PLACEMENT_TIKTOK"],
        "video_download_disabled": True,
        "share_disabled": True,
        "targeting_spec": _targeting(device_models),
        "budget_mode": "BUDGET_MODE_DAY",
        "budget": float(budget),
        "operation_status": "ENABLE",
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": schedule_start(),
        "bid_type": "BID_TYPE_CUSTOM",
        "bid_price": float(bid),
        "billing_event": "OCPM",
    }


def normalize_ad_format(value: str) -> str:
    """CAROUSEL_ADS for a photo post, SINGLE_VIDEO for a video. Blank if unknown."""
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not text:
        return ""
    if "CAROUSEL" in text or "PHOTO" in text or text in {"IMAGE", "MULTI_IMAGE"}:
        return "CAROUSEL_ADS"
    if "VIDEO" in text or text == "SPARK_ADS":
        return "SINGLE_VIDEO"
    return ""


def format_from_url(url: str) -> str:
    text = str(url or "")
    if "/photo/" in text:
        return "CAROUSEL_ADS"
    if "/video/" in text:
        return "SINGLE_VIDEO"
    return ""


def resolved_ad_format(post: dict[str, Any] | None) -> str:
    """Each post keeps its own format. A photo URL is a carousel even with no adFormat."""
    row = post or {}
    return (
        normalize_ad_format(str(row.get("adFormat") or ""))
        or format_from_url(str(row.get("postUrl") or row.get("url") or ""))
    )


def sales_ad_name(
    template: str,
    *,
    offer: str,
    index: int,
    item_id: str,
    ad_format: str,
) -> str:
    """One name per ad: offer, index, item id, and carousel or video."""
    from ..affiliate.s2 import slug

    kind = "carousel" if normalize_ad_format(ad_format) == "CAROUSEL_ADS" else "video"
    offer_slug = slug(offer) or "reco"
    item = str(item_id or "").strip()
    text = str(template or "{offer}-{i}")
    named = (
        text.replace("{offer}", offer_slug)
        .replace("{i}", str(index))
        .replace("{item}", item)
        .replace("{format}", kind)
    )
    if item and item not in named:
        named = f"{named}-{item}"
    if kind not in named:
        named = f"{named}-{kind}"
    return named[:512]


def ad_body(
    *,
    advertiser_id: str,
    adgroup_id: str,
    posts: list[dict[str, Any]],
    identity: dict[str, Any],
    page_id: str,
    name_for: Any,
    call_to_action_id: str = "",
    card_id: str = "",
    landing_url: str = "",
) -> dict[str, Any]:
    """One Spark post per ad. Carousel and video stay on that post's own format.

    Instant Page uses page_list. Website CPC uses landing_page_url and no page.
    """
    post = posts[0] if posts else {}
    creative: dict[str, Any] = {
        "creative_info": {
            "ad_format": resolved_ad_format(post) or "SINGLE_VIDEO",
            "tiktok_item_id": str(post.get("itemId") or post.get("item_id") or ""),
            "identity_type": str(identity.get("identity_type") or "BC_AUTH_TT"),
            "identity_id": str(identity.get("identity_id") or ""),
            "identity_authorized_bc_id": str(identity.get("identity_authorized_bc_id") or ""),
        }
    }
    body: dict[str, Any] = {
        "advertiser_id": str(advertiser_id),
        "adgroup_id": str(adgroup_id),
        "ad_name": name_for(1, post)[:512],
        "operation_status": "ENABLE",
        "call_to_action_list": [{"call_to_action": "LEARN_MORE"}],
        "creative_list": [creative],
        "ad_configuration": {
            "creative_auto_add_toggle": False,
            "creative_auto_enhancement_strategy_list": [],
            "product_info_enabled": "UNSET",
            "product_info": {
                "product_titles": [],
                "product_image_list": [],
                "selling_points": [],
            },
            "promo_info_list": [],
        },
    }
    url = str(landing_url or "").strip()
    if not page_id:
        url = url or str(post.get("landingUrl") or "").strip()
    if page_id:
        body["page_list"] = [{"page_id": str(page_id)}]
    elif url:
        body["landing_page_url_list"] = [{"landing_page_url": url}]
    if card_id:
        body["interactive_add_on_list"] = [{"card_id": str(card_id)}]
    if call_to_action_id:
        body["ad_configuration"]["call_to_action_id"] = str(call_to_action_id)
    return body
