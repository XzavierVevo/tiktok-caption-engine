#!/usr/bin/env python3
"""MCP / Worker e2e smoke helpers.

Usage (container already running with INTERNAL_API_KEY unset for local):

  python scripts/e2e_mcp_smoke.py --base http://127.0.0.1:8765 --uid dev

Does not post to TikTok by itself — exercises thread + tool plumbing.
Full ship-gate (four live posts) still needs a logged-in account + wrangler deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def post(base: str, path: str, body: dict, *, key: str = "") -> dict:
    data = json.dumps(body).encode("utf-8")
    headers = {"content-type": "application/json"}
    if key:
        headers["x-internal-api-key"] = key
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} {path}: {raw}") from exc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("CAPTION_BASE") or "http://127.0.0.1:8765")
    ap.add_argument("--uid", default="dev")
    ap.add_argument("--internal-key", default=os.environ.get("INTERNAL_API_KEY") or "")
    args = ap.parse_args()

    thr = post(
        args.base,
        "/internal/mcp/threads",
        {
            "uid": args.uid,
            "action": "create",
            "title": "e2e-mcp",
            "ephemeral": True,
        },
        key=args.internal_key,
    )
    tid = thr.get("threadId")
    print("thread", tid)

    try:
        listed = post(
            args.base,
            "/internal/mcp/call",
            {"uid": args.uid, "threadId": tid, "name": "list_sources", "arguments": {}},
            key=args.internal_key,
        )
        print("list_sources", json.dumps(listed, indent=2)[:800])

        imgs = post(
            args.base,
            "/internal/mcp/call",
            {"uid": args.uid, "threadId": tid, "name": "list_images", "arguments": {}},
            key=args.internal_key,
        )
        print("list_images count", (imgs.get("count") if isinstance(imgs, dict) else None))

        print("OK — internal MCP plumbing works. Run web+MCP TikTok posts for ship gate.")
    finally:
        if tid:
            post(
                args.base,
                "/internal/mcp/threads",
                {"uid": args.uid, "action": "delete", "threadId": tid},
                key=args.internal_key,
            )
            print("deleted thread", tid)


if __name__ == "__main__":
    main()
