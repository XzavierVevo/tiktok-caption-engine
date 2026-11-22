"""Spend, impressions, conversions, and cash balances."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .. import catalog
from .client import tiktok_request
from .store import list_campaigns, upsert_campaign, upsert_report_day, upsert_spark


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def read_bc_balance(token: str, bc_id: str) -> dict[str, Any]:
    return tiktok_request("GET", "/bc/balance/get/", token, params={"bc_id": str(bc_id)})


def read_advertiser_balance(token: str, bc_id: str, advertiser_id: str) -> dict[str, Any]:
    data = tiktok_request(
        "GET",
        "/advertiser/balance/get/",
        token,
        params={"bc_id": str(bc_id), "advertiser_id": str(advertiser_id)},
    )
    rows = data.get("list") or []
    for row in rows:
        if isinstance(row, dict) and str(row.get("advertiser_id") or "") == str(advertiser_id):
            return row
    return rows[0] if rows and isinstance(rows[0], dict) else data


def is_delivering(operation: str, secondary: str) -> bool:
    """True only while TikTok is actually serving the ad.

    A campaign switch left on ENABLE is not enough. Punished, paused, and
    out-of-budget accounts are not running.
    """
    op = str(operation or "").upper()
    sec = str(secondary or "").upper()
    if op not in {"", "ENABLE"}:
        return False
    if any(flag in sec for flag in ("PUNISH", "DISABLE", "BUDGET_EXCEED", "DELETE", "FROZEN")):
        return False
    return sec in {
        "CAMPAIGN_STATUS_ENABLE",
        "AD_STATUS_DELIVERY_OK",
        "ADGROUP_STATUS_DELIVERY_OK",
    }


def _iso_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return ""


def sync_campaign_statuses(
    uid: str | None,
    token: str,
    advertiser_id: str,
    campaign_id: str,
) -> dict[str, Any]:
    """Refresh ad status and create time without pulling a report.

    Suspended campaigns still get checked here, so they come back on their own
    when TikTok re-enables them. Spend is only pulled for running campaigns.
    """
    data = tiktok_request(
        "GET",
        "/ad/get/",
        token,
        params={
            "advertiser_id": str(advertiser_id),
            "filtering": {"campaign_ids": [str(campaign_id)]},
            "page_size": 100,
        },
    )
    updated = 0
    for ad in data.get("list") or []:
        if not isinstance(ad, dict):
            continue
        smart_id = str(ad.get("smart_plus_ad_id") or "")
        if not smart_id:
            continue
        item_id = str(ad.get("tiktok_item_id") or "")
        operation = str(ad.get("operation_status") or "")
        secondary = str(ad.get("secondary_status") or "")
        patch = {
            "adId": smart_id,
            "itemId": item_id,
            "adName": " ".join(str(ad.get("ad_name") or "").split()),
            "campaignId": str(ad.get("campaign_id") or campaign_id),
            "advertiserId": str(ad.get("advertiser_id") or advertiser_id),
            "operationStatus": operation,
            "secondaryStatus": secondary,
            "running": is_delivering(operation, secondary),
            "tiktokError": "",
        }
        created = _iso_time(ad.get("create_time"))
        if created:
            patch["createTime"] = created
        upsert_spark(uid, item_id or smart_id, patch)
        updated += 1
    return {"ads": len(data.get("list") or []), "updated": updated}


def sync_smart_plus_campaign(uid: str, token: str, advertiser_id: str, campaign_id: str) -> dict[str, Any]:
    """Spend is reported on auction ad_id. Smart+ rows are keyed by smart_plus_ad_id."""
    ads = tiktok_request(
        "GET",
        "/ad/get/",
        token,
        params={
            "advertiser_id": str(advertiser_id),
            "filtering": {"campaign_ids": [str(campaign_id)]},
            "page_size": 100,
        },
    )
    by_auction: dict[str, dict[str, Any]] = {}
    for ad in ads.get("list") or []:
        if not isinstance(ad, dict):
            continue
        auction_id = str(ad.get("ad_id") or "")
        if auction_id:
            by_auction[auction_id] = ad
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    metrics_by_auction: dict[str, dict[str, Any]] = {}
    page = 1
    while page <= 10:
        report = tiktok_request(
            "GET",
            "/report/integrated/get/",
            token,
            params={
                "advertiser_id": str(advertiser_id),
                "report_type": "BASIC",
                "data_level": "AUCTION_AD",
                "dimensions": ["ad_id"],
                "metrics": ["spend", "impressions", "clicks", "conversion", "cost_per_conversion"],
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page": page,
                "page_size": 100,
            },
        )
        for item in report.get("list") or []:
            if not isinstance(item, dict):
                continue
            auction_id = str((item.get("dimensions") or {}).get("ad_id") or "")
            if auction_id in by_auction:
                metrics_by_auction[auction_id] = item.get("metrics") or {}
        info = report.get("page_info") or {}
        total_page = int(info.get("total_page") or 1)
        if page >= total_page:
            break
        page += 1
    updated = 0
    for auction_id, ad in by_auction.items():
        metrics = metrics_by_auction.get(auction_id) or {}
        smart_id = str(ad.get("smart_plus_ad_id") or auction_id)
        spend = _float(metrics.get("spend"))
        conversions = _float(metrics.get("conversion"))
        created = str(ad.get("create_time") or "")
        operation = str(ad.get("operation_status") or "")
        secondary = str(ad.get("secondary_status") or "")
        upsert_spark(
            uid,
            str(ad.get("tiktok_item_id") or smart_id),
            {
                "adId": smart_id,
                "reportAdId": auction_id,
                "adName": " ".join(str(ad.get("ad_name") or "").split()),
                "itemId": str(ad.get("tiktok_item_id") or ""),
                "launchDate": created[:10],
                "campaignId": str(campaign_id),
                "advertiserId": str(advertiser_id),
                "operationStatus": operation,
                "secondaryStatus": secondary,
                "running": is_delivering(operation, secondary),
                "tiktokError": "",
                "placements": [{
                    "adId": smart_id,
                    "reportAdId": auction_id,
                    "advertiserId": str(advertiser_id),
                    "campaignId": str(campaign_id),
                    "spend": spend,
                    "impressions": _float(metrics.get("impressions")),
                    "clicks": _float(metrics.get("clicks")),
                    "conversions": conversions,
                    "costPerConversion": _float(metrics.get("cost_per_conversion")) or ((spend / conversions) if conversions else 0),
                }],
            },
        )
        updated += 1
    return {"ads": len(by_auction), "updated": updated}


def sync_advertiser(uid: str, token: str, advertiser_id: str) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    data = tiktok_request(
        "GET",
        "/report/integrated/get/",
        token,
        params={
            "advertiser_id": str(advertiser_id),
            "report_type": "BASIC",
            "data_level": "AUCTION_AD",
            "dimensions": ["ad_id"],
            "metrics": ["spend", "impressions", "clicks", "conversion", "cost_per_conversion"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "page": 1,
            "page_size": 1000,
        },
    )
    rows = []
    for item in data.get("list") or []:
        if not isinstance(item, dict):
            continue
        metrics = item.get("metrics") or {}
        dimensions = item.get("dimensions") or {}
        spend = _float(metrics.get("spend"))
        conversions = _float(metrics.get("conversion"))
        row = {
            "adId": str(dimensions.get("ad_id") or metrics.get("ad_id") or ""),
            "spend": spend,
            "impressions": _float(metrics.get("impressions")),
            "clicks": _float(metrics.get("clicks")),
            "conversions": conversions,
            "costPerConversion": _float(metrics.get("cost_per_conversion")) or ((spend / conversions) if conversions else 0),
        }
        rows.append(row)
    upsert_report_day(uid, end.isoformat(), advertiser_id, {"rows": rows})
    _apply_rows(uid, advertiser_id, rows)
    return rows


def _apply_rows(uid: str, advertiser_id: str, rows: list[dict[str, Any]]) -> None:
    by_ad = {row["adId"]: row for row in rows if row.get("adId")}
    campaigns = [
        campaign
        for campaign in list_campaigns(uid)
        if str(campaign.get("advertiserId") or "") == str(advertiser_id)
    ]
    ad_campaign: dict[str, dict[str, Any]] = {}
    for campaign in campaigns:
        for ad_id in campaign.get("adIds") or []:
            ad_campaign[str(ad_id)] = campaign
        for post in campaign.get("posts") or []:
            if isinstance(post, dict) and post.get("adId"):
                ad_campaign[str(post.get("adId"))] = campaign
    touched: dict[str, dict[str, float]] = {}
    for ad_id, metrics in by_ad.items():
        campaign = ad_campaign.get(str(ad_id)) or {}
        if not campaign:
            continue
        item_id = ""
        for post in campaign.get("posts") or []:
            if isinstance(post, dict) and str(post.get("adId") or "") == str(ad_id):
                item_id = str(post.get("itemId") or "")
        upsert_spark(
            uid,
            item_id or str(ad_id),
            {
                "adId": str(ad_id),
                "itemId": item_id,
                "campaignId": campaign.get("campaignId") or "",
                "advertiserId": advertiser_id,
                "placements": [{
                    "advertiserId": advertiser_id,
                    "campaignId": campaign.get("campaignId"),
                    "adId": ad_id,
                    "profileId": campaign.get("profileId") or "",
                    "spend": metrics["spend"],
                    "impressions": metrics["impressions"],
                    "clicks": metrics.get("clicks") or 0,
                    "conversions": metrics["conversions"],
                    "costPerConversion": metrics["costPerConversion"],
                }],
            },
        )
        bucket = touched.setdefault(
            str(campaign.get("campaignId") or ""),
            {"spend": 0.0, "impressions": 0.0, "conversions": 0.0},
        )
        bucket["spend"] += metrics["spend"]
        bucket["impressions"] += metrics["impressions"]
        bucket["conversions"] += metrics["conversions"]
        output_rel = str(campaign.get("outputRel") or "")
        if output_rel:
            catalog.update_publish(
                uid,
                output_rel,
                {
                    "tt_spend": metrics["spend"],
                    "tt_impressions": metrics["impressions"],
                    "tt_results": metrics["conversions"],
                    "tt_cost_per_result": metrics["costPerConversion"],
                    "tt_insights_synced_at": end_now(),
                },
            )
    for campaign in campaigns:
        cid = str(campaign.get("campaignId") or "")
        if not cid:
            continue
        bucket = touched.get(cid) or {"spend": 0.0, "impressions": 0.0, "conversions": 0.0}
        spend = bucket["spend"]
        conversions = bucket["conversions"]
        upsert_campaign(
            uid,
            cid,
            {
                "reportSpend": spend,
                "reportImpressions": bucket["impressions"],
                "reportConversions": conversions,
                "reportCostPerConversion": (spend / conversions) if conversions else 0,
                "reportSyncedAt": end_now(),
            },
        )


def end_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def rollup_spark(placements: list[dict[str, Any]]) -> dict[str, float]:
    spend = sum(float(row.get("spend") or 0) for row in placements)
    impressions = sum(float(row.get("impressions") or 0) for row in placements)
    conversions = sum(float(row.get("conversions") or 0) for row in placements)
    return {
        "spend": spend,
        "impressions": impressions,
        "conversions": conversions,
        "costPerConversion": (spend / conversions) if conversions else 0,
    }


def sync_all(uid: str) -> dict[str, Any]:
    from .health import sync_account_suspensions
    from .store import iter_token_rows

    errors: list[str] = []
    synced = 0
    try:
        suspension = sync_account_suspensions(uid)
    except Exception as exc:
        suspension = {"errors": [str(exc)]}
        errors.append(str(exc))
    for row in iter_token_rows(uid):
        token = str(row.get("accessToken") or "")
        for advertiser_id in row.get("advertiserIds") or []:
            try:
                sync_advertiser(uid, token, str(advertiser_id))
                synced += 1
            except Exception as exc:
                errors.append(str(exc))
    return {
        "synced": synced,
        "errors": errors,
        "campaigns": len(list_campaigns(uid)),
        "suspensions": suspension,
    }
