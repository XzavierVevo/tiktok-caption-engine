#!/usr/bin/env python3
"""Regenerate worker/src/mcp/tool-defs.json from app.agent.tools.TOOL_DEFS."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import tools as t  # noqa: E402

out = []
for d in t.TOOL_DEFS:
    out.append(
        {
            "name": d["name"],
            "description": d["description"],
            "inputSchema": d["parameters"],
            "confirm": bool(d.get("confirm")),
        }
    )

extra = [
    {
        "name": "create_thread",
        "description": "Create a chat thread for MCP plans/uploads/sparks. Returns threadId.",
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_threads",
        "description": "List recent chat threads for this user.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "set_active_thread",
        "description": "Set the active threadId for subsequent tools in this MCP session.",
        "inputSchema": {
            "type": "object",
            "properties": {"threadId": {"type": "string"}},
            "required": ["threadId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "confirm_tool",
        "description": "Execute a previously gated confirm tool by confirmId.",
        "inputSchema": {
            "type": "object",
            "properties": {"confirmId": {"type": "string"}},
            "required": ["confirmId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_spark_job",
        "description": "Poll a spark job by id (from fetch_spark).",
        "inputSchema": {
            "type": "object",
            "properties": {"jobId": {"type": "string"}},
            "required": ["jobId"],
            "additionalProperties": False,
        },
    },
]

for row in out:
    if row["name"] in ("fetch_spark", "start_carousel_upload"):
        props = row["inputSchema"].setdefault("properties", {})
        if "wait" in props:
            props["wait"]["description"] = (
                "Ignored — always returns jobId immediately; poll status tools instead."
            )
        props["idempotencyKey"] = {
            "type": "string",
            "description": "Retry-safe key; reuse on client retries",
        }
    if row["name"] in ("start_render", "start_tiktok_upload", "start_facebook_ads"):
        props = row["inputSchema"].setdefault("properties", {})
        props["idempotencyKey"] = {
            "type": "string",
            "description": "Retry-safe key; reuse on client retries",
        }

dest = ROOT / "worker" / "src" / "mcp" / "tool-defs.json"
dest.write_text(json.dumps(extra + out, indent=2), encoding="utf-8")
print(f"wrote {dest} ({len(extra) + len(out)} tools)")
