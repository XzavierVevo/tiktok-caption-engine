"""Per-user still-image library for photo carousels."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from . import storage
from . import userstore
from .config import (
    IMAGE_EXTENSIONS,
    MAX_CAROUSEL_SLIDES,
    MAX_IMAGE_MB,
    user_chat_images_dir,
    user_images_dir,
)
from .ingest import IngestError, file_hash, safe_stem, save_library_image

_log = logging.getLogger(__name__)
_registry: dict[str, dict] = {}
_lock = threading.RLock()

_PIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.pinterest.com/",
}


def _probe_image(path: Path) -> dict[str, int]:
    width = 0
    height = 0
    try:
        from PIL import Image

        with Image.open(path) as im:
            width, height = im.size
    except Exception:
        _log.debug("image probe failed for %s", path, exc_info=True)
    return {"width": int(width or 0), "height": int(height or 0)}


def state(uid: str) -> dict:
    with _lock:
        cached = _registry.get(uid)
        if cached is not None:
            return cached
        st = {"images": {}, "order": []}
        data = userstore.get_kv(uid, "images") or {}
        loaded: list[str] = []
        for img in data.get("images") or []:
            if not isinstance(img, dict):
                continue
            iid = str(img.get("id") or "").strip() or uuid.uuid4().hex[:10]
            img["id"] = iid
            if iid in st["images"]:
                continue
            st["images"][iid] = img
            loaded.append(iid)
        saved_order = data.get("order")
        if isinstance(saved_order, list):
            st["order"] = [sid for sid in saved_order if sid in st["images"]]
        else:
            st["order"] = loaded
        _registry[uid] = st
        return st


def save_state(uid: str, *, allow_empty: bool = False) -> None:
    with _lock:
        st = state(uid)
        payload = {
            "images": list(st["images"].values()),
            "order": [sid for sid in st["order"] if sid in st["images"]],
        }
    # Never push an empty registry over a non-empty durable copy — cold
    # containers and racey hydrates have wiped carousel libraries this way.
    # Explicit purge passes allow_empty=True.
    if not payload["images"] and not allow_empty:
        existing = userstore.get_kv(uid, "images") or {}
        prior = existing.get("images") if isinstance(existing, dict) else None
        if isinstance(prior, list) and prior:
            _log.warning(
                "refusing empty images save over %s durable entries uid=%s",
                len(prior),
                str(uid or "")[:8],
            )
            with _lock:
                st = {"images": {}, "order": []}
                loaded: list[str] = []
                for img in prior:
                    if not isinstance(img, dict):
                        continue
                    iid = str(img.get("id") or "").strip()
                    if not iid or iid in st["images"]:
                        continue
                    img = dict(img)
                    img["id"] = iid
                    st["images"][iid] = img
                    loaded.append(iid)
                saved_order = existing.get("order") if isinstance(existing, dict) else None
                if isinstance(saved_order, list):
                    st["order"] = [sid for sid in saved_order if sid in st["images"]]
                else:
                    st["order"] = loaded
                _registry[uid] = st
            return
    userstore.set_kv(uid, "images", payload)


def public_item(img: dict) -> dict:
    iid = str(img.get("id") or "")
    return {
        "id": iid,
        "name": img.get("name") or Path(str(img.get("path") or "")).name,
        "stem": img.get("stem") or "",
        "origin": img.get("origin") or "upload",
        "url": img.get("url") or "",
        "pinUrl": img.get("pinUrl") or img.get("url") or "",
        "width": img.get("width") or 0,
        "height": img.get("height") or 0,
        "addedAt": img.get("addedAt") or 0,
        "fileUrl": f"/api/images/{iid}/file",
        "thumbUrl": f"/api/images/{iid}/file",
    }


def list_images(uid: str) -> list[dict]:
    backfill_images(uid)
    st = state(uid)
    items = [
        public_item(st["images"][iid])
        for iid in st["order"]
        if iid in st["images"]
    ]
    extra = [
        public_item(img)
        for iid, img in st["images"].items()
        if iid not in set(st["order"])
    ]
    extra.sort(key=lambda s: (-float(s.get("addedAt") or 0), s.get("name") or ""))
    return items + extra


def backfill_images(uid: str, limit: int = 120, *, force: bool = False) -> int:
    """Re-register stills sitting on disk that dropped out of the KV registry."""
    folders = [user_images_dir(uid), user_chat_images_dir(uid)]
    disk_files: list[tuple[Path, str]] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        origin = "chat" if folder == user_chat_images_dir(uid) else "upload"
        try:
            for path in folder.iterdir():
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                    disk_files.append((path, origin))
        except OSError:
            continue

    with _lock:
        st = state(uid)
        known_paths: set[str] = set()
        known_hashes: set[str] = set()
        for img in st["images"].values():
            try:
                known_paths.add(str(Path(img.get("path") or "").resolve()))
            except (OSError, TypeError):
                pass
            h = img.get("hash")
            if h:
                known_hashes.add(str(h))
        reg_n = len(st["images"])

    orphans = []
    for path, origin in disk_files:
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in known_paths:
            continue
        orphans.append((path, origin, resolved))

    flag = userstore.get_kv(uid, "_migrated_images")
    if (
        not force
        and flag
        and flag.get("done")
        and not orphans
        and len(disk_files) <= reg_n
    ):
        return 0

    added = 0
    orphans.sort(key=lambda row: row[0].stat().st_mtime if row[0].is_file() else 0)
    for path, origin, resolved in orphans:
        if added >= limit:
            break
        try:
            h = file_hash(path)
        except OSError:
            continue
        if h in known_hashes:
            known_paths.add(resolved)
            continue
        try:
            found = register_image(uid, path, origin, None)
            known_paths.add(resolved)
            known_hashes.add(str(found.get("hash") or h))
            added += 1
        except (IngestError, OSError):
            continue

    userstore.set_kv(
        uid,
        "_migrated_images",
        {"done": True, "added": added, "scannedAt": time.time()},
    )
    return added


def _id_from_filename(name: str) -> str:
    """Library files are saved as ``stem_{10hex}.ext`` — reuse that hex as id."""
    stem = Path(str(name or "")).stem
    if len(stem) >= 11 and stem[-11] == "_" and all(
        c in "0123456789abcdef" for c in stem[-10:]
    ):
        return stem[-10:]
    return ""


def get_image(uid: str, image_id: str) -> dict:
    iid = str(image_id or "").strip()
    st = state(uid)
    img = st["images"].get(iid)
    if img:
        return img
    recovered = recover_image(uid, iid)
    if recovered:
        return recovered
    raise KeyError("Image not found")


def recover_image(uid: str, image_id: str) -> dict | None:
    """Re-register a still whose KV row was lost but whose file (or R2 object) remains."""
    iid = str(image_id or "").strip()
    if not iid or len(iid) > 40 or "/" in iid or "\\" in iid or ".." in iid:
        return None
    st = state(uid)
    if iid in st["images"]:
        return st["images"][iid]

    candidates: list[tuple[Path, str]] = []
    for folder, origin in (
        (user_images_dir(uid), "upload"),
        (user_chat_images_dir(uid), "chat"),
    ):
        if not folder.is_dir():
            continue
        try:
            for path in folder.iterdir():
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                if _id_from_filename(path.name) == iid or path.stem == iid:
                    candidates.append((path, origin))
        except OSError:
            continue

    if not candidates and storage.ENABLED:
        for prefix, origin in (("images", "upload"), ("chat_images", "chat")):
            try:
                remote = storage.list_remote(uid, prefix) or []
            except Exception:
                remote = []
            for it in remote:
                rel = str(it.get("rel") or "")
                name = Path(rel).name
                if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                if _id_from_filename(name) != iid and Path(name).stem != iid:
                    continue
                restored = storage.ensure_local(uid, rel)
                if restored and Path(restored).is_file():
                    candidates.append((Path(restored), origin))
                    break

    for path, origin in candidates:
        try:
            return register_image(uid, path, origin, force_id=iid)
        except (IngestError, OSError):
            continue
    return None


def ensure_image_file(uid: str, img: dict) -> Path:
    path = Path(img.get("path") or "")
    if path.is_file():
        return path
    try:
        rel = storage.rel_of(uid, path)
    except ValueError as exc:
        raise FileNotFoundError("Image file missing") from exc
    restored = storage.ensure_local(uid, rel)
    if not restored:
        # Last chance: filename may still exist under images/ even if path drifted.
        iid = str(img.get("id") or "")
        if iid:
            recovered = recover_image(uid, iid)
            if recovered:
                path2 = Path(recovered.get("path") or "")
                if path2.is_file():
                    return path2
        raise FileNotFoundError(f"Image file no longer available: {img.get('name')}")
    return restored


def register_image(
    uid: str,
    path: Path,
    origin: str,
    url: str | None = None,
    *,
    force_id: str | None = None,
) -> dict:
    path = Path(path)
    if not path.is_file():
        raise IngestError("Image file not found")
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise IngestError(f"Unsupported image type: {path.suffix or '(none)'}")
    h = file_hash(path)
    meta = _probe_image(path)
    pin_url = (url or "").strip() or None
    prefer = str(force_id or "").strip() or _id_from_filename(path.name)
    extra = False
    with _lock:
        st = state(uid)
        existing = next((s for s in st["images"].values() if s.get("hash") == h), None)
        if existing is None and prefer and prefer in st["images"]:
            existing = st["images"][prefer]
        if existing:
            old = Path(str(existing.get("path") or ""))
            if path.is_file() and not old.is_file():
                existing["path"] = str(path)
                existing["name"] = path.name
            if pin_url and not existing.get("url"):
                existing["url"] = pin_url
                existing["pinUrl"] = pin_url
            try:
                extra = path.resolve() != Path(existing.get("path") or "").resolve()
            except OSError:
                extra = False
            found = dict(existing)
        else:
            iid = prefer or uuid.uuid4().hex[:10]
            if iid in st["images"]:
                iid = uuid.uuid4().hex[:10]
            found = {
                "id": iid,
                "path": str(path),
                "stem": safe_stem(path.name),
                "name": path.name,
                "hash": h,
                "origin": origin or "upload",
                "url": pin_url,
                "pinUrl": pin_url,
                "addedAt": time.time(),
                "width": meta["width"],
                "height": meta["height"],
            }
            st["images"][found["id"]] = found
            st["order"].append(found["id"])
        save_state(uid)

    if existing is not None:
        if extra:
            try:
                if path.is_file() and path.resolve() != Path(existing.get("path") or "").resolve():
                    # Keep the first copy; drop the duplicate file when it is extra.
                    pass
            except OSError:
                pass
        return found

    try:
        storage.rel_of(uid, path)
        # Sync upload — async queue drops / process exits were losing carousel stills.
        storage.mirror_up(uid, path)
    except ValueError:
        pass
    return found


def promote_chat_image(uid: str, image_id: str, name: str = "") -> dict:
    """Register a chat-attached still as a library image (deduped by hash)."""
    safe = Path(image_id or "").name
    if not safe or safe != image_id:
        raise IngestError("Image not found")
    path = (user_chat_images_dir(uid) / safe).resolve()
    root = user_chat_images_dir(uid).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise IngestError("Image not found") from exc
    if not path.is_file():
        restored = storage.ensure_local(uid, f"chat_images/{safe}")
        if not restored:
            raise IngestError("Image not found")
        path = restored
    return register_image(uid, path, origin="chat")


def purge_image(uid: str, image_id: str) -> dict:
    iid = str(image_id or "").strip()
    with _lock:
        st = state(uid)
        img = st["images"].pop(iid, None) if iid else None
        if not img and iid:
            # Tolerate id/name confusion (filename stem often ends with the id).
            for key, candidate in list(st["images"].items()):
                name = str(candidate.get("name") or "")
                stem = Path(name).stem
                if key == iid or stem == iid or stem.endswith(f"_{iid}"):
                    img = st["images"].pop(key, None)
                    iid = key
                    break
        if not img:
            # Idempotent: already gone (stale client cache / double-click).
            return {"ok": True, "missing": True}
        if iid in st["order"]:
            st["order"].remove(iid)
    try:
        p = Path(img["path"])
        rel = storage.rel_of(uid, p)
        if p.is_file():
            p.unlink()
        storage.mirror_delete(uid, rel)
    except (ValueError, OSError):
        pass
    save_state(uid, allow_empty=True)
    return {"ok": True}


def reorder_images(uid: str, ids: list[str]) -> list[dict]:
    """Persist a new slide order. Unknown ids are ignored; missing ids append at end."""
    cleaned = [str(i).strip() for i in (ids or []) if str(i).strip()]
    with _lock:
        st = state(uid)
        known = set(st["images"].keys())
        seen: set[str] = set()
        new_order: list[str] = []
        for iid in cleaned:
            if iid in known and iid not in seen:
                new_order.append(iid)
                seen.add(iid)
        for iid in st["order"]:
            if iid in known and iid not in seen:
                new_order.append(iid)
                seen.add(iid)
        for iid in st["images"]:
            if iid not in seen:
                new_order.append(iid)
                seen.add(iid)
        st["order"] = new_order
        save_state(uid)
    return list_images(uid)


def _guess_ext(url: str, content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }
    if ctype in mapping:
        return mapping[ctype]
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return ext
    return ".jpg"


def download_image_url(uid: str, url: str, *, filename: str = "") -> Path:
    target = (url or "").strip()
    if not target.startswith("http"):
        raise IngestError("Not a valid image URL")
    try:
        resp = requests.get(target, headers=_PIN_HEADERS, timeout=45)
    except requests.RequestException as exc:
        raise IngestError(f"Image download failed: {exc}") from exc
    if resp.status_code != 200 or not resp.content:
        raise IngestError(f"Image download failed (HTTP {resp.status_code})")
    if len(resp.content) > MAX_IMAGE_MB * 1024 * 1024:
        raise IngestError(f"Image exceeds the {MAX_IMAGE_MB} MB upload limit")
    ext = _guess_ext(target, resp.headers.get("Content-Type") or "")
    name = filename or Path(urlparse(target).path).name or f"pin{ext}"
    if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
        name = f"{safe_stem(name)}{ext}"
    return save_library_image(uid, name, resp.content)


def register_from_url(uid: str, url: str, *, origin: str = "pinterest", title: str = "") -> dict:
    path = download_image_url(uid, url, filename=title or "")
    return register_image(uid, path, origin=origin, url=url)


def resolve_slide(uid: str, token: str) -> dict:
    """Resolve a library id, chat image filename, or http(s) image URL."""
    raw = str(token or "").strip()
    if not raw:
        raise IngestError("Empty slide id")
    st = state(uid)
    if raw in st["images"]:
        return st["images"][raw]
    # Chat still filename (uuid suffix).
    if Path(raw).suffix.lower() in IMAGE_EXTENSIONS:
        return promote_chat_image(uid, Path(raw).name)
    if raw.startswith("http://") or raw.startswith("https://"):
        return register_from_url(uid, raw, origin="pinterest")
    raise IngestError(f"Unknown image: {raw}")


def chunk_slides(image_ids: list[str], slide_count: int, count: int) -> list[list[str]]:
    k = max(1, min(MAX_CAROUSEL_SLIDES, int(slide_count or 1)))
    n = max(1, int(count or 0))
    ids = [str(x).strip() for x in image_ids if str(x).strip()]
    need = n * k
    if len(ids) < need:
        raise IngestError(
            f"Need {need} images for {n} carousels of {k} slide(s); got {len(ids)}"
        )
    groups: list[list[str]] = []
    idx = 0
    for _ in range(n):
        groups.append(ids[idx : idx + k])
        idx += k
    return groups


def carousel_output_rel(carousel_id: str) -> str:
    cid = str(carousel_id or "").strip()
    return f"carousels/{cid}" if cid else ""


def slim_for_agent(img: dict) -> dict[str, Any]:
    pub = public_item(img) if "fileUrl" not in img else dict(img)
    return {
        "id": pub.get("id"),
        "name": pub.get("name"),
        "origin": pub.get("origin"),
        "url": pub.get("url") or "",
        "width": pub.get("width") or 0,
        "height": pub.get("height") or 0,
    }
