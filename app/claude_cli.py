"""Opus text via the signed-in Claude Code CLI (subscription, not the API).

The Cloudflare container cannot keep a browser login. `claude setup-token`
produces CLAUDE_CODE_OAUTH_TOKEN. If ANTHROPIC_API_KEY is visible to the
process, the CLI bills the Anthropic API instead, so the child env drops it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Callable

CLAUDE_CLI_MODEL = (os.environ.get("CLAUDE_CLI_MODEL") or "").strip() or "opus"
_TIMEOUT_S = int(os.environ.get("CLAUDE_CLI_TIMEOUT_S") or "180")
_DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeCliError(RuntimeError):
    pass


def is_opus_model(model: str | None) -> bool:
    return "opus" in (model or "").strip().lower().replace("_", "-")


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Subscription env. The API key must not ride along."""
    env = dict(base if base is not None else os.environ)
    for key in _DROP_ENV:
        env.pop(key, None)
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    # Claude Code blocks --dangerously-skip-permissions as root unless sandboxed.
    env.setdefault("IS_SANDBOX", "1")
    return env


def _parse_stdout(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ClaudeCliError("claude CLI returned empty output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        if data.get("is_error") or data.get("subtype") == "error":
            raise ClaudeCliError(str(data.get("result") or data.get("error") or "claude CLI error"))
        result = data.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    raise ClaudeCliError("claude CLI JSON had no result text")


def complete(
    system: str,
    user: str,
    *,
    model: str | None = None,
    env: dict[str, str] | None = None,
    run: Callable[..., Any] | None = None,
    executable: str | None = None,
) -> str:
    """One non-interactive Opus turn. No tools. No API fallback."""
    prompt = (user or "").strip()
    if not prompt:
        raise ClaudeCliError("claude CLI prompt is empty")
    child = child_env(env)
    if not (child.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        raise ClaudeCliError(
            "CLAUDE_CODE_OAUTH_TOKEN is not set. "
            "Run `claude setup-token`, then `wrangler secret put CLAUDE_CODE_OAUTH_TOKEN`."
        )
    binary = (executable or os.environ.get("CLAUDE_BIN") or "").strip() or shutil.which("claude")
    if not binary:
        raise ClaudeCliError(
            "claude CLI is not installed in this container. "
            "Rebuild the image after installing @anthropic-ai/claude-code."
        )
    chosen = (model or CLAUDE_CLI_MODEL or "opus").strip() or "opus"
    cmd = [
        binary,
        "-p",
        "--model",
        chosen,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--permission-mode",
        "dontAsk",
        "--dangerously-skip-permissions",
    ]
    system_text = (system or "").strip()
    if system_text:
        cmd.extend(["--system-prompt", system_text])
    runner = run or subprocess.run
    try:
        proc = runner(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude CLI timed out after {_TIMEOUT_S}s") from exc
    except FileNotFoundError as exc:
        raise ClaudeCliError("claude CLI is not installed") from exc
    code = int(getattr(proc, "returncode", 1) or 0)
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "").strip()
    if code != 0:
        detail = stderr or stdout.strip() or f"exit {code}"
        raise ClaudeCliError(f"claude CLI failed: {detail[:500]}")
    return _parse_stdout(stdout)
