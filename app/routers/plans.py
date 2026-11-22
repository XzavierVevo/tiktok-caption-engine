"""Workspace-scoped plan CRUD for Create + Chat."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import get_current_uid
from .. import plans as plan_store

_log = logging.getLogger(__name__)
router = APIRouter(tags=["plans"], dependencies=[Depends(get_current_uid)])


class PlanCreate(BaseModel):
    name: str = ""
    renderPairs: list[dict[str, Any]] = Field(default_factory=list)
    selected: list[Any] = Field(default_factory=list)
    activate: bool = True


class PlanUpdate(BaseModel):
    name: str | None = None
    title: str | None = None
    renderPairs: list[dict[str, Any]] | None = None
    selected: list[Any] | None = None


@router.get("/plans")
def api_list_plans(uid: str = Depends(get_current_uid)) -> dict:
    start_time = time.time()

    result = plan_store.list_plans(uid)

    elapsed = time.time() - start_time
    if elapsed > 0.5:  # Log slow requests
        plan_count = len(result.get("plans", []))
        _log.warning(
            "Slow /plans request: uid=%s, count=%d, elapsed=%.2fs",
            uid[:8] if uid else "?",
            plan_count,
            elapsed
        )

    return result


@router.post("/plans")
def api_create_plan(req: PlanCreate, uid: str = Depends(get_current_uid)) -> dict:
    return plan_store.create_plan(
        uid,
        name=req.name,
        render_pairs=req.renderPairs,
        selected=req.selected,
        activate=req.activate,
    )


@router.get("/plans/{plan_id}")
def api_get_plan(plan_id: str, uid: str = Depends(get_current_uid)) -> dict:
    start_time = time.time()

    try:
        plan = plan_store.get_plan(uid, plan_id)

        elapsed = time.time() - start_time
        if elapsed > 1.0:  # Log slow requests
            pair_count = len(plan.get("renderPairs", []))
            _log.warning(
                "Slow /plans/%s request: pairs=%d, elapsed=%.2fs",
                plan_id[:8],
                pair_count,
                elapsed
            )

        return plan
    except KeyError:
        raise HTTPException(status_code=404, detail="Plan not found") from None


@router.put("/plans/{plan_id}")
def api_update_plan(plan_id: str, req: PlanUpdate, uid: str = Depends(get_current_uid)) -> dict:
    patch = req.model_dump(exclude_unset=True)
    try:
        return plan_store.update_plan(uid, plan_id, patch)
    except KeyError:
        raise HTTPException(status_code=404, detail="Plan not found") from None


@router.post("/plans/{plan_id}/activate")
def api_activate_plan(plan_id: str, uid: str = Depends(get_current_uid)) -> dict:
    try:
        return plan_store.activate_plan(uid, plan_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Plan not found") from None


@router.delete("/plans/{plan_id}")
def api_delete_plan(plan_id: str, uid: str = Depends(get_current_uid)) -> dict:
    try:
        return plan_store.delete_plan(uid, plan_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Plan not found") from None
