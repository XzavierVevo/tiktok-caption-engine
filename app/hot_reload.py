"""Hot reload support: watch for code changes and reload after workflow completion.

This module monitors Python files for changes and triggers a graceful reload
when no workflows are actively running. Progress is saved before reload so
workflows can resume automatically.

Usage:
    - Call `start_watcher()` during app startup
    - Call `check_reload_needed()` after each workflow/step completion
    - The system will auto-reload when safe (no active work)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_APP_DIR = _ROOT / "app"
_PENDING_RELOAD_PATH = _ROOT / "data" / "_pending_reload.json"

# State
_pending_reload = False
_pending_reload_lock = threading.Lock()
_last_change_time: float = 0
_watcher_thread: threading.Thread | None = None
_stop_watcher = threading.Event()

# Minimum time after code change before reload (debounce)
_DEBOUNCE_SECONDS = 2.0


def _get_python_files() -> dict[str, float]:
    """Get all Python files and their modification times."""
    files: dict[str, float] = {}
    for pattern in ("**/*.py",):
        for path in _APP_DIR.glob(pattern):
            if "__pycache__" in str(path):
                continue
            try:
                files[str(path)] = path.stat().st_mtime
            except OSError:
                pass
    return files


def _watch_loop() -> None:
    """Background thread that watches for file changes."""
    global _pending_reload, _last_change_time

    known_files = _get_python_files()
    _log.info("Hot reload watcher started, monitoring %d files", len(known_files))

    while not _stop_watcher.is_set():
        try:
            _stop_watcher.wait(timeout=1.0)
            if _stop_watcher.is_set():
                break

            current_files = _get_python_files()

            # Check for changes
            changed = False
            for path, mtime in current_files.items():
                if path not in known_files or known_files[path] != mtime:
                    changed = True
                    _log.info("File changed: %s", path)
                    break

            # Check for new files
            for path in current_files:
                if path not in known_files:
                    changed = True
                    _log.info("New file: %s", path)
                    break

            if changed:
                with _pending_reload_lock:
                    _pending_reload = True
                    _last_change_time = time.monotonic()
                known_files = current_files
                _log.info("Code changes detected, reload pending...")

        except Exception:
            _log.debug("Watcher iteration error", exc_info=True)


def start_watcher() -> None:
    """Start the file watcher thread."""
    global _watcher_thread

    if _watcher_thread is not None and _watcher_thread.is_alive():
        return

    _stop_watcher.clear()
    _watcher_thread = threading.Thread(target=_watch_loop, daemon=True, name="hot-reload-watcher")
    _watcher_thread.start()


def stop_watcher() -> None:
    """Stop the file watcher thread."""
    _stop_watcher.set()
    if _watcher_thread is not None:
        _watcher_thread.join(timeout=2.0)


def is_reload_pending() -> bool:
    """Check if a reload is pending due to code changes."""
    with _pending_reload_lock:
        return _pending_reload


def clear_reload_pending() -> None:
    """Clear the pending reload flag."""
    global _pending_reload
    with _pending_reload_lock:
        _pending_reload = False


def _count_active_workflows() -> int:
    """Count currently running provision workflows."""
    try:
        from . import job_runtime

        count = 0
        for job in job_runtime.provisions.list_all():
            if hasattr(job, "status") and str(job.status) in ("running", "queued"):
                count += 1
        return count
    except Exception:
        return 0


def _count_active_steps() -> int:
    """Count currently claimed provision steps."""
    try:
        from .provision.owner import owner_uid
        from .provision import inventory as inv

        uid = owner_uid()
        steps = inv.list_steps(uid) or []
        count = 0
        for step in steps:
            if isinstance(step, dict) and step.get("status") == "claimed":
                count += 1
        return count
    except Exception:
        return 0


def _save_reload_state() -> None:
    """Save state before reload for recovery."""
    try:
        state = {
            "reload_time": time.time(),
            "pending_workflows": _count_active_workflows(),
            "pending_steps": _count_active_steps(),
        }
        _PENDING_RELOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PENDING_RELOAD_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        _log.info("Saved reload state: %s", state)
    except Exception:
        _log.warning("Failed to save reload state", exc_info=True)


def _trigger_reload() -> None:
    """Trigger a process reload by touching the main.py file.

    This works with uvicorn --reload which watches for file changes.
    We touch a file to trigger the reload gracefully.
    """
    _log.info("Triggering hot reload...")
    _save_reload_state()

    # Stop the embedded worker first
    try:
        from . import provision_embed
        provision_embed.stop_embedded_worker()
    except Exception:
        pass

    # Touch the main.py file to trigger uvicorn --reload
    try:
        main_py = _APP_DIR / "main.py"
        if main_py.is_file():
            main_py.touch()
            _log.info("Touched %s to trigger uvicorn reload", main_py)
            return
    except Exception:
        _log.warning("Failed to touch main.py", exc_info=True)

    # Fallback: send SIGTERM to trigger reload
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        _log.error("Failed to send SIGTERM", exc_info=True)
        # Last resort: exit the process
        sys.exit(0)


def check_reload_after_completion() -> bool:
    """Check if we should reload after a workflow/step completion.

    Returns True if reload was triggered, False otherwise.
    Call this after each step or workflow completes.
    """
    global _last_change_time

    with _pending_reload_lock:
        if not _pending_reload:
            return False

        # Debounce: wait for changes to settle
        if time.monotonic() - _last_change_time < _DEBOUNCE_SECONDS:
            return False

    # Check if any work is still running
    active_workflows = _count_active_workflows()
    active_steps = _count_active_steps()

    if active_workflows > 0 or active_steps > 0:
        _log.debug(
            "Reload pending but work still active: workflows=%d steps=%d",
            active_workflows,
            active_steps,
        )
        return False

    # Safe to reload
    _log.info("No active work, proceeding with hot reload")
    _trigger_reload()
    return True


def load_reload_state() -> dict[str, Any] | None:
    """Load state from previous reload (if any)."""
    try:
        if _PENDING_RELOAD_PATH.is_file():
            data = json.loads(_PENDING_RELOAD_PATH.read_text(encoding="utf-8"))
            # Clean up the file after reading
            _PENDING_RELOAD_PATH.unlink(missing_ok=True)
            return data
    except Exception:
        pass
    return None


def resume_after_reload() -> None:
    """Called on startup to resume any work interrupted by reload."""
    state = load_reload_state()
    if not state:
        return

    _log.info("Resuming after hot reload: %s", state)

    # Resume orphaned jobs
    try:
        from . import provision_jobs
        resumed = provision_jobs.resume_orphaned_jobs()
        if resumed:
            _log.info("Resumed %d orphaned jobs after reload: %s", len(resumed), resumed)
    except Exception:
        _log.warning("Failed to resume orphaned jobs", exc_info=True)
