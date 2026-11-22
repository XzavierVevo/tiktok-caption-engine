"""TikTok Caption Tool — FastAPI backend (Version C, multi-user cloud).

Every /api route requires a Firebase ID token (Authorization: Bearer …, or
?auth= for media/download links). Each user's sources, renders, TikTok
accounts, and settings are isolated under their uid and mirrored to R2.

Run locally:  ALLOW_DEV_NO_AUTH=1 uvicorn app.main:app --port 8765
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import cleanup
from . import facebook_ads
from . import storage
from .auth import ALLOW_DEV_NO_AUTH, FIREBASE_PROJECT_ID, FIREBASE_WEB_CONFIG, get_current_uid
from .config import (
    BASE_DIR,
    CAPTION_MODES,
    CAPTION_STYLE,
    FFMPEG_PATH,
    FFPROBE_PATH,
    FONTS_DIR,
    MAX_BATCH_JOBS,
    MAX_CONCURRENT_BATCHES_PER_USER,
    MAX_CONCURRENT_UPLOADS_PER_USER,
    MAX_UPLOAD_MB,
    POSITION_PRESETS,
    RETENTION_DAYS,
    SOFT_JOB_CAP,
    STATIC_DIR,
    STYLE_PRESETS,
    TIKTOK_UPLOAD_SECONDS,
    TIKTOK_HTTP_UPLOAD_SECONDS,
    user_ytdlp_cookies_path,
)
from .encode import nvenc_available

if os.environ.get("LOG_JSON") == "1":
    # Cloudflare Workers Logs ingests one JSON object per line.
    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            entry = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                entry["exc"] = self.formatException(record.exc_info)
            return json.dumps(entry, ensure_ascii=False)

    _handler = logging.StreamHandler()
    _handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[_handler])
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _install_quiet_asyncio_handler() -> None:
    """Silence Windows Proactor RST noise (WinError 10054) on client disconnect.

    Browsers and the provision worker (``Connection: close``) often abort sockets
    before uvicorn finishes ``shutdown()``. asyncio logs that as ERROR even though
    the request already succeeded — drown out real failures.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    previous = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:  # noqa: A002
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        if isinstance(exc, asyncio.CancelledError):
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10054, 10053, 10038):
            return
        msg = str(context.get("message") or "")
        if "_call_connection_lost" in msg and isinstance(exc, OSError):
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def _stop_embedded_worker_quiet() -> None:
    try:
        from . import provision_embed

        provision_embed.stop_embedded_worker()
    except Exception:
        pass


def _chain_signal_to_stop_worker() -> None:
    """Stop the embedded worker as soon as uvicorn gets SIGTERM/SIGINT (reload).

    Lifespan cleanup runs only *after* graceful connection drain — too late when
    the worker keeps opening /worker/next sockets that pin shutdown forever.
    """
    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev = signal.getsignal(sig)
        except Exception:
            continue

        def _handler(signum, frame, _prev=prev):  # noqa: ANN001
            _stop_embedded_worker_quiet()
            if callable(_prev) and _prev not in (signal.SIG_DFL, signal.SIG_IGN):
                _prev(signum, frame)

        try:
            signal.signal(sig, _handler)
        except Exception:
            pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start optional HTTPS listener + TikTok signature sidecar + provision worker."""
    _install_quiet_asyncio_handler()
    try:
        import anyio

        anyio.to_thread.current_default_thread_limiter().total_tokens = 32
    except Exception:
        logging.getLogger(__name__).debug("anyio thread limiter not set", exc_info=True)
    task = None
    extra = (os.environ.get("LOCAL_HTTPS_PORT") or "").strip()
    cert = BASE_DIR / "certs" / "localhost-cert.pem"
    key = BASE_DIR / "certs" / "localhost-key.pem"
    if extra and cert.is_file() and key.is_file():
        import uvicorn

        port = int(extra)
        server = uvicorn.Server(
            uvicorn.Config(
                _app,
                host="127.0.0.1",
                port=port,
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                log_level="warning",
                lifespan="off",
            )
        )
        task = asyncio.create_task(server.serve())
        logging.getLogger(__name__).info(
            "Extra HTTPS listener on https://localhost:%s",
            port,
        )

    # HTTP upload engine signer (Node). Best-effort — Studio engine still works.
    # Never block container listen on sidecar startup (Cloudflare port probe).
    try:
        from .tiktok_engines import signer as tt_signer

        if tt_signer.is_enabled():
            started = await asyncio.wait_for(
                asyncio.to_thread(tt_signer.start_sidecar),
                timeout=20,
            )
            logging.getLogger(__name__).info(
                "tiktok-signature sidecar start=%s health=%s",
                started,
                tt_signer.health(),
            )
    except asyncio.TimeoutError:
        logging.getLogger(__name__).warning(
            "tiktok-signature sidecar start timed out; continuing without it"
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "tiktok-signature sidecar not started", exc_info=True
        )

    # Uvicorn installs its handlers after lifespan startup; re-chain shortly after.
    async def _rechain() -> None:
        await asyncio.sleep(0.25)
        _chain_signal_to_stop_worker()

    # Defer worker setup until AFTER the accept loop is up.
    # Starting them inside lifespan startup lets /worker/next pile up before the
    # server can answer /healthz — dual uvicorn + orphan workers then wedge 8765
    # (CLOSE_WAIT storm, browser ERR_CONNECTION_TIMED_OUT on localhost).
    async def _post_listen_boot() -> None:
        await asyncio.sleep(0.4)
        # Ephemeral container /tmp: pull owner BC kv from Firestore into local
        # _fs. Do NOT call build_inventory here — that can block on AdsPower /
        # overlays and turn Ctrl+C into a scary uvicorn ERROR traceback.
        try:
            owner = (os.environ.get("PROVISION_OWNER_UID") or "").strip()
            if owner:

                def _hydrate_owner_kv() -> None:
                    from . import userstore

                    for key in (
                        "provision_profiles",
                        "provision_ad_accounts",
                        "provision_spend",
                        "provision_geos",
                        "provision_worker",
                        "provision_hosts",
                        "provision_finance",
                        "provision_steps",
                    ):
                        try:
                            # Prefer the R2/local mirror. On an ephemeral
                            # container this avoids an eight-document
                            # Firestore burst at boot. A regular inventory
                            # request still uses its normal canonical refresh
                            # if this container has no mirror.
                            userstore.get_kv_local_first(
                                owner, key, refresh=False, fallback_to_remote=False
                            )
                        except Exception:
                            logging.getLogger(__name__).debug(
                                "hydrate kv %s failed", key, exc_info=True
                            )

                await asyncio.wait_for(
                    asyncio.to_thread(_hydrate_owner_kv),
                    timeout=30,
                )
                logging.getLogger(__name__).info(
                    "completed owner kv mirror bootstrap uid=%s", owner[:8]
                )
                try:
                    from .edge_cache import publish_activity

                    await asyncio.to_thread(
                        lambda: publish_activity(owner, force=True)
                    )
                except Exception:
                    logging.getLogger(__name__).debug(
                        "edge activity bootstrap failed", exc_info=True
                    )
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning(
                "owner kv hydrate timed out; first /inventory may be slower"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning(
                "owner kv hydrate failed", exc_info=True
            )
        try:
            from . import provision_jobs

            stopped = await asyncio.to_thread(provision_jobs.stop_orphaned_jobs)
            if stopped:
                logging.getLogger(__name__).info(
                    "stopped %s orphaned provision job(s) on startup: %s",
                    len(stopped),
                    ",".join(stopped),
                )
            resumed = await asyncio.to_thread(
                provision_jobs.resume_orphaned_jobs, statuses=("queued",)
            )
            if resumed:
                logging.getLogger(__name__).info(
                    "restored %s queued provision job(s) after startup", len(resumed)
                )
        except Exception:
            logging.getLogger(__name__).warning(
                "orphaned provision job stop failed", exc_info=True
            )
        # Stop in-flight jobs before the worker can reclaim their browser steps.
        try:
            from . import provision_embed

            await asyncio.to_thread(provision_embed.start_embedded_worker)
        except Exception:
            logging.getLogger(__name__).warning(
                "embedded provision worker not started", exc_info=True
            )
        try:
            from . import job_runtime

            def _fail_orphans() -> None:
                for rt in (
                    job_runtime.batches,
                    job_runtime.uploads,
                    job_runtime.facebook,
                    job_runtime.sparks,
                ):
                    n = rt.fail_orphaned()
                    if n:
                        logging.getLogger(__name__).info(
                            "marked %s orphaned %s job(s) failed",
                            n,
                            rt._collection,
                        )
                # Workspace plans read Spark state from the render catalog.
                # Repair stale per-item catalog flags after the envelopes above
                # have been failed, otherwise a restart leaves Fetching badges.
                from . import spark_jobs

                from .config import BASE_DIR

                users_root = BASE_DIR / "data" / "users"
                if users_root.is_dir():
                    for entry in users_root.iterdir():
                        if not entry.is_dir() or entry.name.startswith("_"):
                            continue
                        repaired = spark_jobs.repair_terminal_snapshots(entry.name)
                        if repaired:
                            logging.getLogger(__name__).info(
                                "repaired %s stale Spark catalog row(s)", repaired
                            )

            await asyncio.to_thread(_fail_orphans)
        except Exception:
            logging.getLogger(__name__).warning(
                "orphan job fail-over failed", exc_info=True
            )
        # Recover any pending orchestration evidence files from crash/split-brain
        try:
            from .orchestration.client import enabled as orchestration_enabled
            from .orchestration.provision_bridge import retry_pending_projections
            from .config import BASE_DIR

            if orchestration_enabled():
                users_root = BASE_DIR / "data" / "users"
                if users_root.is_dir():
                    for entry in users_root.iterdir():
                        if not entry.is_dir() or entry.name.startswith("_"):
                            continue
                        result = retry_pending_projections(entry.name)
                        if result.get("total", 0) > 0:
                            logging.getLogger(__name__).info(
                                "recovered %d/%d pending orchestration projections for %s",
                                result.get("succeeded", 0),
                                result.get("total", 0),
                                entry.name[:8] + "...",
                            )
        except Exception:
            logging.getLogger(__name__).warning(
                "orchestration evidence recovery failed", exc_info=True
            )
        # Start hot reload watcher and resume any work from previous reload
        try:
            from . import hot_reload

            hot_reload.resume_after_reload()
            hot_reload.start_watcher()
            logging.getLogger(__name__).info("Hot reload watcher started")
        except Exception:
            logging.getLogger(__name__).warning(
                "Hot reload watcher not started", exc_info=True
            )

    rechain_task = asyncio.create_task(_rechain())
    boot_task = asyncio.create_task(_post_listen_boot())

    async def _supervise_provision_worker() -> None:
        """Bring back the local browser worker if its child process exits.

        A step failure is handled inside the worker, but process exits (for
        example a browser/runtime crash) used to leave the bot offline until the
        launcher was invoked again.  ``supervise_embedded_worker`` limits retry
        storms and records the exit code for diagnosis.
        """
        while True:
            await asyncio.sleep(5)
            try:
                from . import provision_embed

                await asyncio.to_thread(provision_embed.supervise_embedded_worker)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "embedded provision worker supervision failed", exc_info=True
                )

    worker_supervisor_task = asyncio.create_task(_supervise_provision_worker())

    async def _container_keepalive() -> None:
        from .container_keepalive import keepalive_loop

        await keepalive_loop()

    keepalive_task = asyncio.create_task(_container_keepalive())

    try:
        yield
    finally:
        for t in (keepalive_task, worker_supervisor_task, boot_task, rechain_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        # Stop worker first so it cannot open new connections during drain.
        _stop_embedded_worker_quiet()
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            from .tiktok_engines import signer as tt_signer

            tt_signer.stop_sidecar()
        except Exception:
            pass


app = FastAPI(title="TikTok Caption Tool", lifespan=_lifespan)
api = APIRouter(prefix="/api")


@app.middleware("http")
async def _localhost_hsts(request, call_next):
    """Teach Chrome to upgrade http://localhost → https after first good visit."""
    response = await call_next(request)
    host = (request.url.hostname or "").lower()
    if host in {"localhost", "127.0.0.1"} and request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=86400; includeSubDomains"
        )
    return response


# ---------------------------------------------------------------------------
# Public endpoints (no auth): health + auth config for the login screen
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    from . import userstore

    return {
        "ok": bool(FFMPEG_PATH and FFPROBE_PATH) and storage.health(),
        "ffmpeg": bool(FFMPEG_PATH),
        "storage": storage.health(),
        "userstore": userstore.store_status(),
    }


@app.post("/ensure-worker")
def ensure_worker(request: Request) -> dict:
    """Localhost-only: respawn the embedded provision worker if it died.

    ``start.bat`` calls this when the API is already healthy so a second click
    still brings AdsPower automation back online (opens profiles / BC tabs).
    """
    client = (request.client.host if request.client else "") or ""
    if client not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost only")
    from . import provision_embed

    proc = provision_embed.ensure_embedded_worker()
    status = provision_embed.embedded_status()
    return {
        "ok": bool(status.get("running")),
        "started": bool(proc is not None),
        **status,
    }


@app.get("/hot-reload/status")
def hot_reload_status(request: Request) -> dict:
    """Localhost-only: check if hot reload is pending and get active work count."""
    client = (request.client.host if request.client else "") or ""
    if client not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost only")
    from . import hot_reload

    return {
        "pending": hot_reload.is_reload_pending(),
        "activeWorkflows": hot_reload._count_active_workflows(),
        "activeSteps": hot_reload._count_active_steps(),
    }


@app.post("/hot-reload/trigger")
def hot_reload_trigger(request: Request) -> dict:
    """Localhost-only: manually trigger a hot reload if no work is active."""
    client = (request.client.host if request.client else "") or ""
    if client not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "localhost only")
    from . import hot_reload

    workflows = hot_reload._count_active_workflows()
    steps = hot_reload._count_active_steps()

    if workflows > 0 or steps > 0:
        return {
            "ok": False,
            "reason": "active_work",
            "activeWorkflows": workflows,
            "activeSteps": steps,
        }

    hot_reload._trigger_reload()
    return {"ok": True, "reloading": True}


@app.get("/api/auth-config")
def auth_config() -> dict:
    return {
        "mode": "firebase" if FIREBASE_PROJECT_ID else ("dev" if ALLOW_DEV_NO_AUTH else "unconfigured"),
        "firebase": FIREBASE_WEB_CONFIG,
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@api.get("/config")
def get_config(uid: str = Depends(get_current_uid)) -> dict:
    from . import chat_models

    return {
        "captionStyle": CAPTION_STYLE,
        "captionModes": CAPTION_MODES,
        "stylePresets": STYLE_PRESETS,
        "positionPresets": POSITION_PRESETS,
        "softJobCap": SOFT_JOB_CAP,
        "maxBatchJobs": MAX_BATCH_JOBS,
        "maxConcurrentBatches": MAX_CONCURRENT_BATCHES_PER_USER,
        "maxConcurrentUploads": MAX_CONCURRENT_UPLOADS_PER_USER,
        "maxUploadMb": MAX_UPLOAD_MB,
        "retentionDays": RETENTION_DAYS,
        "tiktokUploadSeconds": TIKTOK_UPLOAD_SECONDS,
        "tiktokHttpUploadSeconds": TIKTOK_HTTP_UPLOAD_SECONDS,
        "ffmpeg": bool(FFMPEG_PATH),
        "ffprobe": bool(FFPROBE_PATH),
        "nvenc": nvenc_available(),
        "storage": storage.ENABLED,
        # Local file only — never block /config on R2 restore.
        "hasYtdlpCookies": user_ytdlp_cookies_path(uid).is_file(),
        "aiProviders": chat_models.provider_status(),
        "facebookConfigured": facebook_ads.configured(),
        "duoPlusConfigured": bool((os.environ.get("DUOPLUS_API_KEY") or "").strip()),
        "provisionConfigured": bool(
            (os.environ.get("HOOT_API_KEY") or "").strip()
            and (os.environ.get("WEBSHARE_API_KEY") or "").strip()
        ),
        "pinterestSearchConfigured": bool(
            (os.environ.get("SCRAPE_CREATORS_API_KEY") or "").strip()
        ),
    }


from .routers import batch as batch_routes
from .routers import chat as chat_routes
from .routers import cookies as cookies_routes
from .routers import publish as publish_routes
from .routers import sources as sources_routes
from .routers import spark as spark_routes
from .routers import images as images_routes
from .routers import provision as provision_routes
from .routers import affiliate_public as affiliate_public_routes
from .routers import internal_mcp as internal_mcp_routes
from .routers import plans as plans_routes
from .routers import hooks as hooks_routes
from .routers import workspace as workspace_routes
from .routers import mcp_keys as mcp_keys_routes
from .routers import analytics as analytics_routes
from .routers import operations as operations_routes

app.include_router(sources_routes.router, prefix="/api")
app.include_router(batch_routes.router, prefix="/api")
app.include_router(cookies_routes.router, prefix="/api")
app.include_router(publish_routes.router, prefix="/api")
app.include_router(publish_routes.public_router, prefix="/api")
app.include_router(chat_routes.router, prefix="/api")
app.include_router(hooks_routes.router, prefix="/api")
app.include_router(workspace_routes.router, prefix="/api")
app.include_router(mcp_keys_routes.router, prefix="/api")
app.include_router(analytics_routes.router, prefix="/api")
app.include_router(operations_routes.router, prefix="/api")
app.include_router(spark_routes.router, prefix="/api")
app.include_router(images_routes.router, prefix="/api")
app.include_router(provision_routes.router, prefix="/api")
app.include_router(plans_routes.router, prefix="/api")
app.include_router(internal_mcp_routes.router)
app.include_router(affiliate_public_routes.router)
app.include_router(api)


@app.middleware("http")
async def _prevent_stale_plan_client(request: Request, call_next):
    """Always revalidate the HTML and plan UI script during local development.

    The browser could keep an older ``chat.js`` after an API restart, leaving a
    retired Spark spinner visible even when the catalog already says failed.
    """
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/chat.js", "/app.js", "/bc.js"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# Optional MCP HTTP (same tools as chat) — must mount before static "/"
try:
    from .agent.mcp_http import try_mount_mcp
    from fastapi import Request as _Req

    def _mcp_uid(request: _Req) -> str:
        return get_current_uid(request)

    try_mount_mcp(app, get_uid=_mcp_uid, tool_ctx_factory=chat_routes.tool_context)
except Exception as _mcp_exc:
    logging.getLogger(__name__).info("MCP mount skipped: %s", _mcp_exc)

app.mount("/fonts", StaticFiles(directory=str(FONTS_DIR)), name="fonts")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.exception_handler(Exception)
async def unhandled(request, exc):
    logging.getLogger(__name__).exception("unhandled %s", getattr(request.url, "path", ""))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


cleanup.start_background_cleanup()
