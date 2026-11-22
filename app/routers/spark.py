"""DuoPlus spark callback ingress (NET_REQUEST from RPA workflow)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import get_current_uid

from .. import spark_ingress as ingress
from ..duoplus import extract_spark_code

router = APIRouter(tags=["spark"])


def _body_spark(body: Any, *, post_url: str = "") -> str:
    if isinstance(body, str):
        return extract_spark_code(body, post_url=post_url)
    if not isinstance(body, dict):
        return ""
    for key in (
        "sparkCode",
        "spark_code",
        "spark_output",
        "sparkOutput",
        "code",
        "content",
        "value",
    ):
        val = extract_spark_code(str(body.get(key) or ""), post_url=post_url)
        if val:
            return val
    return extract_spark_code(json.dumps(body, ensure_ascii=False), post_url=post_url)


@router.post("/spark/callback")
async def spark_callback(
    request: Request,
    token: str = Query("", description="Must match SPARK_CALLBACK_SECRET when set"),
    task: str = Query("", description="Scheduled task name (correlation id)"),
) -> dict:
    if not ingress.verify_callback_token(token):
        raise HTTPException(status_code=403, detail="Invalid spark callback token")

    raw = await request.body()
    post_url = ""
    spark = ""
    if raw:
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            body = json.loads(text) if text.startswith("{") else text
        except json.JSONDecodeError:
            body = text
        if isinstance(body, dict):
            post_url = str(body.get("postUrl") or body.get("post_url") or "").strip()
        spark = _body_spark(body, post_url=post_url)
        if not spark and text:
            spark = extract_spark_code(text, post_url=post_url)

    if not spark:
        raise HTTPException(
            status_code=400,
            detail="No spark code in body. Send JSON "
            '{"spark_code":"#…","post_url":"https://…"} or plain text.',
        )

    ingress.save_spark(
        spark_code=spark,
        task_name=task,
        post_url=post_url,
        source="http_callback",
    )
    return {"ok": True, "sparkCode": spark, "taskName": task, "postUrl": post_url}


@router.get("/spark/callback-url")
def spark_callback_url(
    task: str = Query("", description="Optional task name"),
    uid: str = Depends(get_current_uid),
) -> dict:
    del uid
    url = ingress.callback_url(task_name=task)
    # Never echo the secret in logs; the URL is only for the signed-in owner.
    return {
        "url": url,
        "secretRequired": ingress.secret_required(),
    }


@router.get("/spark/jobs/{job_id}")
def spark_job_status(job_id: str, uid: str = Depends(get_current_uid)) -> dict:
    from .. import job_runtime
    from .. import spark_jobs

    job = job_runtime.sparks.get(job_id, uid)
    if job is None:
        raise HTTPException(404, "Spark job not found")
    return spark_jobs.job_state(job)


@router.post("/spark/jobs/{job_id}/cancel")
def spark_job_cancel(job_id: str, uid: str = Depends(get_current_uid)) -> dict:
    from .. import spark_jobs

    job = spark_jobs.cancel_job(uid, job_id)
    if job is None:
        raise HTTPException(404, "Spark job not found")
    return spark_jobs.job_state(job)
