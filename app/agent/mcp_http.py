"""Authenticated MCP-shaped HTTP endpoint at /mcp.

Exposes the same tool registry as in-app chat. Uses a simple JSON-RPC
surface that works without depending on a specific FastMCP package layout:

  GET  /mcp              → server info + tool names
  POST /mcp/tools/list   → { tools: [...] }
  POST /mcp/tools/call   → { name, arguments } → tool result

Auth: Authorization Bearer Firebase ID token (same as /api).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

_log = logging.getLogger(__name__)


class ToolCallBody(BaseModel):
    name: str
    arguments: dict | None = None


def try_mount_mcp(
    app,
    *,
    get_uid: Callable[[Any], str],
    tool_ctx_factory: Callable[[], Any],
) -> bool:
    from . import tools as toolreg

    router = APIRouter(prefix="/mcp", tags=["mcp"])

    def _uid(request: Request) -> str:
        try:
            return get_uid(request)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(401, str(exc)) from exc

    @router.get("")
    def mcp_info(uid: str = Depends(_uid)) -> dict:
        return {
            "name": "caption-tool",
            "protocol": "mcp-shaped-http",
            "tools": [t["name"] for t in toolreg.TOOL_DEFS],
            "confirmTools": sorted(toolreg.CONFIRM_TOOLS),
        }

    @router.post("/tools/list")
    def mcp_tools_list(uid: str = Depends(_uid)) -> dict:
        return {"tools": toolreg.mcp_tool_list()}

    @router.post("/tools/call")
    def mcp_tools_call(body: ToolCallBody, uid: str = Depends(_uid)) -> dict:
        ctx = tool_ctx_factory()
        result = toolreg.execute(uid, body.name, body.arguments or {}, ctx)
        return {"ok": True, "name": body.name, "result": result}

    app.include_router(router)
    _log.info("MCP-shaped HTTP mounted at /mcp")
    return True
