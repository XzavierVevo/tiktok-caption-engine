"""Connection, spark, and campaign documents. Tokens stay out of public JSON."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from .. import userstore
from ..provision.owner import owner_uid

_log = logging.getLogger(__name__)

CONNECTIONS_KV = "tiktok_marketing_connections"
IDENTITIES_KV = "tiktok_ad_identities"
SPARKS = "sparks"
CAMPAIGNS = "tiktok_campaigns"
REPORT_DAYS = "tiktok_report_days"
TOPUPS_KV = "provision_topups"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _uid(uid: str | None) -> str:
    return uid or owner_uid()


def doc_id_for_item(item_id: str) -> str:
    return hashlib.sha256(str(item_id or "").encode("utf-8")).hexdigest()[:24]


def public_connection(row: dict[str, Any] | None) -> dict[str, Any]:
    data = row if isinstance(row, dict) else {}
    return {
        "profileId": data.get("profileId") or "",
        "oauthStatus": data.get("oauthStatus") or "",
        "advertiserIds": list(data.get("advertiserIds") or []),
        "bcId": data.get("bcId") or "",
        "connectedAt": data.get("connectedAt") or "",
        "lastError": data.get("lastError") or "",
        "adspowerProfileId": data.get("adspowerProfileId") or "",
        "hostId": data.get("hostId") or "",
    }


def _connections(uid: str) -> dict[str, Any]:
    row = userstore.get_kv(uid, CONNECTIONS_KV)
    return dict(row) if isinstance(row, dict) else {}


def save_connection(uid: str | None, profile_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    key_uid = _uid(uid)
    pid = str(profile_id or "").strip()

    def mutator(cur: dict[str, Any]) -> dict[str, Any]:
        current = dict(cur or {})
        by_id = dict(current.get("byId") or {})
        row = dict(by_id.get(pid) or {})
        row.update(patch)
        row["profileId"] = pid
        row["updatedAt"] = _now()
        if str(row.get("oauthStatus") or "") == "connected":
            if not str(row.get("connectedAt") or "").strip():
                row["connectedAt"] = row["updatedAt"]
            row["lastError"] = str(patch.get("lastError") or "")
        by_id[pid] = row
        current["byId"] = by_id
        return current

    saved = userstore.patch_kv(key_uid, CONNECTIONS_KV, mutator)
    return public_connection((saved.get("byId") or {}).get(pid))


def get_connection(uid: str | None, profile_id: str) -> dict[str, Any]:
    pid = str(profile_id or "").strip()
    row = (_connections(_uid(uid)).get("byId") or {}).get(pid) or {}
    return dict(row) if isinstance(row, dict) else {}


def connection_token(uid: str | None, profile_id: str) -> str:
    return str(get_connection(uid, profile_id).get("accessToken") or "")


def iter_token_rows(uid: str | None) -> list[dict[str, Any]]:
    by_id = _connections(_uid(uid)).get("byId") or {}
    return [dict(row) for row in by_id.values() if isinstance(row, dict) and row.get("accessToken")]


def _row_when(row: dict[str, Any]) -> str:
    return str(row.get("updatedAt") or row.get("connectedAt") or "")


def merge_connections(
    base: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    """Union two connection documents, newest row wins, advertiser ids never drop."""
    out = dict(base or {})
    by_id = {
        str(pid): dict(row)
        for pid, row in ((base or {}).get("byId") or {}).items()
        if isinstance(row, dict)
    }
    for pid, row in ((incoming or {}).get("byId") or {}).items():
        if not isinstance(row, dict):
            continue
        pid = str(pid)
        current = by_id.get(pid)
        if current is None:
            by_id[pid] = dict(row)
            continue
        newer, older = (
            (dict(row), current)
            if _row_when(row) >= _row_when(current)
            else (dict(current), dict(row))
        )
        merged = {**older, **newer}
        ids: list[str] = []
        for item in list(current.get("advertiserIds") or []) + list(row.get("advertiserIds") or []):
            text = str(item).strip()
            if text and text not in ids:
                ids.append(text)
        merged["advertiserIds"] = ids
        token = str(newer.get("accessToken") or older.get("accessToken") or "")
        if token:
            merged["accessToken"] = token
            if (
                str(newer.get("oauthStatus") or "") == "connected"
                or str(older.get("oauthStatus") or "") == "connected"
            ):
                merged["oauthStatus"] = "connected"
                merged["lastError"] = ""
        by_id[pid] = merged
    out["byId"] = by_id
    return out


def mirror_connections(uid: str | None) -> None:
    """Mirror Marketing API connections to D1 when the local store is not D1.

    Local provisioning runs on Firestore while production reads D1. Without
    this mirror a token minted during a local BC warm-up never reaches the
    deployed Spark page, which then reports "No TikTok token".
    """
    from .. import cloudflare_store as cfstore

    key_uid = _uid(uid)
    if userstore._primary() == "d1" or not cfstore.enabled():
        return
    local = _connections(key_uid)
    if not local.get("byId"):
        return
    try:
        remote = cfstore.get(key_uid, "kv", CONNECTIONS_KV)
    except Exception:
        _log.warning("could not read D1 connections before mirror", exc_info=True)
        return
    if remote is cfstore.CF_ERROR:
        _log.warning("D1 connections read failed; skipping mirror")
        return
    if not isinstance(remote, dict):
        remote = {}
    merged = merge_connections(remote, local)
    try:
        cfstore.set_doc(key_uid, "kv", CONNECTIONS_KV, merged)
    except Exception:
        _log.warning("Marketing API connections mirror to D1 failed", exc_info=True)


def list_connections(uid: str | None) -> list[dict[str, Any]]:
    by_id = _connections(_uid(uid)).get("byId") or {}
    return [public_connection(row) for row in by_id.values() if isinstance(row, dict)]


def save_oauth_state(uid: str | None, profile_id: str, state: str) -> None:
    save_connection(uid, profile_id, {"oauthState": state, "oauthStatus": "pending"})


def consume_oauth_state(uid: str | None, state: str) -> str:
    """Return the profile id for a one-time state, then clear it."""
    key_uid = _uid(uid)
    found = ""

    def mutator(cur: dict[str, Any]) -> dict[str, Any]:
        nonlocal found
        current = dict(cur or {})
        by_id = dict(current.get("byId") or {})
        for pid, row in by_id.items():
            if isinstance(row, dict) and str(row.get("oauthState") or "") == state and state:
                found = str(pid)
                nxt = dict(row)
                nxt["oauthState"] = ""
                by_id[pid] = nxt
                break
        current["byId"] = by_id
        return current

    userstore.patch_kv(key_uid, CONNECTIONS_KV, mutator)
    return found


def upsert_campaign(uid: str | None, campaign_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    key_uid = _uid(uid)
    cid = str(campaign_id or "").strip()
    current = userstore.get_doc(key_uid, CAMPAIGNS, cid) or {}
    row = dict(current)
    row.update(patch)
    row["campaignId"] = cid
    row["updatedAt"] = _now()
    userstore.set_doc(key_uid, CAMPAIGNS, cid, row)
    return row


def get_campaign(uid: str | None, campaign_id: str) -> dict[str, Any]:
    row = userstore.get_doc(_uid(uid), CAMPAIGNS, str(campaign_id or "").strip())
    return dict(row) if isinstance(row, dict) else {}


def list_campaigns(uid: str | None) -> list[dict[str, Any]]:
    return userstore.list_docs(_uid(uid), CAMPAIGNS)


def spark_doc_id(ad_id: str = "", item_id: str = "") -> str:
    """One document per TikTok ad. Same video on two ads is two rows."""
    ad = str(ad_id or "").strip()
    if ad:
        return "ad-" + hashlib.sha256(ad.encode("utf-8")).hexdigest()[:24]
    return doc_id_for_item(item_id)


def _placement_ad_id(patch: dict[str, Any]) -> str:
    places = patch.get("placements")
    if isinstance(places, list) and len(places) == 1 and isinstance(places[0], dict):
        return str(places[0].get("adId") or "").strip()
    return ""


def upsert_spark(uid: str | None, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    key_uid = _uid(uid)
    data = dict(patch or {})
    ad_id = str(data.get("adId") or "").strip() or _placement_ad_id(data)
    doc_id = spark_doc_id(ad_id, item_id)
    current = userstore.get_doc(key_uid, SPARKS, doc_id) or {}
    row = dict(current)
    row.update({k: v for k, v in data.items() if k != "placements"})
    if item_id:
        row["itemId"] = str(item_id)
    if ad_id:
        row["adId"] = ad_id
    placements = list(row.get("placements") or [])
    incoming = data.get("placements")
    if isinstance(incoming, list):
        by_ad = {
            str(p.get("adId") or p.get("advertiserId") or i): p
            for i, p in enumerate(placements)
            if isinstance(p, dict)
        }
        for item in incoming:
            if isinstance(item, dict):
                by_ad[str(item.get("adId") or item.get("advertiserId") or len(by_ad))] = item
        placements = list(by_ad.values())
        row["placements"] = placements
    spend = impressions = clicks = conversions = 0.0
    for place in placements:
        if not isinstance(place, dict):
            continue
        spend += float(place.get("spend") or 0)
        impressions += float(place.get("impressions") or 0)
        clicks += float(place.get("clicks") or 0)
        conversions += float(place.get("conversions") or 0)
    row["totals"] = {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "conversions": conversions,
        "costPerConversion": (spend / conversions) if conversions else 0,
    }
    op = str(data.get("operationStatus") or "").strip()
    sec = str(data.get("secondaryStatus") or "").strip()
    if op or sec:
        prev = (
            str(current.get("operationStatus") or ""),
            str(current.get("secondaryStatus") or ""),
        )
        nxt = (op or prev[0], sec or prev[1])
        if nxt != prev:
            changed = _now()
            row["statusChangedAt"] = changed
            history = list(row.get("statusHistory") or [])
            history.append({"at": changed, "operationStatus": nxt[0], "secondaryStatus": nxt[1]})
            row["statusHistory"] = history[-10:]
            if nxt[0].upper() == "ENABLE" and "DELIVERY_OK" in nxt[1].upper():
                row.setdefault("approvedAt", changed)
            else:
                row.setdefault("stoppedAt", changed)
    row["updatedAt"] = _now()
    userstore.set_doc(key_uid, SPARKS, doc_id, row)
    return row


def list_sparks(uid: str | None) -> list[dict[str, Any]]:
    return userstore.list_docs(_uid(uid), SPARKS)


def get_spark(uid: str | None, item_id: str) -> dict[str, Any]:
    row = userstore.get_doc(_uid(uid), SPARKS, doc_id_for_item(item_id))
    return dict(row) if isinstance(row, dict) else {}


def get_spark_by_ad(uid: str | None, ad_id: str) -> dict[str, Any]:
    row = userstore.get_doc(_uid(uid), SPARKS, spark_doc_id(ad_id, ""))
    return dict(row) if isinstance(row, dict) else {}


def upsert_report_day(uid: str | None, day: str, advertiser_id: str, patch: dict[str, Any]) -> None:
    doc_id = f"{advertiser_id}-{day}"
    userstore.set_doc(_uid(uid), REPORT_DAYS, doc_id, {**patch, "day": day, "advertiserId": advertiser_id})


def save_topup(uid: str | None, topup_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    key_uid = _uid(uid)

    def mutator(cur: dict[str, Any]) -> dict[str, Any]:
        current = dict(cur or {})
        by_id = dict(current.get("byId") or {})
        row = dict(by_id.get(topup_id) or {})
        row.update(patch)
        row["id"] = topup_id
        row["updatedAt"] = _now()
        by_id[topup_id] = row
        current["byId"] = by_id
        return current

    saved = userstore.patch_kv(key_uid, TOPUPS_KV, mutator)
    return dict((saved.get("byId") or {}).get(topup_id) or {})


def list_topups(uid: str | None) -> list[dict[str, Any]]:
    row = userstore.get_kv(_uid(uid), TOPUPS_KV)
    by_id = (row or {}).get("byId") if isinstance(row, dict) else {}
    return [dict(item) for item in (by_id or {}).values() if isinstance(item, dict)]
