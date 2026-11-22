"""One-account Marketing API launch. Browser fallback stays in the worker."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..provision.finance import card as card_mod
from ..provision.sales.tracking import tracking_landing_url
from . import payloads
from .client import TikTokApiError, tiktok_request
from .deal import plan_video_campaigns
from .pages import find_page

_log = logging.getLogger("tiktok_marketing.launch")


def publish_enabled() -> bool:
    """Sales campaigns and their ads are created live. Never leave them paused."""
    return True


def update_bid(token: str, advertiser_id: str, adgroup_id: str, bid: float, *, lead: bool = False) -> dict:
    field = "bid_price" if lead else "conversion_bid_price"
    return tiktok_request(
        "POST",
        "/adgroup/update/",
        token,
        json_body={"advertiser_id": advertiser_id, "adgroup_id": adgroup_id, field: float(bid)},
    )


def update_budget(token: str, advertiser_id: str, adgroup_id: str, budget: float) -> dict:
    return tiktok_request(
        "POST",
        "/adgroup/budget/update/",
        token,
        json_body={"advertiser_id": advertiser_id, "budget": [{"adgroup_id": adgroup_id, "budget": float(budget)}]},
    )


def set_operation_status(token: str, advertiser_id: str, campaign_id: str, status: str) -> dict:
    enabled = str(status or "").strip().upper() in {"ENABLE", "ON", "ACTIVE"}
    operation = "ENABLE" if enabled else "DISABLE"
    last = ""
    for path in ("/smart_plus/campaign/status/update/", "/campaign/status/update/"):
        try:
            return tiktok_request(
                "POST",
                path,
                token,
                json_body={
                    "advertiser_id": advertiser_id,
                    "campaign_ids": [campaign_id],
                    "operation_status": operation,
                },
            )
        except TikTokApiError as exc:
            last = str(exc)
    raise TikTokApiError(last or "Could not set campaign status")


def activate_launched(
    token: str,
    advertiser_id: str,
    campaign_id: str,
    ad_ids: list[str] | None = None,
    adgroup_id: str = "",
) -> None:
    """Campaign, ad group, and every ad go on. A paused create is not a launch."""
    if campaign_id:
        try:
            set_operation_status(token, advertiser_id, campaign_id, "ENABLE")
        except TikTokApiError as exc:
            _log.warning("Campaign %s stayed paused: %s", campaign_id, exc)
    if adgroup_id:
        try:
            tiktok_request(
                "POST",
                "/smart_plus/adgroup/status/update/",
                token,
                json_body={
                    "advertiser_id": advertiser_id,
                    "adgroup_ids": [adgroup_id],
                    "operation_status": "ENABLE",
                },
            )
        except TikTokApiError as exc:
            _log.warning("Ad group %s stayed paused: %s", adgroup_id, exc)
    ids = [str(item) for item in (ad_ids or []) if str(item).strip()]
    if not ids:
        return
    last = ""
    for path, key in (
        ("/smart_plus/ad/status/update/", "smart_plus_ad_ids"),
        ("/ad/status/update/", "ad_ids"),
    ):
        try:
            tiktok_request(
                "POST",
                path,
                token,
                json_body={
                    "advertiser_id": advertiser_id,
                    key: ids,
                    "operation_status": "ENABLE",
                },
            )
            return
        except TikTokApiError as exc:
            last = str(exc)
    _log.warning("Ads stayed paused: %s", last)


def _delete_status(
    token: str,
    advertiser_id: str,
    candidates: list[tuple[str, str]],
    ids: list[str],
) -> bool:
    """DELETE is a status update. Smart+ and regular campaigns use different paths."""
    clean = [str(item) for item in ids if str(item).strip()]
    if not clean:
        return False
    last = ""
    for path, id_key in candidates:
        try:
            data = tiktok_request(
                "POST",
                path,
                token,
                json_body={
                    "advertiser_id": str(advertiser_id),
                    id_key: clean,
                    "operation_status": "DELETE",
                },
            )
        except TikTokApiError as exc:
            last = str(exc)
            continue
        errors = data.get("error_list") or data.get("errors") or []
        if errors:
            last = str(errors)
            continue
        return True
    _log.warning("Could not delete %s %s: %s", clean, candidates, last)
    return False


def delete_launch_shell(
    token: str,
    advertiser_id: str,
    *,
    campaign_id: str = "",
    adgroup_ids: list[str] | None = None,
    ad_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Remove a launch that produced no ads. Children first, campaign last.

    Best effort: a failed delete is logged and never masks the launch error
    that sent us here. Returns which levels were removed.
    """
    groups = [str(item) for item in (adgroup_ids or []) if str(item).strip()]
    if campaign_id and adgroup_ids is None:
        try:
            groups = [
                str(row.get("adgroup_id") or "")
                for row in list_campaign_adgroups(token, advertiser_id, campaign_id)
            ]
        except TikTokApiError:
            groups = []
    groups = [item for item in groups if item]
    ads_deleted = _delete_status(
        token,
        advertiser_id,
        [
            ("/smart_plus/ad/status/update/", "smart_plus_ad_ids"),
            ("/ad/status/update/", "ad_ids"),
        ],
        list(ad_ids or []),
    )
    groups_deleted = _delete_status(
        token,
        advertiser_id,
        [
            ("/smart_plus/adgroup/status/update/", "adgroup_ids"),
            ("/adgroup/status/update/", "adgroup_ids"),
        ],
        groups,
    )
    campaign_deleted = _delete_status(
        token,
        advertiser_id,
        [
            ("/smart_plus/campaign/status/update/", "campaign_ids"),
            ("/campaign/status/update/", "campaign_ids"),
        ],
        [campaign_id] if campaign_id else [],
    )
    return {
        "adsDeleted": ads_deleted,
        "adgroupsDeleted": groups_deleted,
        "campaignDeleted": campaign_deleted,
    }


def _dynamic_cta_id(token: str, advertiser_id: str) -> str:
    """Spark web ads require a CTA portfolio. Only Learn More is allowed."""
    from .creative import learn_more_cta_id

    try:
        return learn_more_cta_id(token, advertiser_id)
    except TikTokApiError:
        return ""


def _creative_type_mismatch(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "creative type" in text and "ad_format" in text


def _other_ad_format(fmt: str) -> str:
    return "SINGLE_VIDEO" if str(fmt or "").strip().upper() == "CAROUSEL_ADS" else "CAROUSEL_ADS"


def create_sales_ads(
    token: str,
    *,
    advertiser_id: str,
    adgroup_id: str,
    posts: list[dict[str, Any]],
    identity: dict[str, Any],
    page_id: str,
    name_for: Any,
    display_card: bool | None = None,
    landing_url: str = "",
    progress: list[str] | None = None,
) -> list[str]:
    """One Smart+ ad per video. Learn More only. TAP HERE Display Card is optional.

    ``progress`` receives each ad id as it is created. A caller that gets an
    exception can use it to tell a failed launch with zero ads (roll back) from
    a partial launch that already has at least one ad (leave it running).
    """
    from .creative import display_card_id, learn_more_cta_id

    if display_card is None:
        display_card = bool(card_mod.sales_settings().get("salesDisplayCard", True))
    cta_id = learn_more_cta_id(token, advertiser_id)
    card_id = display_card_id(token, advertiser_id) if display_card else ""
    ad_ids: list[str] = []
    for index, post in enumerate(posts, 1):
        this_page = str(post.get("pageId") or page_id or "").strip()
        this_url = str(landing_url or "").strip()
        if not this_page:
            this_url = this_url or str(post.get("landingUrl") or "").strip()
        if this_page:
            # Native-page ad groups reject a raw website URL as the experience.
            this_url = ""
        if not this_page and not this_url:
            raise TikTokApiError(f"Instant page or landing URL is missing for ad {index}")
        ad_name = str(post.get("adName") or "").strip()
        _log = __import__("logging").getLogger("tiktok_marketing.launch")
        current = dict(post)
        flipped = False
        while True:
            body = payloads.ad_body(
                advertiser_id=advertiser_id,
                adgroup_id=adgroup_id,
                posts=[current],
                identity=identity,
                page_id="" if this_url else this_page,
                name_for=lambda _i, _post, n=index, custom=ad_name: custom or name_for(n, post),
                call_to_action_id=cta_id,
                card_id=card_id,
                landing_url=this_url,
            )
            fmt = str(body["creative_list"][0]["creative_info"].get("ad_format") or "")
            _log.info(
                "sales ad %s format=%s page=%s url=%s",
                ad_name or index,
                fmt,
                "yes" if body.get("page_list") else "no",
                "yes" if body.get("landing_page_url_list") else "no",
            )
            try:
                ad = tiktok_request("POST", "/smart_plus/ad/create/", token, json_body=body)
                break
            except TikTokApiError as exc:
                if flipped or not _creative_type_mismatch(exc):
                    raise
                current["adFormat"] = _other_ad_format(fmt)
                flipped = True
                _log.warning("Ad %s rejected as %s; retrying as %s", ad_name or index, fmt, current["adFormat"])
        post["adFormat"] = current.get("adFormat") or post.get("adFormat")
        ad_id = str(ad.get("smart_plus_ad_id") or ad.get("ad_id") or "")
        if ad_id:
            ad_ids.append(ad_id)
            if progress is not None:
                progress.append(ad_id)
    if not ad_ids:
        raise TikTokApiError("No ads were created")
    return ad_ids


def campaign_name(offer: str, profile_no: str, account: str) -> str:
    return f"{offer or 'offer'}-{profile_no}-ac{account}"


def _post_item_id(post: dict[str, Any]) -> str:
    item = str(post.get("itemId") or post.get("item_id") or "").strip()
    if item:
        return item
    match = re.search(r"/(?:video|photo)/(\d+)", str(post.get("postUrl") or post.get("url") or ""))
    return match.group(1) if match else ""


def launch_for_account(
    uid: str,
    profile: dict[str, Any],
    account: dict[str, Any],
    payload: dict[str, Any],
    *,
    token: str,
    posts: list[dict[str, Any]] | None = None,
    identity: dict[str, Any] | None = None,
    page_id: str = "",
) -> dict[str, Any]:
    """Create one Sales or Lead campaign. Refuses a second campaign on the account."""
    if str(account.get("tiktokCampaignId") or "").strip() and not payload.get("force"):
        return {
            "apiLaunchStep": "launched",
            "tiktokCampaignId": account.get("tiktokCampaignId"),
            "skipped": True,
        }
    objective = str(payload.get("objective") or "sales").strip().lower()
    settings = card_mod.sales_settings(uid=uid)
    budget = float(payload.get("dailyBudgetUsd") or settings.get("salesDailyBudgetUsd") or 20)
    bid = float(payload.get("conversionBidUsd") or settings.get("salesConversionBidUsd") or 0.4)
    advertiser_id = str(account.get("accountId") or payload.get("advertiserId") or "")
    profile_no = str(profile.get("profileNo") or profile.get("id") or "")
    offer = str(payload.get("offer") or "offer")
    from ..affiliate.service import is_payload_reco

    payload_ad = is_payload_reco(
        offer,
        payload.get("offerName"),
        payload.get("lander"),
        payload.get("landingUrl"),
    )
    device_models = (
        list(payloads.APPLE_DEVICE_MODEL_IDS)
        if payload_ad
        else payloads.device_model_ids(payload)
    )
    name = campaign_name(offer, profile_no, str(payload.get("account") or "1"))
    existing = find_existing_campaign(token, advertiser_id, name)
    campaign_id = str((existing or {}).get("campaign_id") or "")
    if campaign_id and _campaign_has_adgroup(token, advertiser_id, campaign_id) and not payload.get("force"):
        return _saved(uid, profile, account, payload, existing, posts or [], identity or {}, page_id, objective)
    chosen: list[dict[str, Any]] = []
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        item = _post_item_id(post)
        if item:
            chosen.append({**post, "itemId": item})
    if not chosen:
        slices = plan_video_campaigns(
            list(payload.get("renders") or []),
            videos_per_campaign=int(payload.get("videosPerCampaign") or 10),
        )
        for post in slices[0]["posts"] if slices else []:
            item = _post_item_id(post)
            if item:
                chosen.append({**post, "itemId": item})
    # A campaign with no video can never hold an ad. Refuse before creating
    # anything, so an empty render list cannot leave a shell in Ads Manager.
    if not chosen:
        raise TikTokApiError("No TikTok videos to launch; campaign was not created")

    pixel_id = str(payload.get("pixelId") or "")
    website = objective != "lead" and payloads.website_click(payload)
    if website:
        from .pixel import ensure_dummy_pixel

        try:
            pixel_id = pixel_id or ensure_dummy_pixel(token, advertiser_id)
        except TikTokApiError:
            pixel_id = pixel_id or ""
        # Click cannot take a Pixel until that Pixel has an event. The Pixel
        # still sits on the ad account so Website is a valid destination.
        pixel_id = ""
    landing = ""
    if objective != "lead":
        from ..affiliate.service import PAYLOAD_RECO_URL, is_payload_reco

        offer_name = str(payload.get("offer") or payload.get("offerName") or "")
        explicit = str(payload.get("landingUrl") or "").strip()
        if explicit:
            landing = explicit
        else:
            if is_payload_reco(offer_name, payload.get("lander")):
                base = PAYLOAD_RECO_URL
            else:
                base = str(
                    payload.get("landingBase")
                    or settings.get("salesLandingUrl")
                    or "https://linkthem.net/?offer_id=4866&aff_id=170848"
                )
            landing = tracking_landing_url(
                base,
                profile_no=profile_no or "0",
                bc=str(payload.get("trackingBc") or "1"),
                account=str(payload.get("account") or "1"),
                ad=1,
            )
    page = page_id or str(payload.get("pageId") or "")
    if not page and not website and token and advertiser_id:
        found = find_page(
            token,
            advertiser_id,
            business_type="LEAD_GEN" if objective == "lead" else "TIKTOK_INSTANT_PAGE",
            title=str(payload.get("pageTitle") or landing or name),
        )
        page = str((found or {}).get("page_id") or "")
    if not page and not website:
        return {
            "apiLaunchStep": "sales_form" if objective != "lead" else "lead_form",
            "apiLaunchError": "Instant page is not ready for this ad account",
            "tiktokCampaignId": "",
            "tiktokAdgroupIds": [],
        }

    publish = publish_enabled()
    created_campaign = not campaign_id
    if created_campaign:
        if objective == "lead":
            campaign = tiktok_request(
                "POST",
                "/smart_plus/campaign/create/",
                token,
                json_body=payloads.lead_campaign_body(advertiser_id=advertiser_id, name=name, publish=publish),
            )
        else:
            campaign = tiktok_request(
                "POST",
                "/smart_plus/campaign/create/",
                token,
                json_body=payloads.sales_campaign_body(
                    advertiser_id=advertiser_id, name=name, publish=publish
                ),
            )
        campaign_id = str(campaign.get("campaign_id") or "")
    if objective == "lead":
        adgroup_body = payloads.lead_adgroup_body(
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
            name=name,
            budget=budget,
            bid=bid,
            device_models=device_models,
        )
    elif website:
        adgroup_body = payloads.website_adgroup_body(
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
            name=name,
            budget=budget,
            bid=bid,
            pixel_id=pixel_id,
            device_models=device_models,
        )
    else:
        adgroup_body = payloads.sales_adgroup_body(
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
            name=name,
            budget=budget,
            bid=bid,
            pixel_id=pixel_id,
            device_models=device_models,
        )
    try:
        adgroup = tiktok_request("POST", "/smart_plus/adgroup/create/", token, json_body=adgroup_body)
    except Exception:
        if created_campaign:
            delete_launch_shell(token, advertiser_id, campaign_id=campaign_id)
        raise
    adgroup_id = str(adgroup.get("adgroup_id") or "")

    use_card = payload.get("displayCard")
    if use_card is None:
        use_card = settings.get("salesDisplayCard", True)
    from ..affiliate.s2 import make_s2, public_ad_url

    template = str(settings.get("salesAdNameTemplate") or "{offer}-{i}")
    offer_ids = [str(item) for item in (payload.get("offerIds") or []) if str(item).strip()]
    annotated = []
    for index, post in enumerate(chosen, 1):
        fmt = payloads.resolved_ad_format(post)
        if fmt:
            post = {**post, "adFormat": fmt}
        ad_name = payloads.sales_ad_name(
            template,
            offer=offer,
            index=index,
            item_id=str(post.get("itemId") or ""),
            ad_format=fmt,
        )
        s2 = str(post.get("s2") or "") or make_s2(profile_no or "0", campaign_id, index, ad_name)
        if website:
            ad_landing = str(post.get("landingUrl") or "") or tracking_landing_url(
                str(payload.get("landingUrl") or landing or settings.get("salesLandingUrl") or ""),
                profile_no=profile_no or "0",
                bc=str(payload.get("trackingBc") or "1"),
                account=str(payload.get("account") or "1"),
                ad=index,
            )
        else:
            ad_landing = public_ad_url(s2, str(post.get("landingUrl") or ""))
        annotated.append({
            **post,
            "adName": ad_name,
            "s2": s2,
            "landingUrl": ad_landing,
            "offerId": str(post.get("offerId") or post.get("offer_id") or (offer_ids[0] if offer_ids else "")),
        })
    chosen = annotated
    created_ads: list[str] = []
    try:
        ad_ids = create_sales_ads(
            token,
            advertiser_id=advertiser_id,
            adgroup_id=adgroup_id,
            posts=chosen,
            identity=identity or {},
            page_id=page,
            name_for=lambda index, _post: str(settings.get("salesAdNameTemplate") or "{offer}-{i}").replace("{offer}", offer).replace("{i}", str(index)),
            display_card=bool(use_card),
            progress=created_ads,
        )
    except Exception:
        if not created_ads:
            delete_launch_shell(
                token,
                advertiser_id,
                campaign_id=campaign_id if created_campaign else "",
                adgroup_ids=[adgroup_id],
            )
        raise
    activate_launched(token, advertiser_id, campaign_id, ad_ids, adgroup_id)
    return _saved(uid, profile, account, payload, {"campaign_id": campaign_id, "adgroup_id": adgroup_id, "ad_ids": ad_ids}, chosen, identity or {}, page, objective, landing=landing, budget=budget, bid=bid)


_CAMPAIGN_FIELDS = [
    "campaign_id",
    "campaign_name",
    "objective_type",
    "operation_status",
    "secondary_status",
    "create_time",
]


def list_advertiser_campaigns(token: str, advertiser_id: str) -> list[dict[str, Any]]:
    """Read campaign status. Does not open AdsPower."""
    if not token or not advertiser_id:
        return []
    data = tiktok_request(
        "GET",
        "/campaign/get/",
        token,
        params={
            "advertiser_id": str(advertiser_id),
            "fields": _CAMPAIGN_FIELDS,
            "page_size": 100,
        },
    )
    return [row for row in (data.get("list") or []) if isinstance(row, dict)]


def classify_white_hat_secondary(secondary: str, operation: str = "") -> str:
    """Map a Marketing API status onto the white-hat watcher kinds.

    A Reach campaign that starts in the future is Scheduled in Ads Manager
    while TikTok reports NOT_START on the ad group (and often the ad).
    Campaign operation ENABLE is not Scheduled: the ad group can still be in
    review. Audit stays pending. Deny is rejected.
    """
    blob = f"{secondary or ''}".upper()
    if not blob.strip() and not str(operation or "").strip():
        return "unknown"
    if any(token in blob for token in ("AUDIT_DENY", "DISAPPROVE", "REJECT")):
        return "rejected"
    # Account-level punish is not a failed white-hat review and not Scheduled.
    if "PUNISH" in blob or "STATUS_LIMIT" in blob:
        return "pending"
    if any(token in blob for token in ("NOT_START", "SCHEDULE", "DELIVERY_OK", "STATUS_DELIVERY")):
        return "scheduled"
    if any(token in blob for token in ("AUDIT", "REVIEW", "PENDING", "REAUDIT")):
        return "pending"
    return "pending"


def white_hat_from_campaigns(
    rows: list[dict[str, Any]],
    *,
    campaign_name: str = "whitehat",
) -> dict[str, Any]:
    want = " ".join((campaign_name or "whitehat").split()).casefold()
    hits = []
    for row in rows:
        name = " ".join(str(row.get("campaign_name") or "").split()).casefold()
        objective = str(row.get("objective_type") or "").strip().upper()
        named = bool(want and want in name)
        # Ads Manager auto-names the Reach wizard ReachYYYYMMDDHHMMSS.
        auto_reach = objective == "REACH" and name.startswith("reach")
        if named or auto_reach:
            hits.append(row)
    if not hits:
        return {"found": False, "kind": "pending", "status": "", "secondary": "", "campaignId": ""}
    chosen = hits[0]
    best = "pending"
    for row in hits:
        kind = classify_white_hat_secondary(
            str(row.get("secondary_status") or ""),
            str(row.get("operation_status") or ""),
        )
        if kind == "scheduled":
            chosen = row
            best = "scheduled"
            break
        if kind == "rejected":
            chosen = row
            best = "rejected"
    secondary = str(chosen.get("secondary_status") or "")
    return {
        "found": True,
        "kind": best,
        "status": secondary or best,
        "secondary": secondary,
        "campaignId": str(chosen.get("campaign_id") or ""),
    }


_ADGROUP_FIELDS = [
    "adgroup_id",
    "adgroup_name",
    "campaign_id",
    "operation_status",
    "secondary_status",
    "schedule_start_time",
    "schedule_end_time",
]


def _start_in_future(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) < 19:
        return False
    try:
        start = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return start > datetime.now(timezone.utc)


def _blocks_schedule(secondary: str) -> bool:
    blob = str(secondary or "").upper()
    return any(
        token in blob
        for token in ("AUDIT", "REVIEW", "PENDING", "REAUDIT", "PUNISH", "DENY", "REJECT", "DISAPPROVE", "LIMIT")
    )


def list_campaign_adgroups(token: str, advertiser_id: str, campaign_id: str) -> list[dict[str, Any]]:
    """Ad groups for one campaign. Schedule start and NOT_START live here."""
    if not token or not advertiser_id or not campaign_id:
        return []
    params = {
        "advertiser_id": str(advertiser_id),
        "filtering": {"campaign_ids": [str(campaign_id)]},
        "page_size": 20,
    }
    try:
        data = tiktok_request(
            "GET",
            "/adgroup/get/",
            token,
            params={**params, "fields": _ADGROUP_FIELDS},
        )
    except TikTokApiError:
        data = tiktok_request("GET", "/adgroup/get/", token, params=params)
    return [row for row in (data.get("list") or []) if isinstance(row, dict)]


def refine_white_hat_status(
    campaign: dict[str, Any],
    adgroups: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Prefer the ad group over the campaign.

    Campaign ENABLE while the ad group is still in review stays pending.
    NOT_START, or a start time that is still in the future and not in review,
    is Scheduled.
    """
    if not campaign.get("found") or not adgroups:
        return campaign
    rejected = ""
    scheduled = ""
    review = ""
    schedule_start = ""
    for row in adgroups:
        secondary = str(row.get("secondary_status") or "")
        operation = str(row.get("operation_status") or "")
        kind = classify_white_hat_secondary(secondary, operation)
        start = str(row.get("schedule_start_time") or "")
        if kind != "scheduled" and _start_in_future(start) and not _blocks_schedule(secondary):
            kind = "scheduled"
        if kind == "rejected" and not rejected:
            rejected = secondary or "rejected"
        elif kind == "scheduled" and not scheduled:
            scheduled = secondary or "ADGROUP_STATUS_NOT_START"
            schedule_start = start
        elif _blocks_schedule(secondary) and not review:
            review = secondary
    if rejected:
        return {**campaign, "kind": "rejected", "status": rejected, "secondary": rejected}
    if scheduled:
        out = {**campaign, "kind": "scheduled", "status": scheduled, "secondary": scheduled}
        if schedule_start:
            out["scheduleStart"] = schedule_start
        return out
    status = review or str(campaign.get("secondary") or "pending")
    return {**campaign, "kind": "pending", "status": status, "secondary": status}


def read_white_hat_status_api(
    token: str,
    advertiser_id: str,
    *,
    campaign_name: str = "whitehat",
) -> dict[str, Any]:
    if not token or not advertiser_id:
        return {"found": False, "kind": "pending", "status": "", "error": "no_token"}
    try:
        rows = list_advertiser_campaigns(token, advertiser_id)
    except TikTokApiError as exc:
        return {"found": False, "kind": "pending", "status": "", "error": str(exc)}
    parsed = white_hat_from_campaigns(rows, campaign_name=campaign_name)
    campaign_id = str(parsed.get("campaignId") or "")
    if not parsed.get("found") or not campaign_id:
        return parsed
    try:
        groups = list_campaign_adgroups(token, advertiser_id, campaign_id)
    except TikTokApiError:
        return parsed
    return refine_white_hat_status(parsed, groups)


def token_for_advertiser(uid: str, profile_id: str, advertiser_id: str = "") -> str:
    """Profile token, else any connection that already includes this advertiser."""
    from .store import connection_token, iter_token_rows

    direct = connection_token(uid, profile_id) if profile_id else ""
    if direct:
        return direct
    want = str(advertiser_id or "").strip()
    for row in iter_token_rows(uid):
        token = str(row.get("accessToken") or "")
        if not token:
            continue
        ids = {str(item) for item in (row.get("advertiserIds") or [])}
        if want and want in ids:
            return token
        if profile_id and str(row.get("profileId") or "") == str(profile_id):
            return token
    return ""


def _campaign_has_adgroup(token: str, advertiser_id: str, campaign_id: str) -> bool:
    if not token or not advertiser_id or not campaign_id:
        return False
    try:
        data = tiktok_request(
            "GET",
            "/adgroup/get/",
            token,
            params={
                "advertiser_id": advertiser_id,
                "filtering": {"campaign_ids": [str(campaign_id)]},
                "page_size": 1,
            },
        )
    except TikTokApiError:
        return False
    return bool(data.get("list"))


def find_existing_campaign(token: str, advertiser_id: str, name: str) -> dict[str, Any] | None:
    if not token or not advertiser_id:
        return None
    try:
        data = tiktok_request(
            "GET",
            "/campaign/get/",
            token,
            params={"advertiser_id": advertiser_id, "filtering": {"campaign_name": name}, "page_size": 20},
        )
    except TikTokApiError:
        return None
    for row in data.get("list") or []:
        if isinstance(row, dict) and str(row.get("campaign_name") or "") == name:
            return {"campaign_id": row.get("campaign_id")}
    return None


def _saved(
    uid: str,
    profile: dict[str, Any],
    account: dict[str, Any],
    payload: dict[str, Any],
    created: dict[str, Any],
    posts: list[dict[str, Any]],
    identity: dict[str, Any],
    page_id: str,
    objective: str,
    *,
    landing: str = "",
    budget: float = 0,
    bid: float = 0,
) -> dict[str, Any]:
    campaign_id = str(created.get("campaign_id") or "")
    adgroup_id = str(created.get("adgroup_id") or "")
    ad_ids = [str(item) for item in (created.get("ad_ids") or [])]
    status = "published" if publish_enabled() else "draft"
    offer_ids = [str(item) for item in (payload.get("offerIds") or []) if str(item).strip()]
    offer_id = str(payload.get("offerId") or (offer_ids[0] if offer_ids else "") or "")
    from ..affiliate.service import record_launch_ads

    record_launch_ads(
        uid,
        profile=profile,
        account=account,
        campaign_id=campaign_id,
        adgroup_id=adgroup_id,
        posts=posts,
        ad_ids=ad_ids,
        offer_id=offer_id,
        identity=identity,
        objective=objective,
        landing=landing,
        budget=budget,
        bid=bid,
        page_id_fallback=page_id,
        account_number=str(payload.get("account") or "1"),
        bc_number=str(payload.get("trackingBc") or "1"),
        extra_campaign={
            "operationStatus": "ENABLE" if status == "published" else "DISABLE",
            "apiLaunchStep": status,
            "bcId": payload.get("bcId") or "",
            "hostId": profile.get("hostId") or payload.get("hostId") or "",
        },
    )
    return {
        "apiLaunchStep": status,
        "tiktokCampaignId": campaign_id,
        "tiktokAdgroupIds": [adgroup_id] if adgroup_id else [],
        "tiktokAdIds": ad_ids,
        "pageId": page_id,
        "salesObjective": objective,
        "salesBidUsd": bid,
        "salesBudgetUsd": budget,
    }
