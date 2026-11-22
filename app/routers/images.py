"""Image library API: upload, list, serve, delete."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import images
from ..auth import get_current_uid
from ..ingest import IngestError, save_library_image

router = APIRouter(dependencies=[Depends(get_current_uid)])

_MEDIA = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


@router.post("/images/upload")
async def upload_image(
    file: UploadFile = File(...),
    uid: str = Depends(get_current_uid),
) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    try:
        path = await run_in_threadpool(
            save_library_image, uid, file.filename or "image.jpg", data
        )
        img = await run_in_threadpool(images.register_image, uid, path, "upload", None)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    return images.public_item(img)


class ImageUrlRequest(BaseModel):
    url: str
    pinUrl: str | None = None
    title: str | None = None


@router.post("/images/from-url")
async def image_from_url(req: ImageUrlRequest, uid: str = Depends(get_current_uid)) -> dict:
    def _go() -> dict:
        img = images.register_from_url(
            uid, req.url, origin="pinterest", title=req.title or ""
        )
        if req.pinUrl:
            img["url"] = req.pinUrl
            img["pinUrl"] = req.pinUrl
            st = images.state(uid)
            if img.get("id") in st["images"]:
                st["images"][img["id"]]["url"] = req.pinUrl
                st["images"][img["id"]]["pinUrl"] = req.pinUrl
                images.save_state(uid)
        return img

    try:
        img = await run_in_threadpool(_go)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    return images.public_item(img)


@router.get("/images")
def list_images(uid: str = Depends(get_current_uid)) -> dict:
    items = images.list_images(uid)
    return {"items": items, "count": len(items)}


class ImageOrderRequest(BaseModel):
    ids: list[str]


@router.put("/images/order")
def reorder_images(req: ImageOrderRequest, uid: str = Depends(get_current_uid)) -> dict:
    items = images.reorder_images(uid, req.ids or [])
    return {"items": items, "count": len(items)}


@router.get("/images/{image_id}/file")
def image_file(image_id: str, uid: str = Depends(get_current_uid)) -> FileResponse:
    try:
        img = images.get_image(uid, image_id)
        path = images.ensure_image_file(uid, img)
    except KeyError as exc:
        raise HTTPException(404, "Image not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    media = _MEDIA.get(Path(path).suffix.lower(), "image/jpeg")
    return FileResponse(path, media_type=media)


@router.delete("/images/{image_id}")
def delete_image(image_id: str, uid: str = Depends(get_current_uid)) -> dict:
    return images.purge_image(uid, image_id)
