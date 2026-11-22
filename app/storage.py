"""R2 object storage layer with a local scratch cache.

Local disk (DATA_DIR) is treated as an ephemeral cache; R2 is the durable
store for video/cookie/preview blobs when configured. JSON state goes through
app.userstore (one durable write). Blob writers still call `mirror_up` after
writing locally; readers call `ensure_local` before touching a file that may
only exist in R2.

Env: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
"""

from __future__ import annotations

import json
import logging
import os
import queue
import stat
import threading
import time
from pathlib import Path

from .config import user_root

_log = logging.getLogger(__name__)

_MIRROR_Q_MAX = 500

R2_ENDPOINT = os.environ.get("R2_ENDPOINT") or ""
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID") or ""
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY") or ""
R2_BUCKET = os.environ.get("R2_BUCKET") or ""

ENABLED = bool(R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)

_client = None
_client_lock = threading.Lock()


def _s3():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from botocore.config import Config

                _client = boto3.client(
                    "s3",
                    endpoint_url=R2_ENDPOINT,
                    aws_access_key_id=R2_ACCESS_KEY_ID,
                    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                    config=Config(
                        signature_version="s3v4",
                        retries={"max_attempts": 3, "mode": "standard"},
                        connect_timeout=10,
                        read_timeout=30,
                    ),
                    region_name="auto",
                )
    return _client


def rel_of(uid: str, path: Path) -> str:
    """Path relative to the user root, POSIX separators."""
    return path.resolve().relative_to(user_root(uid).resolve()).as_posix()


def key_of(uid: str, rel: str) -> str:
    return f"users/{uid}/{rel}"


def local_of(uid: str, rel: str) -> Path:
    p = (user_root(uid) / rel).resolve()
    p.relative_to(user_root(uid).resolve())  # traversal guard
    return p


# ---------------------------------------------------------------------------
# Mirror operations
# ---------------------------------------------------------------------------

def mirror_up(uid: str, path: Path) -> bool:
    """Upload a local file under the user root to R2. True if durable or disabled."""
    if not ENABLED:
        return True
    last: Exception | None = None
    for attempt in range(4):
        try:
            rel = rel_of(uid, path)
            # Read file into memory first to avoid IncompleteBody errors
            # when file is modified between Content-Length check and upload
            data = path.read_bytes()
            _s3().put_object(Bucket=R2_BUCKET, Key=key_of(uid, rel), Body=data)
            return True
        except Exception as exc:
            last = exc
            _log.warning("R2 upload failed for %s (attempt %s): %s", path, attempt + 1, exc)
            time.sleep(min(2.0, 0.2 * (2 ** attempt)))
    if last is not None:
        raise last
    return False


_mirror_q: queue.Queue | None = None
_mirror_thread: threading.Thread | None = None
_mirror_lock = threading.Lock()


def _mirror_worker() -> None:
    while True:
        item = _mirror_q.get() if _mirror_q is not None else None
        if item is None:
            continue
        uid, path_str = item
        try:
            p = Path(path_str)
            if p.is_file():
                mirror_up(uid, p)
        except Exception as exc:
            _log.warning("R2 async upload failed for %s: %s", path_str, exc)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` via a sibling temp file + ``os.replace`` so readers never
    see a truncated document if the process dies mid-write.

    Windows often raises WinError 5/32 when OneDrive, Defender, or another
    thread still has the destination open. Retry, then copy-over as a last resort.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        _replace_with_retry(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _replace_with_retry(tmp: Path, dest: Path, *, attempts: int = 12) -> None:
    delay = 0.03
    last: BaseException | None = None
    for _ in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            win = getattr(exc, "winerror", None)
            if win not in (5, 32):
                raise
            last = exc
        try:
            if dest.exists():
                dest.chmod(stat.S_IWRITE)
        except OSError:
            pass
        time.sleep(delay)
        delay = min(0.25, delay * 1.5)
    try:
        import shutil

        shutil.copyfile(str(tmp), str(dest))
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    except OSError:
        if last is not None:
            raise last
        raise


def atomic_write_json(path: Path, data: dict, *, indent: int | None = 2) -> None:
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    atomic_write_text(path, text)


def mirror_up_async(uid: str, path: Path) -> None:
    """Queue an R2 upload so JSON writes do not block the request thread."""
    if not ENABLED:
        return
    global _mirror_q, _mirror_thread
    with _mirror_lock:
        if _mirror_q is None:
            _mirror_q = queue.Queue(maxsize=_MIRROR_Q_MAX)
            _mirror_thread = threading.Thread(
                target=_mirror_worker, name="r2-json-mirror", daemon=True
            )
            _mirror_thread.start()
    try:
        _mirror_q.put_nowait((uid, str(path)))
    except queue.Full:
        try:
            _mirror_q.put((uid, str(path)), timeout=5)
        except queue.Full:
            _log.warning("R2 mirror queue full; syncing %s", path)
            mirror_up(uid, Path(path))


def mirror_delete(uid: str, rel: str) -> None:
    if not ENABLED:
        return
    try:
        _s3().delete_object(Bucket=R2_BUCKET, Key=key_of(uid, rel))
    except Exception as exc:
        _log.warning("R2 delete failed for %s: %s", rel, exc)


def ensure_local(uid: str, rel: str) -> Path | None:
    """Return a local path for `rel`, pulling from R2 if the scratch copy is
    gone. None when the object exists nowhere.

    Zero-byte scratch files are treated as missing so a durable R2 copy can
    replace a truncated local write.
    """
    local = local_of(uid, rel)
    try:
        if local.is_file() and local.stat().st_size > 0:
            return local
    except OSError:
        pass
    if not ENABLED:
        return None
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        _s3().download_file(R2_BUCKET, key_of(uid, rel), str(local))
        return local if local.is_file() and local.stat().st_size > 0 else None
    except Exception:
        return None


def ensure_output_local(uid: str, output_rel: str) -> Path | None:
    """Restore a rendered video given a path relative to the user's output dir.

    Skip-index and catalog store ``output_rel`` without the ``output/`` prefix
    (e.g. ``08-26-2026 .../file.mp4``). ``ensure_local`` is rooted at the user
    root, so callers must try ``output/{rel}`` — this helper does that once.
    """
    rel = str(output_rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return None
    if rel.startswith("output/"):
        return ensure_local(uid, rel)
    found = ensure_local(uid, f"output/{rel}")
    if found:
        return found
    # Legacy / mistaken entries that already included a different root prefix.
    return ensure_local(uid, rel)


def presign(uid: str, rel: str, *, expires: int = 3600, filename: str | None = None) -> str | None:
    """Presigned GET URL for a user object (None when R2 disabled)."""
    if not ENABLED:
        return None
    params: dict = {"Bucket": R2_BUCKET, "Key": key_of(uid, rel)}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    try:
        return _s3().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires
        )
    except Exception as exc:
        _log.warning("R2 presign failed for %s: %s", rel, exc)
        return None


# ---------------------------------------------------------------------------
# JSON state helpers (write-through)
# ---------------------------------------------------------------------------

def save_json(uid: str, path: Path, data: dict) -> None:
    """Persist JSON. Mapped user-root paths go through userstore; scratch files
    (unmapped) use the same atomic writer.
    """
    from . import userstore

    if userstore.save_legacy_path(uid, path, data):
        return
    atomic_write_json(path, data, indent=2)
    mirror_up_async(uid, path)


def load_json(uid: str, path: Path, *, skip_firestore: bool = False) -> dict | None:
    """Read JSON via UserStore when the path is mapped; else local/R2 scratch."""
    from . import userstore

    if not skip_firestore and userstore.maps_path(uid, path):
        return userstore.load_legacy_path(uid, path)
    if not path.is_file() and ENABLED:
        try:
            ensure_local(uid, rel_of(uid, path))
        except ValueError:
            return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def delete_json(uid: str, path: Path) -> None:
    from . import userstore

    if userstore.delete_legacy_path(uid, path):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        mirror_delete(uid, rel_of(uid, path))
    except Exception as exc:
        _log.warning("delete_json failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Listing / cleanup
# ---------------------------------------------------------------------------

def list_remote(uid: str, prefix: str) -> list[dict]:
    """List objects under users/{uid}/{prefix}. [{rel, size, mtime}]"""
    if not ENABLED:
        return []
    out: list[dict] = []
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        base = key_of(uid, prefix.rstrip("/") + "/") if prefix else key_of(uid, "")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=base):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(f"users/{uid}/"):]
                out.append(
                    {
                        "rel": rel,
                        "size": int(obj.get("Size") or 0),
                        "mtime": obj["LastModified"].timestamp() if obj.get("LastModified") else 0.0,
                    }
                )
    except Exception as exc:
        _log.warning("R2 list failed for %s: %s", prefix, exc)
    return out


def delete_remote_many(uid: str, rels: list[str]) -> None:
    if not ENABLED or not rels:
        return
    try:
        for i in range(0, len(rels), 1000):
            chunk = rels[i : i + 1000]
            _s3().delete_objects(
                Bucket=R2_BUCKET,
                Delete={"Objects": [{"Key": key_of(uid, r)} for r in chunk]},
            )
    except Exception as exc:
        _log.warning("R2 bulk delete failed: %s", exc)


def list_bucket(prefix: str) -> list[dict]:
    """List all objects under an arbitrary prefix. [{key, size, mtime}]"""
    if not ENABLED:
        return []
    out: list[dict] = []
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                out.append(
                    {
                        "key": obj["Key"],
                        "size": int(obj.get("Size") or 0),
                        "mtime": obj["LastModified"].timestamp() if obj.get("LastModified") else 0.0,
                    }
                )
    except Exception as exc:
        _log.warning("R2 list failed for %s: %s", prefix, exc)
    return out


def delete_keys(keys: list[str]) -> None:
    if not ENABLED or not keys:
        return
    try:
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            _s3().delete_objects(
                Bucket=R2_BUCKET,
                Delete={"Objects": [{"Key": k} for k in chunk]},
            )
    except Exception as exc:
        _log.warning("R2 bulk delete failed: %s", exc)


def health() -> bool:
    """True when storage is reachable (or intentionally disabled)."""
    if not ENABLED:
        return True
    try:
        _s3().head_bucket(Bucket=R2_BUCKET)
        return True
    except Exception:
        return False
