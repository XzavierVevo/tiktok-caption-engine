"""Shared Create/Chat workspace."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import get_current_uid

router = APIRouter(tags=["workspace"], dependencies=[Depends(get_current_uid)])


class WorkspaceUpdate(BaseModel):
    captions: str | None = None
    preserveBreaks: bool | None = None
    forceLower: bool | None = None
    style: dict | None = None
    mode: str | None = None
    nameTemplate: str | None = None
    skipExisting: bool | None = None
    dedupe: bool | None = None
    endCaptionTemplate: dict | None = None
    strokePreset: str | None = None
    positionPreset: str | None = None
    renderPairs: list[dict] | None = None
    captionMeta: list[dict] | None = None
    offerId: str | None = None
    chatProvider: str | None = None
    chatModels: dict[str, str] | None = None
    createMode: str | None = None
    carouselCaptions: str | None = None
    carouselCaptionsByImage: dict[str, str] | None = None
    activePlanId: str | None = None


@router.get("/workspace")
def get_workspace(uid: str = Depends(get_current_uid)) -> dict:
    from .. import workspace as ws

    return ws.load_workspace(uid)


@router.put("/workspace")
def put_workspace(req: WorkspaceUpdate, uid: str = Depends(get_current_uid)) -> dict:
    from .. import workspace as ws

    return ws.save_workspace(uid, req.model_dump(exclude_unset=True))
