"""User-scoped observability and controls for centrally scheduled work."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Literal

from ..auth import get_current_uid
from ..orchestration.client import Client, SchedulerUnavailable, enabled

router = APIRouter()


def _public_snapshot(snapshot: dict) -> dict:
    """Keep coordinator reconciliation tokens out of browser responses."""
    return {
        **snapshot,
        "tasks": [
            {key: value for key, value in task.items() if key != "token"}
            for task in snapshot.get("tasks", [])
        ],
    }


_STEP_PURPOSE = {
    "deactivate_ad_accounts": ("Deactivate ad accounts", "Close every ad account after Business Center disapproval"),
    "deactivate_business_center": ("Deactivate Business Center", "Close the Business Center after its ad accounts are deactivated"),
    "white_hat": ("Launch white-hat ads", "Run the scheduled white-hat campaign"),
    "monitor_white_hat_pending": ("Check white-hat approval", "Check whether the scheduled campaign is approved"),
    "sales_campaign": ("Launch sales campaign", "Create sales ads for this Business Center"),
    "setup_bc": ("Set up Business Center", "Configure billing and ad accounts"),
    "create_profile": ("Create AdsPower profile", "Prepare the browser profile for this Business Center"),
    "open_ads_manager": ("Open Ads Manager", "Open this profile's TikTok Ads Manager"),
}


def _describe_workflows(uid: str, snapshot: dict) -> dict:
    """Attach the legacy step's identity and purpose to scheduler grants."""
    from ..provision import inventory as inv

    tasks_by_workflow = {}
    for task in snapshot.get("tasks", []):
        tasks_by_workflow.setdefault(str(task.get("workflow") or ""), []).append(task)
    workflows = []
    for workflow in snapshot.get("workflows", []):
        row = dict(workflow)
        if row.get("state") not in ("active", "paused"):
            workflows.append(row)
            continue
        tasks = tasks_by_workflow.get(str(row.get("id") or ""), [])
        step = inv.get_step(uid, str(tasks[0].get("id") or "")) if tasks else None
        if step:
            kind = str(step.get("kind") or "")
            profile_id = str(step.get("profileId") or "")
            profile = inv.get_profile(uid, profile_id) or {}
            title, purpose = _STEP_PURPOSE.get(kind, (kind.replace("_", " ").title(), "Business Center worker step"))
            row.update(
                kind=kind,
                title=title,
                purpose=purpose,
                profileId=profile_id,
                profileLabel=str(profile.get("profileNo") or profile.get("bcName") or profile_id),
                stepStatus=str(step.get("status") or ""),
                # A coordinator workflow can exist from an admission attempt
                # that timed out before the browser received its grant. Do not
                # present that orphan as a running workflow in the dock.
                admitted=str((step.get("orchestration") or {}).get("workflow") or "") == str(row.get("id") or ""),
            )
        workflows.append(row)
    return {**snapshot, "workflows": workflows}


class Control(BaseModel):
    action: Literal["pause", "resume", "cancel"]


@router.get("/operations")
def operations(uid: str = Depends(get_current_uid)) -> dict:
    if not enabled():
        return {"enabled": False, "workflows": [], "tasks": []}
    try:
        return {"enabled": True, **_describe_workflows(uid, _public_snapshot(Client().snapshot(uid)))}
    except SchedulerUnavailable as exc:
        raise HTTPException(503, "Central scheduler unavailable") from exc


@router.get("/operations/{workflow}")
def operation(workflow: str, uid: str = Depends(get_current_uid)) -> dict:
    if not enabled():
        raise HTTPException(409, "Central scheduling is disabled")
    try:
        return _public_snapshot(Client().snapshot(uid, workflow))
    except SchedulerUnavailable as exc:
        raise HTTPException(503, "Workflow unavailable") from exc


@router.post("/operations/{workflow}/control")
def control(workflow: str, body: Control, uid: str = Depends(get_current_uid)) -> dict:
    if not enabled():
        raise HTTPException(409, "Central scheduling is disabled")
    try:
        return Client().control(uid, workflow, body.action)
    except SchedulerUnavailable as exc:
        raise HTTPException(503, "Workflow control unavailable") from exc

@router.get("/operations/recovery/pending")
def recovery_pending(uid: str = Depends(get_current_uid)) -> dict:
    """List pending result evidence files that need projection retry."""
    from ..orchestration.provision_bridge import list_pending_evidence
    pending = list_pending_evidence(uid)
    return {"pending": pending, "count": len(pending)}


@router.post("/operations/recovery/retry")
def recovery_retry(uid: str = Depends(get_current_uid)) -> dict:
    """Retry legacy projection for any pending evidence files."""
    from ..orchestration.provision_bridge import retry_pending_projections
    return retry_pending_projections(uid)

class ReconcileBody(BaseModel):
    """Evidence to resolve a quarantined task outcome."""
    outcome: Literal["success", "failed", "cancelled"]
    evidence: str  # Human-readable verification description
    externalId: str | None = None


@router.get("/operations/quarantined")
def quarantined_tasks(uid: str = Depends(get_current_uid)) -> dict:
    """List tasks in reconciling/waiting_external state that need human verification."""
    if not enabled():
        return {"tasks": []}
    try:
        snap = Client().snapshot(uid)
        quarantined = [
            t for t in snap.get("tasks", [])
            if t.get("state") in ("reconciling", "waiting_external")
        ]
        return {"tasks": _public_snapshot({"tasks": quarantined})["tasks"]}
    except SchedulerUnavailable:
        return {"tasks": [], "error": "Scheduler unavailable"}


@router.post("/operations/{workflow}/{task}/reconcile")
def reconcile_task(
    workflow: str,
    task: str,
    body: ReconcileBody,
    uid: str = Depends(get_current_uid),
) -> dict:
    """Manually resolve a quarantined task with verified evidence.

    Only call this after verifying the actual outcome (e.g., checking if payment
    went through, BC was created, etc.). The evidence string should describe
    what was verified.
    """
    if not enabled():
        raise HTTPException(409, "Central scheduling is disabled")
    try:
        # Get the task to find its token
        snap = Client().snapshot(uid, workflow)
        tasks = [t for t in snap.get("tasks", []) if t.get("id") == task]
        if not tasks:
            raise HTTPException(404, f"Task {task} not found in workflow {workflow}")
        task_row = tasks[0]
        if task_row.get("state") not in ("reconciling", "waiting_external"):
            raise HTTPException(
                409,
                f"Task is in state '{task_row.get('state')}', not quarantined"
            )
        # The coordinator's reconcile needs the original token
        result = Client().call(
            "reconcile",
            tenant=uid,
            workflow=workflow,
            task=task,
            token=task_row.get("token"),
            externalId=task_row.get("external_id"),
            outcome={
                "type": body.outcome,
                "evidence": body.evidence,
            }
        )
        return {"ok": True, "result": result}
    except SchedulerUnavailable as exc:
        raise HTTPException(503, "Reconciliation unavailable") from exc
