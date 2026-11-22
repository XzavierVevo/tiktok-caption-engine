"""Read TikTok advertiser status and mark suspended Ads Managers.

TikTok regularly STATUS_LIMIT / punish accounts even on clean traffic.
This is a read of /advertiser/info/, not a guess.
"""

from __future__ import annotations

from typing import Any

from .client import TikTokApiError, tiktok_request
from .store import iter_token_rows

SUSPENDED_STATUSES = frozenset(
    {
        "STATUS_LIMIT",
        "STATUS_DISABLE",
        "STATUS_CONFIRM_FAIL",
        "STATUS_CONFIRM_FAIL_END",
        "STATUS_PUNISH",
    }
)


def is_suspended_status(status: str, *, secondary: str = "") -> bool:
    blob = f"{status or ''} {secondary or ''}".upper()
    if not blob.strip():
        return False
    token = str(status or "").strip().upper()
    if token in SUSPENDED_STATUSES:
        return True
    return "SUSPEND" in blob or "PUNISH" in blob or "STATUS_LIMIT" in blob


def is_available_status(status: str, *, secondary: str = "") -> bool:
    token = str(status or "").strip().upper()
    return token == "STATUS_ENABLE" and not is_suspended_status(token, secondary=secondary)


def read_advertisers(token: str, advertiser_ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(x).strip() for x in advertiser_ids if str(x).strip()]
    if not ids:
        return []
    data = tiktok_request(
        "GET",
        "/advertiser/info/",
        token,
        params={
            "advertiser_ids": ids[:100],
            "fields": [
                "advertiser_id",
                "status",
                "name",
                "rejection_reason",
                "balance",
                "currency",
            ],
        },
    )
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(data.get("list") or []):
        if not isinstance(row, dict):
            continue
        advertiser_id = str(row.get("advertiser_id") or row.get("id") or "").strip()
        if not advertiser_id and index < len(ids):
            advertiser_id = ids[index]
        if advertiser_id:
            row = {**row, "advertiser_id": advertiser_id}
        rows.append(row)
    return rows


def _identity_patch(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    patch = {
        k: row.get(k)
        for k in (
            "profileId",
            "adspowerProfileId",
            "profileNo",
            "bcName",
            "orgId",
            "accountName",
            "accountId",
        )
    }
    patch.update(extra)
    return patch


def sync_account_suspensions(uid: str) -> dict[str, Any]:
    """Write TikTok advertiser status onto every known Ads Manager row.

    STATUS_LIMIT / punish marks the account (not the whole BC). STATUS_ENABLE
    clears a previous TikTok mark and records the account as available.
    Manual suspension marks are left alone.
    """
    from ..provision import bc_inventory as bc_inv
    from ..provision.finance import accounts as acct_mod
    from ..provision.inventory import InventoryError

    checked = 0
    marked: list[dict[str, str]] = []
    already: list[str] = []
    cleared: list[str] = []
    recorded: list[str] = []
    errors: list[str] = []
    for conn in iter_token_rows(uid):
        token = str(conn.get("accessToken") or "")
        profile_id = str(conn.get("profileId") or "")
        ids = [str(x) for x in (conn.get("advertiserIds") or []) if str(x).strip()]
        if not token or not ids:
            continue
        try:
            rows = read_advertisers(token, ids)
        except TikTokApiError as exc:
            errors.append(str(exc))
            continue
        for row in rows:
            checked += 1
            advertiser_id = str(row.get("advertiser_id") or "")
            status = str(row.get("status") or "")
            reason = str(row.get("rejection_reason") or "").strip()
            if not advertiser_id or not status:
                continue
            available = is_available_status(status)
            suspended = is_suspended_status(status)
            try:
                found = acct_mod.find_accounts(uid=uid, account_id=advertiser_id)
                target = found[0] if found else None
                if target and suspended:
                    if str(target.get("suspensionStatus") or "") == "suspended":
                        if (
                            str(target.get("tiktokAdvertiserStatus") or "") != status
                            or target.get("advertiserAvailable") is not False
                        ):
                            acct_mod.upsert_account(
                                _identity_patch(
                                    target,
                                    tiktokAdvertiserStatus=status,
                                    advertiserAvailable=False,
                                ),
                                uid=uid,
                            )
                        already.append(advertiser_id)
                        continue
                    bc_inv.mark_account_suspended(
                        uid,
                        profile_id=str(target.get("profileId") or profile_id),
                        account_id=advertiser_id,
                        reason_code="policy",
                        reason=reason[:500],
                        how="tiktok",
                        tiktok_status=status,
                    )
                    marked.append({"advertiserId": advertiser_id, "status": status})
                    continue
                if target and available:
                    if (
                        str(target.get("suspensionStatus") or "") == "suspended"
                        and str(target.get("suspendedHow") or "") == "tiktok"
                    ):
                        bc_inv.clear_account_suspended(
                            uid,
                            profile_id=str(target.get("profileId") or profile_id),
                            account_id=advertiser_id,
                            tiktok_status=status,
                            advertiser_available=True,
                        )
                        cleared.append(advertiser_id)
                        continue
                    if (
                        str(target.get("tiktokAdvertiserStatus") or "") != status
                        or target.get("advertiserAvailable") is not True
                    ):
                        acct_mod.upsert_account(
                            _identity_patch(
                                target,
                                tiktokAdvertiserStatus=status,
                                advertiserAvailable=True,
                            ),
                            uid=uid,
                        )
                        recorded.append(advertiser_id)
                    continue
                if target:
                    acct_mod.upsert_account(
                        _identity_patch(
                            target,
                            tiktokAdvertiserStatus=status,
                            advertiserAvailable=False,
                        ),
                        uid=uid,
                    )
                    recorded.append(advertiser_id)
                    continue
                if not suspended:
                    continue
                if not profile_id:
                    errors.append(f"{advertiser_id}: not in inventory")
                    continue
                bc_inv.mark_profile_suspended(
                    uid,
                    profile_id,
                    reason_code="policy",
                    reason=reason[:500] or f"TikTok {status}",
                    how="tiktok",
                )
                marked.append(
                    {
                        "advertiserId": advertiser_id,
                        "profileId": profile_id,
                        "status": status,
                    }
                )
            except InventoryError as exc:
                errors.append(str(exc))
            except Exception as exc:
                errors.append(str(exc))
    return {
        "checked": checked,
        "marked": marked,
        "alreadySuspended": already,
        "cleared": cleared,
        "recorded": recorded,
        "errors": errors,
    }
