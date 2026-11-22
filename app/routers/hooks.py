"""Affiliate hook generator routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import hooks
from ..auth import get_current_uid
from ..hooks import HooksError

router = APIRouter(tags=["hooks"], dependencies=[Depends(get_current_uid)])


@router.get("/hooks/offers")
def hooks_list_offers(uid: str = Depends(get_current_uid)) -> list[dict]:
    return hooks.list_offers(uid)


@router.post("/hooks/offers/sync-firestore")
def hooks_sync_offers_firestore(uid: str = Depends(get_current_uid)) -> dict:
    """Re-push local hooks_offers.json into the Firestore offers collection."""
    return hooks.sync_offers_to_firestore(uid)


@router.get("/hooks/offers/{offer_id}")
def hooks_get_offer(offer_id: str, uid: str = Depends(get_current_uid)) -> dict:
    try:
        return hooks.get_offer(uid, offer_id)
    except HooksError as exc:
        raise HTTPException(404, str(exc)) from exc


class HooksOfferCreate(BaseModel):
    name: str
    url: str | None = None
    notes: str | None = None
    payoutType: str | None = None
    payoutAmount: float | None = None
    payoutSharePercent: float | None = None
    payoutNote: str | None = None
    currency: str | None = None


@router.post("/hooks/offers")
def hooks_create_offer(req: HooksOfferCreate, uid: str = Depends(get_current_uid)) -> dict:
    try:
        return hooks.create_offer(
            uid,
            req.name,
            url=req.url,
            notes=req.notes,
            payout_type=req.payoutType,
            payout_amount=req.payoutAmount,
            payout_share_percent=req.payoutSharePercent,
            payout_note=req.payoutNote,
            currency=req.currency,
        )
    except HooksError as exc:
        raise HTTPException(400, str(exc)) from exc


class HooksOfferUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    notes: str | None = None
    payoutType: str | None = None
    payoutAmount: float | None = None
    payoutSharePercent: float | None = None
    payoutNote: str | None = None
    currency: str | None = None


@router.put("/hooks/offers/{offer_id}")
def hooks_update_offer(
    offer_id: str, req: HooksOfferUpdate, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return hooks.update_offer(
            uid,
            offer_id,
            name=req.name,
            url=req.url,
            notes=req.notes,
            payout_type=req.payoutType,
            payout_amount=req.payoutAmount,
            payout_share_percent=req.payoutSharePercent,
            payout_note=req.payoutNote,
            currency=req.currency,
        )
    except HooksError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status, str(exc)) from exc


class HooksRoutingUpdate(BaseModel):
    landers: list[dict] | None = None
    networks: list[dict] | None = None
    activeLanderId: str | None = None
    activeNetworkId: str | None = None


@router.put("/hooks/offers/{offer_id}/routing")
def hooks_set_routing(
    offer_id: str, req: HooksRoutingUpdate, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return hooks.set_offer_routing(
            uid,
            offer_id,
            landers=req.landers,
            networks=req.networks,
            active_lander_id=req.activeLanderId,
            active_network_id=req.activeNetworkId,
        )
    except HooksError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status, str(exc)) from exc


@router.delete("/hooks/offers/{offer_id}")
def hooks_delete_offer(offer_id: str, uid: str = Depends(get_current_uid)) -> dict:
    try:
        hooks.delete_offer(uid, offer_id)
    except HooksError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


class HooksWinnerCreate(BaseModel):
    text: str


@router.post("/hooks/offers/{offer_id}/winners")
def hooks_add_winner(
    offer_id: str, req: HooksWinnerCreate, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return hooks.add_winner(uid, offer_id, req.text)
    except HooksError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status, str(exc)) from exc


class HooksWinnerUpdate(BaseModel):
    text: str


@router.put("/hooks/offers/{offer_id}/winners/{winner_id}")
def hooks_update_winner(
    offer_id: str,
    winner_id: str,
    req: HooksWinnerUpdate,
    uid: str = Depends(get_current_uid),
) -> dict:
    try:
        return hooks.update_winner(uid, offer_id, winner_id, req.text)
    except HooksError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status, str(exc)) from exc


@router.delete("/hooks/offers/{offer_id}/winners/{winner_id}")
def hooks_delete_winner(
    offer_id: str, winner_id: str, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        hooks.delete_winner(uid, offer_id, winner_id)
    except HooksError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


class HooksLikeRequest(BaseModel):
    text: str


@router.post("/hooks/offers/{offer_id}/likes")
def hooks_add_like(
    offer_id: str, req: HooksLikeRequest, uid: str = Depends(get_current_uid)
) -> dict:
    try:
        return hooks.add_like(uid, offer_id, req.text)
    except HooksError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status, str(exc)) from exc


@router.delete("/hooks/offers/{offer_id}/likes")
def hooks_remove_like(
    offer_id: str,
    likeId: str | None = None,
    text: str | None = None,
    uid: str = Depends(get_current_uid),
) -> dict:
    try:
        hooks.remove_like(uid, offer_id, like_id=likeId, text=text)
    except HooksError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status, str(exc)) from exc
    return {"ok": True}


@router.get("/hooks/history")
def hooks_list_history(
    offerId: str | None = None, uid: str = Depends(get_current_uid)
) -> list[dict]:
    return hooks.list_history(uid, offer_id=offerId)


class HooksGenerateRequest(BaseModel):
    offerId: str
    count: int = 5
    mode: str = "variations"
    extraInstructions: str | None = None
    randomWords: bool = False


@router.post("/hooks/generate")
async def hooks_generate(
    req: HooksGenerateRequest, uid: str = Depends(get_current_uid)
) -> dict:
    """Start hook generation in a background thread (survives tab close)."""
    try:
        return hooks.start_generate_job(
            uid,
            req.offerId,
            req.count,
            req.mode,
            req.extraInstructions,
            random_words=req.randomWords,
        )
    except HooksError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status, str(exc)) from exc


@router.get("/hooks/generate/{job_id}")
def hooks_generate_status(job_id: str, uid: str = Depends(get_current_uid)) -> dict:
    job = hooks.get_generate_job(job_id, uid)
    if not job:
        raise HTTPException(404, "Hook generate job not found")
    return job
