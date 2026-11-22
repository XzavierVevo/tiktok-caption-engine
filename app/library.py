"""Per-user source library (session KV): in-job order + all indexed videos."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from . import storage
from . import userstore
from .config import (
    DATA_DIR,
    VIDEO_EXTENSIONS,
    user_downloads_dir,
    user_previews_dir,
    user_uploads_dir,
)
from .encode import EncodeError, encode_hover_preview, extract_frame, probe
from .ingest import IngestError, download_url, file_hash, safe_stem, validate_url

_log = logging.getLogger(__name__)
_registry: dict[str, dict] = {}
_registry_lock = threading.RLock()
registry_lock = _registry_lock
_frame_locks: dict[str, threading.Lock] = {}
_frame_locks_guard = threading.Lock()


def _frame_lock(key: str) -> threading.Lock:
    with _frame_locks_guard:
        lock = _frame_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _frame_locks[key] = lock
        return lock


def state(uid: str) -> dict:
    with _registry_lock:
        cached = _registry.get(uid)
        if cached is not None:
            return cached
        st = {"sources": {}, "order": []}
        data = userstore.get_kv(uid, "session") or {}
        seen_hash: dict[str, str] = {}
        loaded_ids: list[str] = []
        for src in data.get("sources") or []:
            path = Path(src.get("path") or "")
            sid = src.get("id") or uuid.uuid4().hex[:10]
            if sid in st["sources"]:
                continue
            src["id"] = sid
            src["path"] = str(path)
            if not src.get("platform"):
                src["platform"] = infer_platform(src)
            h = src.get("hash")
            if h in seen_hash:
                src["duplicateOf"] = seen_hash[h]
            else:
                src["duplicateOf"] = None
                if h:
                    seen_hash[h] = sid
            # Log if this source has an end caption
            if src.get("endCaption"):
                ec = src["endCaption"]
                logging.info(f"[END_CAPTION] Loaded source {sid} with endCaption: enabled={ec.get('enabled')}")
            st["sources"][sid] = src
            loaded_ids.append(sid)
        saved_order = data.get("order")
        if isinstance(saved_order, list):
            st["order"] = [sid for sid in saved_order if sid in st["sources"]]
        else:
            st["order"] = loaded_ids
        _registry[uid] = st
        return st


def save_session(uid: str) -> None:
    with _registry_lock:
        st = state(uid)
        payload = {
            "sources": list(st["sources"].values()),
            "order": [sid for sid in st["order"] if sid in st["sources"]],
        }
    userstore.set_kv(uid, "session", payload)


def infer_platform(src: dict) -> str:
    if src.get("platform") in ("pinterest", "tiktok", "youtube", "upload"):
        return str(src["platform"])
    url = (src.get("url") or "").strip()
    if url:
        try:
            return validate_url(url)
        except IngestError:
            pass
    name = f"{src.get('name') or ''} {src.get('stem') or ''} {src.get('path') or ''}".lower()
    if "pinterest" in name:
        return "pinterest"
    if "tiktok" in name or "snaptik" in name:
        return "tiktok"
    if "youtu" in name:
        return "youtube"
    if src.get("origin") == "upload":
        return "upload"
    return "url"


def _detect_burned_caption(path: Path) -> bool | None:
    """Cheap ingest check (4 sampled frames). None when it cannot run."""
    try:
        from .existing_caption import has_burned_caption

        return has_burned_caption(path)
    except Exception:
        _log.warning("burned-in caption check failed for %s", path, exc_info=True)
        return None


def is_blocked(src: dict | None) -> bool:
    """True for sources that must never be used as a render base."""
    return bool(src and src.get("burnedCaption"))


def set_burned_caption(
    uid: str, source_id: str, flagged: bool, *, reason: str = "manual"
) -> dict:
    """Mark (or clear) a source as already captioned. Persists the session."""
    with _registry_lock:
        st = state(uid)
        src = st["sources"].get(source_id)
        if src is None:
            raise HTTPException(404, f"Source not found: {source_id}")
        src["burnedCaption"] = bool(flagged)
        src["burnedCaptionSource"] = reason
        if flagged and source_id in st["order"]:
            # Pull it out of the Create job list so it is not picked again.
            st["order"].remove(source_id)
        found = dict(src)
    save_session(uid)
    return found


def annotate_source(src: dict, in_job: bool) -> dict:
    out = dict(src)
    out["inJob"] = in_job
    out["platform"] = infer_platform(src)
    return out


def register_source(
    uid: str,
    path: Path,
    origin: str,
    url: str | None = None,
    add_to_job: bool = True,
) -> dict:
    meta = probe(path)
    h = file_hash(path)
    burned = _detect_burned_caption(path)
    platform = origin
    if origin != "upload" and url:
        try:
            platform = validate_url(url)
        except IngestError:
            platform = "url"
    elif origin == "upload":
        platform = "upload"
    if platform in ("url", "upload", origin):
        guessed = infer_platform({"name": path.name, "origin": origin, "url": url})
        if guessed in ("pinterest", "tiktok", "youtube"):
            platform = guessed

    extra = False
    with _registry_lock:
        st = state(uid)
        existing = next(
            (s for s in st["sources"].values() if s.get("hash") == h), None
        )
        if existing:
            if burned and not existing.get("burnedCaption"):
                existing["burnedCaption"] = True
                existing["burnedCaptionSource"] = "auto"
            if url and not existing.get("url"):
                existing["url"] = url
                existing["platform"] = infer_platform(existing)
            if add_to_job and existing["id"] not in st["order"]:
                st["order"].append(existing["id"])
            try:
                extra = path.resolve() != Path(existing.get("path") or "").resolve()
            except OSError:
                extra = False
            found = dict(existing)
        else:
            found = None
            src = {
                "id": uuid.uuid4().hex[:10],
                "path": str(path),
                "stem": safe_stem(path.name),
                "name": path.name,
                "hash": h,
                "origin": origin,
                "url": (url or "").strip() or None,
                "platform": platform,
                "addedAt": time.time(),
                "width": meta["width"],
                "height": meta["height"],
                "duration": round(meta["duration"], 2),
                "duplicateOf": None,
                "endCaption": None,
                "crop": None,
                "trim": None,
                # Already has a caption in the pixels: never render onto it.
                "burnedCaption": bool(burned),
                "burnedCaptionSource": "auto" if burned is not None else None,
            }
            st["sources"][src["id"]] = src
            if add_to_job:
                st["order"].append(src["id"])
            found = src
        payload = {
            "sources": list(st["sources"].values()),
            "order": [sid for sid in st["order"] if sid in st["sources"]],
        }
        userstore.set_kv(uid, "session", payload)

    if existing is not None:
        if extra and add_to_job:
            try:
                if path.is_file():
                    path.unlink()
                storage.mirror_delete(uid, storage.rel_of(uid, path))
            except (ValueError, OSError):
                pass
        return found

    try:
        storage.rel_of(uid, path)
        # Don't block the HTTP response on a multi‑MB R2 put — local disk is
        # enough for immediate encode/preview; durability catches up async.
        storage.mirror_up_async(uid, path)
    except ValueError:
        # Legacy paths outside the user root (e.g. data/uploads) cannot be
        # keyed under users/{uid}/ — copy into the user tree then mirror.
        try:
            dest_dir = user_uploads_dir(uid) if origin == "upload" else user_downloads_dir(uid)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / path.name
            if path.resolve() != dest.resolve():
                if not dest.is_file():
                    import shutil

                    shutil.copy2(path, dest)
                with _registry_lock:
                    live = state(uid)["sources"].get(found["id"])
                    if live:
                        live["path"] = str(dest)
                        found = dict(live)
                save_session(uid)
                storage.mirror_up_async(uid, dest)
        except Exception as exc:
            _log.warning("Could not relocate/mirror source %s: %s", path, exc)
    # Warm compact display assets off the request path (ffmpeg + optional R2).
    src_snap = dict(found)

    def _warm() -> None:
        try:
            ensure_preview_frame(uid, src_snap)
        except Exception as exc:
            _log.debug("Preview frame warm failed for %s: %s", src_snap.get("id"), exc)
        try:
            ensure_hover_preview(uid, src_snap)
        except Exception as exc:
            _log.debug("Hover preview warm failed for %s: %s", src_snap.get("id"), exc)

    threading.Thread(
        target=_warm, name=f"preview-warm-{src_snap.get('id')}", daemon=True
    ).start()
    return found


def apply_library_selection(
    uid: str, *, add_ids: list[str] | None = None, remove_ids: list[str] | None = None
) -> dict:
    """Add/remove library videos from the current job in one session write."""
    add_ids = [str(x).strip() for x in (add_ids or []) if str(x).strip()]
    remove_ids = [str(x).strip() for x in (remove_ids or []) if str(x).strip()]
    added: list[str] = []
    removed: list[str] = []
    missing: list[str] = []
    blocked: list[str] = []
    with _registry_lock:
        st = state(uid)
        known = st["sources"]
        order = st["order"]
        for sid in add_ids:
            if sid not in known:
                missing.append(sid)
                continue
            if is_blocked(known[sid]):
                blocked.append(sid)
                continue
            if sid not in order:
                order.append(sid)
                added.append(sid)
        remove_set = set(remove_ids)
        if remove_set:
            before = list(order)
            order[:] = [sid for sid in order if sid not in remove_set]
            removed = [sid for sid in before if sid in remove_set and sid in known]
        if added or removed:
            payload = {
                "sources": list(known.values()),
                "order": [sid for sid in order if sid in known],
            }
            userstore.set_kv(uid, "session", payload)
    return {
        "added": added,
        "removed": removed,
        "missing": missing,
        "blocked": blocked,
        "sources": job_sources(uid),
    }


def unlink_source(uid: str, source_id: str) -> dict:
    with _registry_lock:
        st = state(uid)
        if source_id not in st["sources"]:
            raise HTTPException(404, "Source not found")
        if source_id in st["order"]:
            st["order"].remove(source_id)
    save_session(uid)
    return {"ok": True}


def purge_source(uid: str, source_id: str) -> dict:
    with _registry_lock:
        st = state(uid)
        src = st["sources"].pop(source_id, None)
        if not src:
            raise HTTPException(404, "Source not found")
        if source_id in st["order"]:
            st["order"].remove(source_id)
    try:
        p = Path(src["path"])
        rel = storage.rel_of(uid, p)
        if p.is_file():
            p.unlink()
        storage.mirror_delete(uid, rel)
    except (ValueError, OSError):
        pass
    save_session(uid)
    return {"ok": True}


def _media_scan_dirs(uid: str) -> list[tuple[Path, str]]:
    dirs = [
        (user_uploads_dir(uid), "upload"),
        (user_downloads_dir(uid), "url"),
        (DATA_DIR / "uploads", "upload"),
        (DATA_DIR / "downloads", "url"),
    ]
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    for folder, origin in dirs:
        try:
            key = str(folder.resolve())
        except OSError:
            continue
        if key in seen or not folder.is_dir():
            continue
        seen.add(key)
        out.append((folder, origin))
    return out


def backfill_library(uid: str, limit: int = 80, *, force: bool = False) -> None:
    flag = userstore.get_kv(uid, "_migrated_library")
    if flag and flag.get("done") and not force:
        return
    st = state(uid)
    known: set[str] = set()
    for s in st["sources"].values():
        try:
            known.add(str(Path(s["path"]).resolve()))
        except (OSError, TypeError):
            pass
        name = (s.get("name") or "").lower()
        if name:
            known.add(name)
    added = 0

    def _consider(path: Path, origin: str) -> None:
        nonlocal added
        if added >= limit:
            return
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            return
        try:
            resolved = str(path.resolve())
        except OSError:
            return
        if resolved in known or path.name.lower() in known:
            return
        try:
            src = register_source(uid, path, origin, add_to_job=False)
            known.add(resolved)
            known.add(path.name.lower())
            try:
                known.add(str(Path(src["path"]).resolve()))
            except OSError:
                pass
            added += 1
        except (IngestError, EncodeError, OSError, HTTPException):
            return

    for folder, origin in _media_scan_dirs(uid):
        try:
            files = [p for p in folder.iterdir() if p.is_file()]
        except OSError:
            continue
        files.sort(key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True)
        for path in files:
            if added >= limit:
                break
            _consider(path, origin)

    if storage.ENABLED:
        for prefix, origin in (("uploads", "upload"), ("downloads", "url")):
            for obj in storage.list_remote(uid, prefix):
                if added >= limit:
                    break
                rel = obj.get("rel") or ""
                local = storage.ensure_local(uid, rel)
                if local and local.is_file():
                    _consider(local, origin)

    userstore.set_kv(uid, "_migrated_library", {"done": True, "added": added})


def job_sources(uid: str) -> list[dict]:
    st = state(uid)
    return [
        annotate_source(st["sources"][sid], True)
        for sid in st["order"]
        if sid in st["sources"]
    ]


_backfill_started: set[str] = set()
_backfill_lock = threading.Lock()


def _schedule_library_backfill(uid: str) -> None:
    """Run disk/ffprobe migration off the request path."""
    flag = userstore.get_kv(uid, "_migrated_library")
    if flag and flag.get("done"):
        return
    with _backfill_lock:
        if uid in _backfill_started:
            return
        _backfill_started.add(uid)

    def _run() -> None:
        try:
            backfill_library(uid)
        except Exception:
            _log.exception("Background library backfill failed for %s", uid)
        finally:
            with _backfill_lock:
                _backfill_started.discard(uid)

    threading.Thread(target=_run, name="lib-backfill", daemon=True).start()


def library_summary(src: dict, in_job: bool) -> dict:
    """Lightweight library row for grids / recent strip."""
    return {
        "id": src.get("id"),
        "name": src.get("name") or "",
        "duration": src.get("duration"),
        "width": src.get("width"),
        "height": src.get("height"),
        "addedAt": src.get("addedAt"),
        "origin": src.get("origin"),
        "platform": infer_platform(src),
        "hash": src.get("hash"),
        "url": src.get("url") or "",
        "inJob": in_job,
        "burnedCaption": bool(src.get("burnedCaption")),
        "thumbUrl": f"/api/sources/{src.get('id')}/frame" if src.get("id") else "",
        "hoverUrl": f"/api/sources/{src.get('id')}/hover" if src.get("id") else "",
        # Content-address display assets so browser Cache Storage survives
        # container restarts without ever showing a stale frame for a new file.
        "assetVersion": src.get("hash") or src.get("id") or "",
    }


def library_items(uid: str, *, summary: bool = True) -> list[dict]:
    # Never block GET on disk scan / ffprobe — migrate in the background.
    _schedule_library_backfill(uid)
    st = state(uid)
    active = set(st["order"])
    if summary:
        items = [
            library_summary(src, src["id"] in active)
            for src in st["sources"].values()
        ]
    else:
        items = [
            annotate_source(src, src["id"] in active)
            for src in st["sources"].values()
        ]
    items.sort(key=lambda s: (-float(s.get("addedAt") or 0), s.get("name") or ""))
    return items


def get_source(uid: str, source_id: str) -> dict:
    st = state(uid)
    src = st["sources"].get(source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    return src


def _preview_frame_name(src: dict, at_seconds: float | None) -> tuple[str, float]:
    h = src.get("hash") or src.get("id") or "frame"
    if at_seconds is None:
        return f"frame_{h}.jpg", 0.5
    at_sec = max(0.0, float(at_seconds))
    return f"frame_{h}_t{at_sec:.2f}.jpg", at_sec


def _save_jpeg_bytes(path: Path, blob: bytes) -> bool:
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(blob))
        if img.mode != "RGB":
            img = img.convert("RGB")
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, format="JPEG", quality=85)
        return path.is_file()
    except Exception:
        return False


def _thumb_from_source_url(uid: str, src: dict, frame_path: Path) -> bool:
    """Lightweight poster when the video blob is gone but the original URL remains."""
    url = (src.get("url") or "").strip()
    if not url:
        return False
    thumb_url = ""
    # Fast path for Pinterest pin pages: scrape og:image (no yt-dlp).
    if "pinterest.com/pin/" in url.lower():
        try:
            import re

            import requests

            resp = requests.get(
                url if url.endswith("/") else url + "/",
                timeout=12,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            if resp.status_code == 200 and resp.text:
                thumb_url = ""
                for pattern in (
                    r'<meta[^>]+property=(["\x27])og:image\1[^>]+content=(["\x27])(.*?)\2',
                    r'<meta[^>]+content=(["\x27])(.*?)\1[^>]+property=(["\x27])og:image\3',
                ):
                    m = re.search(pattern, resp.text, re.I)
                    if not m:
                        continue
                    # content is group 3 in the first pattern, group 2 in the second
                    cand = m.group(3) if pattern.startswith("<meta[^>]+property") else m.group(2)
                    if cand and cand.strip():
                        thumb_url = cand.strip()
                        break
        except Exception as exc:
            _log.debug("Pinterest og:image scrape failed for %s: %s", url, exc)

    if not thumb_url:
        try:
            import yt_dlp

            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
            }
            try:
                from .config import user_ytdlp_cookies_path

                cookies_path = user_ytdlp_cookies_path(uid)
                if cookies_path.is_file():
                    opts["cookiefile"] = str(cookies_path)
            except Exception:
                pass
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
            thumb_url = str(info.get("thumbnail") or "").strip()
            if not thumb_url:
                thumbs = info.get("thumbnails") or []
                if isinstance(thumbs, list) and thumbs:
                    last = thumbs[-1] if isinstance(thumbs[-1], dict) else {}
                    thumb_url = str(last.get("url") or "").strip()
        except Exception as exc:
            _log.debug("yt-dlp thumb lookup failed for %s: %s", url, exc)
            return False
    if not thumb_url:
        return False
    try:
        import requests

        resp = requests.get(
            thumb_url,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.pinterest.com/",
            },
        )
        if resp.status_code != 200 or not resp.content:
            return False
        if not _save_jpeg_bytes(frame_path, resp.content):
            return False
        try:
            storage.mirror_up_async(uid, frame_path)
        except Exception:
            pass
        return True
    except Exception as exc:
        _log.debug("Thumb download failed for %s: %s", thumb_url, exc)
        return False


def ensure_preview_frame(
    uid: str, src: dict, *, at_seconds: float | None = None, generate: bool = True
) -> Path:
    """Return a JPEG first-frame path.

    When ``generate=False`` (library grid GET), only serve an existing cached
    JPEG / R2 restore — never yt-dlp, URL scrape, or ffmpeg extract.
    """
    name, at_sec = _preview_frame_name(src, at_seconds)
    frame_path = user_previews_dir(uid) / name
    lock_key = f"{uid}:{name}"
    with _frame_lock(lock_key):
        if frame_path.is_file():
            return frame_path
        restored = storage.ensure_local(uid, f"previews/{name}")
        if restored and restored.is_file():
            return restored

        if not generate:
            # A successful 1x1 placeholder is indistinguishable from a real
            # poster to HTTP/browser caches and caused permanently gray cards
            # after Cloudflare cold starts. Missing means missing; callers that
            # want recovery explicitly request generation in the background.
            raise HTTPException(404, "Preview frame not ready")

        # Prefer a cheap poster when the default mid-frame is missing — avoids
        # re-downloading dozens of videos just to paint the library grid.
        if at_seconds is None and _thumb_from_source_url(uid, src, frame_path):
            return frame_path

        video = ensure_source_file(uid, src)
        try:
            extract_frame(video, frame_path, at_seconds=at_sec)
        except EncodeError as exc:
            raise HTTPException(500, str(exc)) from exc
        if not frame_path.is_file():
            raise HTTPException(500, "Frame extract produced no file")
        try:
            storage.mirror_up_async(uid, frame_path)
        except Exception:
            pass
        return frame_path


def ensure_hover_preview(uid: str, src: dict) -> Path:
    """Return a durable low-resolution hover clip, restoring from R2 first."""
    key = src.get("hash") or src.get("id") or "hover"
    name = f"hover_{key}.mp4"
    out = user_previews_dir(uid) / name
    with _frame_lock(f"{uid}:{name}"):
        if out.is_file() and out.stat().st_size:
            return out
        restored = storage.ensure_local(uid, f"previews/{name}")
        if restored and restored.is_file() and restored.stat().st_size:
            return restored
        video = ensure_source_file(uid, src)
        try:
            encode_hover_preview(video, out)
        except EncodeError as exc:
            raise HTTPException(500, str(exc)) from exc
        try:
            storage.mirror_up_async(uid, out)
        except Exception:
            pass
        return out


def ensure_source_file(uid: str, src: dict) -> Path:
    path = Path(src.get("path") or "")
    if path.is_file():
        return path
    try:
        rel = storage.rel_of(uid, path)
    except ValueError:
        rel = None
    if rel:
        restored = storage.ensure_local(uid, rel)
        if restored:
            return restored

    url = (src.get("url") or "").strip()
    if url:
        try:
            new_path = download_url(uid, url)
        except IngestError as exc:
            raise HTTPException(
                404, f"Source file no longer available: {src.get('name')}"
            ) from exc
        try:
            meta = probe(new_path)
            h = file_hash(new_path)
        except (EncodeError, OSError):
            meta, h = {}, src.get("hash")
        with _registry_lock:
            st = state(uid)
            live = st["sources"].get(src.get("id") or "")
            if live is not None:
                live["path"] = str(new_path)
                if h:
                    live["hash"] = h
                if meta:
                    live["width"] = meta.get("width") or live.get("width")
                    live["height"] = meta.get("height") or live.get("height")
                    if meta.get("duration") is not None:
                        live["duration"] = round(float(meta["duration"]), 2)
                src = dict(live)
        save_session(uid)
        try:
            storage.mirror_up(uid, new_path)
        except Exception as exc:
            _log.warning("R2 re-mirror failed for %s: %s", new_path, exc)
        return new_path

    if rel is None:
        raise HTTPException(404, "Source file missing")
    raise HTTPException(
        404, f"Source file no longer available: {src.get('name')}"
    )
