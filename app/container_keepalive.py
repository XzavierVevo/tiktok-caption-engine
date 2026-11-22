"""Keep the Cloudflare container awake only while a job is still running.

The Worker sleeps the 8 GiB instance 30 seconds after the last request.
This loop calls ``touch()`` on that timer while render, upload, provision,
or hook-generation work is in flight, and stays quiet the rest of the time.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)
_LIVE = ("running", "queued")


def _cloudflare() -> bool:
    return (os.environ.get("CLOUDFLARE") or "").strip() == "1"


def _endpoint() -> str:
    base = (
        os.environ.get("CLOUDFLARE_STATE_BASE_URL")
        or os.environ.get("PUBLIC_UI_ORIGIN")
        or ""
    ).strip().rstrip("/")
    key = (os.environ.get("INTERNAL_API_KEY") or "").strip()
    if not base or not key:
        return ""
    return base


def has_live_work() -> bool:
    """True when a background job still needs this process."""
    try:
        from . import hooks

        if hooks.has_running_generate_job():
            return True
    except Exception:
        _log.debug("hook job check failed", exc_info=True)
    try:
        from . import job_runtime
    except Exception:
        return False
    for runtime in (
        job_runtime.batches,
        job_runtime.uploads,
        job_runtime.facebook,
        job_runtime.sparks,
        job_runtime.provisions,
    ):
        try:
            jobs = runtime.values()
        except Exception:
            continue
        for job in jobs:
            if str(getattr(job, "status", "") or "") in _LIVE:
                return True
    return False


def touch_once() -> bool:
    """Ask the Worker to extend sleepAfter. Never starts a stopped container."""
    base = _endpoint()
    key = (os.environ.get("INTERNAL_API_KEY") or "").strip()
    if not base or not key:
        return False
    req = urllib.request.Request(
        f"{base}/_internal/container-touch",
        data=b"{}",
        method="POST",
        headers={
            "content-type": "application/json",
            "x-internal-api-key": key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            res.read()
            return 200 <= int(getattr(res, "status", 0) or 0) < 300
    except urllib.error.HTTPError as exc:
        _log.warning("container touch HTTP %s", exc.code)
        return False
    except Exception:
        _log.warning("container touch failed", exc_info=True)
        return False


async def keepalive_loop() -> None:
    """Every 15s, renew the container timer if work is still running."""
    import asyncio

    if not _cloudflare():
        return
    while True:
        await asyncio.sleep(15)
        if not has_live_work():
            continue
        await asyncio.to_thread(touch_once)
