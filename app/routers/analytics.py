"""Catalog insight / click-tracker stubs (reserved columns)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_uid

router = APIRouter(tags=["analytics"], dependencies=[Depends(get_current_uid)])


@router.get("/facebook/insights")
def facebook_insights_stub(uid: str = Depends(get_current_uid)) -> dict:
    del uid
    raise HTTPException(
        501,
        "Facebook ads insights sync is not implemented yet. "
        "Catalog rows already reserve fb_spend / impressions / cpm / results.",
    )


@router.get("/tiktok/insights")
def tiktok_insights(fresh: int = 1, uid: str = Depends(get_current_uid)) -> dict:
    from ..tiktok_marketing.reports import sync_all
    from ..tiktok_marketing.store import list_campaigns

    if fresh:
        try:
            sync_all(uid)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "campaigns": list_campaigns(uid)}
    return {"ok": True, "campaigns": list_campaigns(uid)}


@router.post("/track/click")
def track_click_stub(uid: str = Depends(get_current_uid)) -> dict:
    del uid
    raise HTTPException(501, "Click tracker is not implemented yet.")
