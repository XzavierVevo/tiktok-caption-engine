"""MCP API keys (Settings UI)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_uid

router = APIRouter(tags=["mcp-keys"], dependencies=[Depends(get_current_uid)])


@router.get("/mcp/keys")
def mcp_keys_list(uid: str = Depends(get_current_uid)) -> dict:
    from .. import mcp_keys

    return {"ok": True, "keys": mcp_keys.list_keys(uid)}


class McpKeyCreate(BaseModel):
    label: str = ""
    mode: str = "trusted"  # trusted | confirm


@router.post("/mcp/keys")
def mcp_keys_create(req: McpKeyCreate, uid: str = Depends(get_current_uid)) -> dict:
    from .. import mcp_keys

    created = mcp_keys.create_key_indexed(
        uid, label=req.label or "", mode=req.mode or "trusted"
    )
    return {"ok": True, **created}


@router.delete("/mcp/keys/{key_id}")
def mcp_keys_revoke(key_id: str, uid: str = Depends(get_current_uid)) -> dict:
    from .. import mcp_keys

    ok = mcp_keys.revoke_key(uid, key_id)
    if not ok:
        raise HTTPException(404, "Key not found")
    return {"ok": True, "id": key_id}
