"""Retention cleanup: delete old media (scratch + R2) so storage costs stay
bounded. Media files (uploads, downloads, outputs, previews) older than
RETENTION_DAYS are removed; small state files (session, accounts, cookies,
settings, manifests) are kept forever.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import storage
from .config import RETENTION_DAYS, USERS_DIR

_log = logging.getLogger(__name__)

_MEDIA_DIRS = ("uploads", "downloads", "output", "previews")
_INTERVAL_S = 6 * 3600
_started = False
_start_lock = threading.Lock()


def _cleanup_local(cutoff: float) -> int:
    removed = 0
    if not USERS_DIR.is_dir():
        return 0
    for user_dir in USERS_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        for kind in _MEDIA_DIRS:
            d = user_dir / kind
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        removed += 1
                except OSError:
                    continue
            # prune empty batch folders
            for sub in sorted(d.rglob("*"), reverse=True):
                if sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
    return removed


def _cleanup_remote(cutoff: float) -> int:
    if not storage.ENABLED:
        return 0
    stale: list[str] = []
    for obj in storage.list_bucket("users/"):
        key = obj["key"]
        parts = key.split("/")
        # users/{uid}/{kind}/...
        if len(parts) < 4 or parts[2] not in _MEDIA_DIRS:
            continue
        if obj["mtime"] and obj["mtime"] < cutoff:
            stale.append(key)
    storage.delete_keys(stale)
    return len(stale)


def run_once() -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    local = _cleanup_local(cutoff)
    remote = _cleanup_remote(cutoff)
    if local or remote:
        _log.info("Retention cleanup removed %d local / %d remote files", local, remote)


def _loop() -> None:
    while True:
        try:
            run_once()
        except Exception as exc:
            _log.warning("Cleanup pass failed: %s", exc)
        time.sleep(_INTERVAL_S)


def start_background_cleanup() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True, name="retention-cleanup").start()
