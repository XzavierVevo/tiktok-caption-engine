"""Multi-provider LLM calls (Anthropic, OpenAI, xAI, Gemini) with tool calling."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Iterator

from ..caption_markers import HOOK_CREATIVE_RULES, MARKER_RULES
from ..chat_models import get_api_key
from . import tools as toolreg

_log = logging.getLogger(__name__)

# Shared rules so every provider (chat, /all, /multiple, ask_models) means the
# same thing by "caption" / "hook": the burn-in line, not a brainstorm label.
CAPTION_WRITING_RULES = (
    HOOK_CREATIVE_RULES
    + """

What "caption" / "hook" means here:
- The EXACT on-screen TikTok text that will be burned onto the video (CLIP:/SEARCH:
  lines are production notes, never burned in).
- GOOD on-screen: "I've been scrolling for free this whole time", "My phone
  billed me in sleep", "This paid my rent last month".
- BAD: "Proof/receipts angle", "Casual discovery", "Social proof", "Lifestyle flex".
- HARD RULE: every caption must be fully readable on a 9:16 phone in one glance.
  Never write text that would sit under TikTok UI (Following/For You, the right
  like/comment/share column, username, post caption, Ad badge, Learn more, home
  bar). Prefer 1–3 short lines. If it would overflow, shorten it.
- If they want N captions, output N real caption lines. For “N each” on several
  videos, put a video heading, then N lines + CLIP/SEARCH under each heading.
"""
    + "\n"
    + MARKER_RULES
)

COMPARE_SYSTEM = """You are one of several models answering the same Caption Tool chat prompt
in parallel (/all or /multiple). Tools are OFF — you cannot list_sources, set_render_plan,
preview_plan, render, upload, or search Pinterest.

""" + CAPTION_WRITING_RULES + """

Rules:
- When they ask to create/write captions or hooks: output ONLY finished on-screen
  caption lines (numbered). No essays, no angle lists, no “here’s my approach”.
- Never tell them to type /preview, /render, or other slash commands. Never write
  UI instructions. Just the captions.
- If they attached clips/images, each Frame N in the attachment note matches the
  Nth image you see. Match what is on that frame. Group captions per video:
    ### Frame 1 — <name> (sourceId=<id>)
    1. <caption>
    2. <caption>
  Repeat for every attached video. If they ask for N per video, write exactly N
  finished lines under each heading.
- Do NOT pretend you saved a plan, called tools, or rendered anything.
- Do NOT write “(called …)” or tool JSON.
- No hashtags unless asked.
"""


# Most chats are writing hooks. Keep one stable brief, then add a short workflow
# note only when those tools were routed into the request.
COMPACT_SYSTEM_PROMPT = """You are Caption Tool's TikTok affiliate hook and caption operator.
Help write scroll-stopping on-screen hooks, match them to attached clips, and use tools for real app actions.

""" + CAPTION_WRITING_RULES + """
- Named offer: get_offer for pastCaptions, then generate_hooks (Opus, randomWords off). The brief above is the voice. Do not rewrite it.
- For captions tied to specific videos, call list_sources then set_render_plan. Do not merely paste a caption list in chat.
  If there is no library clip, search_pinterest (max 5–10 queries per idea) then pinterest_set show. The selector is how they pick. Do not ask them to paste pins.
- Use tools for real actions and never claim a render, upload, search, or save completed unless its tool succeeded.
- Plans are shared across chats. "Another plan" means use newPlan true; updating a plan replaces the active one.
- start_render, uploads, source adds, and deletes require the app confirmation card. Be concise after tool calls and report counts.
"""

_WORKFLOW_NOTES = {
    "pinterest": """Pinterest is enabled: use a short keyword query with search_pinterest, then pinterest_set action=show to populate the grid. Never invent pins in prose. Download only when asked.""",
    "carousel": """Native photo carousels use list_images then set_carousel_plan; use start_carousel_upload only when asked. They do not use burn-in rendering.""",
    "publish": """For TikTok publishing, render first when working from videos, then use its returned batchId to upload. Poll status tools rather than claiming completion. Fetch Spark codes only after posts exist.""",
    "meta": """For Meta ads, inspect existing campaigns, ad sets, or pixels when relevant. landingUrl is required and ads must be created paused.""",
    "business_center": """Business Center and campaign launch — use tools only. Never write a provision script.
Find a BC with list_bcs (query can be a profile number such as 264) then get_bc.
Warm a new BC: start_bc_warmup, then poll get_bc_job. Warm-up does not auto-launch Sales.
Finish an existing BC: continue_bc when warmup or white-hat is incomplete; mark_bc_payment when payment is still required. Extra ad accounts: set_bc_settings extraAdAccounts, then continue_bc.
Website link with no Instant Page: call ensure_dummy_pixel. It reuses a Pixel already on the Business Center or creates website-dummy.
After white-hat is posted, call start_sales_campaign. Do not wait for Scheduled. Do not call fetch_spark or start_spark_bc_launch unless the user pasted Spark codes.
Pass conversionBidUsd and dailyBudgetUsd on start_sales_campaign (or set_bc_settings first for defaults). Example: profile 264, bid 0.21, daily budget 2000. objective is sales or lead. Campaign and ads are published active.
Poll get_bc and list_sparks. Change a live campaign with update_campaign_bid, update_campaign_budget, and set_campaign_status.
Remake creatives with the caption tools, then start_sales_campaign on that profile.
get_tiktok_connection, sync_tiktok_insights, list_topups, and approve_topup cover connection, spend, and top-ups. Do not invent CPC, CPM, spend, or a finished job.""",
}


def system_prompt_for_tools(tool_names: frozenset[str] | None) -> str:
    names = tool_names or frozenset()
    parts = [COMPACT_SYSTEM_PROMPT]
    for group, group_tools in toolreg.CHAT_TOOL_GROUPS.items():
        if names & group_tools:
            parts.append(_WORKFLOW_NOTES[group])
    return "\n\n".join(parts)


def chat_completion(
    *,
    provider: str,
    model: str,
    messages: list[dict],
    allow_tools: bool = True,
    vision_images: list[dict] | None = None,
    system: str | None = None,
    tool_names: frozenset[str] | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """
    One non-streaming turn. Returns:
      { content: str, tool_calls: [{id, name, arguments}], usage: {...} }
    """
    sys = system_prompt_for_tools(tool_names) if system is None else system
    msgs = list(messages)
    if sys:
        msgs = [{"role": "system", "content": sys}] + msgs
    if vision_images:
        msgs = _with_vision(msgs, vision_images)
    if provider == "anthropic":
        return _anthropic(model, msgs, allow_tools, max_tokens=max_tokens, tool_names=tool_names)
    if provider in ("openai", "xai"):
        return _openai_compatible(
            provider, model, msgs, allow_tools, max_tokens=max_tokens, tool_names=tool_names
        )
    if provider == "gemini":
        return _gemini(model, msgs, allow_tools, max_tokens=max_tokens, tool_names=tool_names)
    raise ValueError(f"Unsupported provider: {provider}")


def _empty_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _add_usage(a: dict[str, int] | None, b: dict[str, int] | None) -> dict[str, int]:
    out = _empty_usage()
    for key in out:
        out[key] = int((a or {}).get(key) or 0) + int((b or {}).get(key) or 0)
    return out


def _anthropic_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None) if resp is not None else None
    if u is None:
        return _empty_usage()
    cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    if not cache_write:
        creation = getattr(u, "cache_creation", None)
        if creation is not None:
            cache_write = int(getattr(creation, "ephemeral_5m_input_tokens", 0) or 0) + int(
                getattr(creation, "ephemeral_1h_input_tokens", 0) or 0
            )
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": cache_write,
    }


def _openai_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None) if resp is not None else None
    if u is None:
        return _empty_usage()
    prompt = int(getattr(u, "prompt_tokens", 0) or 0)
    completion = int(getattr(u, "completion_tokens", 0) or 0)
    cached = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    cached = max(0, min(cached, prompt))
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": completion,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }


def _gemini_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage_metadata", None) if resp is not None else None
    if u is None:
        return _empty_usage()
    prompt = int(getattr(u, "prompt_token_count", 0) or 0)
    candidates = int(getattr(u, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(u, "thoughts_token_count", 0) or 0)
    cached = int(getattr(u, "cached_content_token_count", 0) or 0)
    cached = max(0, min(cached, prompt))
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": candidates + thoughts,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }


def _with_vision(messages: list[dict], images: list[dict]) -> list[dict]:
    msgs = [dict(m) for m in messages]
    for m in reversed(msgs):
        if m.get("role") == "tool":
            m["_vision"] = images
            return msgs
    # Do not insert a user turn after an unmatched tool_use — that 400s Anthropic.
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return msgs
    # Prefer attaching to the latest user message (e.g. /all compare prompt).
    for m in reversed(msgs):
        if m.get("role") == "user" and not _is_anthropic_tool_result_user(m):
            m["_vision"] = images
            return msgs
    msgs.append(
        {
            "role": "user",
            "content": "Visual references for the latest search/inspect result.",
            "_vision": images,
        }
    )
    return msgs


def _anthropic_tool_result_stub(tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(
            {
                "ok": False,
                "interrupted": True,
                "error": "No tool result was recorded; the user continued in chat.",
            }
        ),
    }


def _is_anthropic_tool_result_user(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(p, dict) and p.get("type") == "tool_result" for p in content
    )


def _anthropic_tool_use_ids(msg: dict) -> list[str]:
    content = msg.get("content")
    if msg.get("role") != "assistant" or not isinstance(content, list):
        return []
    return [
        str(p.get("id"))
        for p in content
        if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("id")
    ]


def _sanitize_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Claude requires tool_result in the next user message for every tool_use."""
    out: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and content in ("", None, []):
            i += 1
            continue
        if role == "assistant":
            tool_ids = _anthropic_tool_use_ids(msg)
            out.append(msg)
            i += 1
            if not tool_ids:
                continue
            combined: list[dict] = []
            got: set[str] = set()
            while i < n and _is_anthropic_tool_result_user(messages[i]):
                for part in messages[i].get("content") or []:
                    if not isinstance(part, dict) or part.get("type") != "tool_result":
                        continue
                    tid = str(part.get("tool_use_id") or "")
                    if not tid or tid in got:
                        continue
                    combined.append(part)
                    got.add(tid)
                i += 1
            for tid in tool_ids:
                if tid not in got:
                    combined.append(_anthropic_tool_result_stub(tid))
                    got.add(tid)
            out.append({"role": "user", "content": combined})
            continue
        if _is_anthropic_tool_result_user(msg):
            # Orphan tool_result with no preceding tool_use — drop it.
            i += 1
            continue
        out.append(msg)
        i += 1
    return out


def _opus_text_via_cli(messages: list[dict]) -> dict:
    from ..claude_cli import ClaudeCliError, complete

    system_parts: list[str] = []
    user_parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            bits = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    bits.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    bits.append(part)
            content = "\n".join(b for b in bits if b)
        text = str(content or "").strip()
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        elif role == "user":
            user_parts.append(text)
        else:
            user_parts.append(f"{role}: {text}")
    try:
        content = complete("\n\n".join(system_parts), "\n\n".join(user_parts))
    except ClaudeCliError as exc:
        raise ValueError(str(exc)) from exc
    return {"content": content, "tool_calls": [], "usage": _empty_usage()}


def _anthropic(
    model: str,
    messages: list[dict],
    allow_tools: bool,
    *,
    max_tokens: int | None = None,
    tool_names: frozenset[str] | None = None,
) -> dict:
    from ..claude_cli import is_opus_model

    if is_opus_model(model):
        if allow_tools:
            raise ValueError(
                "Opus with tools would call the Anthropic API. "
                "Use a non-Opus model for tool turns. "
                "Opus text runs on the Claude CLI subscription."
            )
        return _opus_text_via_cli(messages)

    from anthropic import Anthropic

    client = Anthropic(api_key=get_api_key("anthropic"))
    system = ""
    converted = []
    for m in messages:
        if m["role"] == "system":
            system = (system + "\n" + (m.get("content") or "")).strip()
            continue
        role = m["role"]
        if role == "tool":
            tid = str(m.get("tool_call_id") or "")
            if not tid:
                continue
            vision = m.get("_vision") or []
            tool_content: Any = m.get("content") or ""
            if vision:
                blocks: list[dict[str, Any]] = [
                    {"type": "text", "text": str(tool_content)[:6000]}
                ]
                for img in vision[:8]:
                    data = img.get("data")
                    mime = img.get("media_type") or "image/jpeg"
                    if not data:
                        continue
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": data,
                            },
                        }
                    )
                tool_content = blocks
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": tool_content,
                        }
                    ],
                }
            )
            if (
                len(converted) >= 2
                and converted[-2].get("role") == "user"
                and isinstance(converted[-2].get("content"), list)
                and converted[-2]["content"]
                and all(
                    isinstance(p, dict) and p.get("type") == "tool_result"
                    for p in converted[-2]["content"]
                )
            ):
                converted[-2]["content"].extend(converted[-1]["content"])
                converted.pop()
            continue
        if role == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                args = tc.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "tool",
                        "name": tc.get("name"),
                        "input": args or {},
                    }
                )
            converted.append({"role": "assistant", "content": blocks})
            continue
        vision = m.get("_vision") or []
        if vision and role == "user":
            blocks = []
            text = (m.get("content") or "").strip()
            if text:
                blocks.append({"type": "text", "text": text})
            for img in vision[:8]:
                data = img.get("data")
                mime = img.get("media_type") or "image/jpeg"
                if not data:
                    continue
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": data,
                        },
                    }
                )
            converted.append({"role": role, "content": blocks or (m.get("content") or "")})
            continue
        text = m.get("content") or ""
        if role in ("user", "assistant") and not m.get("tool_calls") and not str(text).strip():
            continue
        converted.append({"role": role, "content": text})

    converted = _sanitize_anthropic_messages(converted)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_tokens or 8192),
        "messages": converted,
    }
    # Sonnet 5 / Opus 5 default to high effort; thinking can consume
    # the whole max_tokens budget and leave an empty visible reply.
    mid = (model or "").lower()
    if any(
        tag in mid
        for tag in (
            "sonnet-5",
            "opus-5",
            "fable-5",
            "sonnet-4-6",
            "opus-4-6",
            "opus-4-7",
            "opus-4-8",
        )
    ):
        kwargs["output_config"] = {"effort": "low"}
    if system:
        # Explicitly cache the static instructions.  The final routed tool has
        # its own breakpoint below, which extends this cache across the stable
        # system + tool prefix on follow-up turns.
        kwargs["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    if allow_tools:
        kwargs["tools"] = toolreg.anthropic_tools(tool_names, cache_prefix=True)

    try:
        resp = client.messages.create(**kwargs)
    except Exception as exc:
        # Older gateways can proxy the Messages API without prompt caching.
        # Degrade to the identical uncached request instead of failing chat.
        if "cache_control" in str(exc).lower():
            if system:
                kwargs["system"] = system
            if allow_tools:
                kwargs["tools"] = toolreg.anthropic_tools(tool_names)
            resp = client.messages.create(**kwargs)
        elif "output_config" in kwargs and (
            isinstance(exc, TypeError)
            or "output_config" in str(exc)
            or "effort" in str(exc).lower()
        ):
            kwargs.pop("output_config", None)
            resp = client.messages.create(**kwargs)
        else:
            raise

    def _parse(resp_obj: Any) -> tuple[str, list[dict]]:
        content_parts = []
        tool_calls = []
        for block in resp_obj.content or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                content_parts.append(getattr(block, "text", "") or "")
            elif btype == "refusal":
                refused = (
                    getattr(block, "refusal", None)
                    or getattr(block, "text", None)
                    or ""
                )
                if refused:
                    content_parts.append(str(refused))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "arguments": dict(getattr(block, "input", None) or {}),
                    }
                )
        return "\n".join(content_parts).strip(), tool_calls

    content, tool_calls = _parse(resp)
    usage = _anthropic_usage(resp)
    if not content and not tool_calls:
        # Empty visible reply (often hidden thinking ate the budget).
        retry_kw = dict(kwargs)
        retry_kw.pop("output_config", None)
        retry_kw["max_tokens"] = max(int(retry_kw.get("max_tokens") or 8192), 16384)
        retry_kw["thinking"] = {"type": "disabled"}
        retry_resp = None
        try:
            retry_resp = client.messages.create(**retry_kw)
        except Exception:
            retry_kw.pop("thinking", None)
            try:
                retry_resp = client.messages.create(**retry_kw)
            except Exception:
                retry_resp = None
        if retry_resp is not None:
            resp = retry_resp
            content, tool_calls = _parse(resp)
            usage = _add_usage(usage, _anthropic_usage(resp))

    if not content and not tool_calls:
        stop = getattr(resp, "stop_reason", None) if resp is not None else "unknown"
        raise ValueError(
            f"Claude returned no text (stop_reason={stop}). "
            "Retry, or switch to Claude Sonnet 4.6."
        )
    return {"content": content, "tool_calls": tool_calls, "usage": usage}


def _supports_custom_temperature(provider: str, model: str) -> bool:
    """GPT-5 / o-series reasoning models only accept the default temperature."""
    if provider != "openai":
        return True
    mid = (model or "").lower()
    return not (
        mid.startswith("gpt-5")
        or mid.startswith("o1")
        or mid.startswith("o3")
        or mid.startswith("o4")
    )


def _gpt56_chat_tools_need_no_reasoning(provider: str, model: str) -> bool:
    """gpt-5.6* on Chat Completions rejects tools unless reasoning_effort is none."""
    if provider != "openai":
        return False
    return (model or "").lower().startswith("gpt-5.6")


def _openai_compatible(
    provider: str,
    model: str,
    messages: list[dict],
    allow_tools: bool,
    *,
    max_tokens: int | None = None,
    tool_names: frozenset[str] | None = None,
) -> dict:
    from openai import OpenAI

    if provider == "xai":
        client = OpenAI(api_key=get_api_key("xai"), base_url="https://api.x.ai/v1")
    else:
        client = OpenAI(api_key=get_api_key("openai"))

    oai_msgs = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            oai_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or "",
                    "content": m.get("content") or "",
                }
            )
            vision = m.get("_vision") or []
            if vision:
                parts: list[dict[str, Any]] = [
                    {
                        "type": "text",
                        "text": "Visuals for the latest tool result (match or write captions from these frames).",
                    }
                ]
                for img in vision[:8]:
                    data = img.get("data")
                    mime = img.get("media_type") or "image/jpeg"
                    if not data:
                        continue
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"},
                        }
                    )
                oai_msgs.append({"role": "user", "content": parts})
            continue
        entry: dict[str, Any] = {"role": role, "content": m.get("content") or ""}
        if role == "assistant" and m.get("tool_calls"):
            entry["tool_calls"] = [
                {
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc.get("name"),
                        "arguments": (
                            tc.get("arguments")
                            if isinstance(tc.get("arguments"), str)
                            else json.dumps(tc.get("arguments") or {})
                        ),
                    },
                }
                for i, tc in enumerate(m["tool_calls"])
            ]
        vision = m.get("_vision") or []
        if vision and role == "user":
            parts: list[dict[str, Any]] = [
                {"type": "text", "text": m.get("content") or "Visual references."}
            ]
            for img in vision[:8]:
                data = img.get("data")
                mime = img.get("media_type") or "image/jpeg"
                if not data:
                    continue
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    }
                )
            entry["content"] = parts
        oai_msgs.append(entry)

    # OpenAI prompt caching is automatic for eligible Chat Completions models.
    # Keep the routed tool schemas in deterministic order and prepend the
    # immutable system message above, so every turn has the same cacheable
    # prefix; user/history messages always remain at the end.
    kwargs: dict[str, Any] = {"model": model, "messages": oai_msgs}
    if max_tokens:
        mid = (model or "").lower()
        if provider == "openai" and (
            mid.startswith("gpt-5") or mid.startswith("o1") or mid.startswith("o3")
        ):
            kwargs["max_completion_tokens"] = int(max_tokens)
        else:
            kwargs["max_tokens"] = int(max_tokens)
    # GPT-5+ reasoning models reject non-default temperature (only 1 is allowed).
    if _supports_custom_temperature(provider, model):
        kwargs["temperature"] = 0.7
    if allow_tools:
        kwargs["tools"] = toolreg.openai_tools(tool_names)
        kwargs["tool_choice"] = "auto"
        if _gpt56_chat_tools_need_no_reasoning(provider, model):
            kwargs["reasoning_effort"] = "none"

    def _create(kw: dict[str, Any]):
        try:
            return client.chat.completions.create(**kw)
        except TypeError:
            effort = kw.pop("reasoning_effort", None)
            if effort is None:
                raise
            extra = dict(kw.get("extra_body") or {})
            extra["reasoning_effort"] = effort
            kw["extra_body"] = extra
            return client.chat.completions.create(**kw)

    try:
        resp = _create(kwargs)
    except Exception as exc:
        msg = str(exc).lower()
        retried = False
        if "temperature" in kwargs and "temperature" in msg:
            kwargs.pop("temperature", None)
            retried = True
        if (
            allow_tools
            and "reasoning_effort" in msg
            and kwargs.get("reasoning_effort") != "none"
        ):
            kwargs["reasoning_effort"] = "none"
            retried = True
        if retried:
            resp = _create(kwargs)
        else:
            raise
    choice = resp.choices[0].message if resp.choices else None
    usage = _openai_usage(resp)
    if not choice:
        return {"content": "", "tool_calls": [], "usage": usage}
    tool_calls = []
    for tc in choice.tool_calls or []:
        args_raw = tc.function.arguments if tc.function else "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(
            {
                "id": tc.id,
                "name": tc.function.name if tc.function else "",
                "arguments": args,
            }
        )
    return {"content": (choice.content or "").strip(), "tool_calls": tool_calls, "usage": usage}


def _sig_b64(value: Any) -> str | None:
    if value is None or value == b"" or value == "":
        return None
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, str):
        return value
    return None


def _sig_bytes(value: Any) -> bytes | None:
    if value is None or value == b"" or value == "":
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return base64.b64decode(value)
        except Exception:
            return None
    return None


def _plain_args(args: Any) -> dict:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {}
    if not args:
        return {}
    try:
        return json.loads(json.dumps(dict(args)))
    except (TypeError, ValueError):
        return dict(args) if isinstance(args, dict) else {}


def _capture_gemini_response(parts: list[Any]) -> dict[str, Any]:
    gemini_parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    content_parts: list[str] = []
    for part in parts:
        is_thought = bool(getattr(part, "thought", False))
        fc = getattr(part, "function_call", None)
        text = getattr(part, "text", None)
        sig = _sig_b64(getattr(part, "thought_signature", None))
        entry: dict[str, Any] = {}
        if sig:
            entry["thought_signature"] = sig
        if is_thought:
            entry["thought"] = True
        if fc:
            args = _plain_args(getattr(fc, "args", None))
            entry["function_call"] = {"name": fc.name, "args": args}
            tc: dict[str, Any] = {
                "id": f"gemini_{fc.name}_{len(tool_calls)}",
                "name": fc.name,
                "arguments": args,
            }
            if sig:
                tc["thought_signature"] = sig
            tool_calls.append(tc)
        elif isinstance(text, str):
            entry["text"] = text
            if text and not is_thought:
                content_parts.append(text)
        if entry:
            gemini_parts.append(entry)
    return {
        "content": "\n".join(content_parts).strip(),
        "tool_calls": tool_calls,
        "gemini_parts": gemini_parts,
    }


def _restore_gemini_part(p: dict, types: Any) -> Any:
    kwargs: dict[str, Any] = {}
    sig = _sig_bytes(p.get("thought_signature"))
    if sig:
        kwargs["thought_signature"] = sig
    if p.get("thought"):
        kwargs["thought"] = True
    fc = p.get("function_call")
    if fc:
        kwargs["function_call"] = types.FunctionCall(
            name=fc.get("name") or "tool",
            args=fc.get("args") or {},
        )
    elif p.get("text") is not None:
        kwargs["text"] = p.get("text") or ""
    return types.Part(**kwargs)


def _function_calls_signed(parts: list[dict]) -> bool:
    fcs = [p for p in parts if p.get("function_call")]
    return bool(fcs) and all(p.get("thought_signature") for p in fcs)


def _assistant_model_parts(m: dict, types: Any) -> tuple[list[Any], bool]:
    """Return (parts, echo_function_responses).

    Gemini 3 rejects functionCall parts without thought_signature, so unsigned
    history is flattened to plain text instead of echoed as tool calls.
    """
    stored = m.get("gemini_parts")
    if isinstance(stored, list) and stored:
        has_fc = any(p.get("function_call") for p in stored if isinstance(p, dict))
        if not has_fc or _function_calls_signed(stored):
            return [_restore_gemini_part(p, types) for p in stored if p], True
        # Fall through and flatten if any function call is unsigned.

    parts: list[Any] = []
    text = (m.get("content") or "").strip()
    tool_calls = m.get("tool_calls") or []
    signed = bool(tool_calls) and all(
        tc.get("thought_signature") for tc in tool_calls
    )
    if text:
        parts.append(types.Part(text=text))
    if tool_calls and signed:
        for tc in tool_calls:
            args = _plain_args(tc.get("arguments"))
            kwargs: dict[str, Any] = {
                "function_call": types.FunctionCall(
                    name=tc.get("name") or "tool",
                    args=args,
                )
            }
            sig = _sig_bytes(tc.get("thought_signature"))
            if sig:
                kwargs["thought_signature"] = sig
            parts.append(types.Part(**kwargs))
        return parts, True
    if tool_calls and not signed:
        if not text:
            names = ", ".join(tc.get("name") or "tool" for tc in tool_calls)
            parts.append(types.Part(text=f"(called {names})"))
        return parts, False
    return parts, True


def _gemini(
    model: str,
    messages: list[dict],
    allow_tools: bool,
    *,
    max_tokens: int | None = None,
    tool_names: frozenset[str] | None = None,
) -> dict:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ValueError("google-genai package is not installed") from exc

    client = genai.Client(api_key=get_api_key("gemini"))
    system = ""
    contents = []
    pending_user_parts: list[Any] = []
    echo_function_responses = True

    def flush_user_parts() -> None:
        if not pending_user_parts:
            return
        contents.append(types.Content(role="user", parts=list(pending_user_parts)))
        pending_user_parts.clear()

    for m in messages:
        role = m.get("role")
        if role == "system":
            system = (system + "\n" + (m.get("content") or "")).strip()
            continue
        if role == "tool":
            raw = m.get("content") or ""
            name = m.get("name") or "tool"
            if echo_function_responses:
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    parsed = {"result": raw}
                if not isinstance(parsed, dict):
                    parsed = {"result": parsed}
                pending_user_parts.append(
                    types.Part.from_function_response(name=name, response=parsed)
                )
            else:
                pending_user_parts.append(
                    types.Part(text=f"Tool {name} result:\n{raw}")
                )
            for img in m.get("_vision") or []:
                data = img.get("data")
                mime = img.get("media_type") or "image/jpeg"
                if not data:
                    continue
                try:
                    blob = base64.b64decode(data)
                    pending_user_parts.append(
                        types.Part.from_bytes(data=blob, mime_type=mime)
                    )
                except Exception:
                    continue
            continue
        flush_user_parts()
        if role == "assistant":
            parts, echo_function_responses = _assistant_model_parts(m, types)
        else:
            echo_function_responses = True
            parts = []
            text = (m.get("content") or "").strip()
            if text:
                parts.append(types.Part(text=text))
            for img in m.get("_vision") or []:
                data = img.get("data")
                mime = img.get("media_type") or "image/jpeg"
                if not data:
                    continue
                try:
                    blob = base64.b64decode(data)
                    parts.append(types.Part.from_bytes(data=blob, mime_type=mime))
                except Exception:
                    continue
        if not parts:
            continue
        gemini_role = "user" if role == "user" else "model"
        contents.append(types.Content(role=gemini_role, parts=parts))
    flush_user_parts()

    config_kwargs: dict[str, Any] = {}
    if system:
        config_kwargs["system_instruction"] = system
    if max_tokens:
        config_kwargs["max_output_tokens"] = int(max_tokens)
    if allow_tools:
        decls = [
            types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=t["parameters"],
            )
            for t in toolreg.gemini_tools(tool_names)
        ]
        config_kwargs["tools"] = [types.Tool(function_declarations=decls)]
        config_kwargs["automatic_function_calling"] = (
            types.AutomaticFunctionCallingConfig(disable=True)
        )

    thinking_keys = False
    think_cfg = None
    for ctor in (
        lambda: types.ThinkingConfig(thinking_budget=0),
        lambda: types.ThinkingConfig(include_thoughts=False),
    ):
        try:
            think_cfg = ctor()
            break
        except (TypeError, ValueError):
            continue
    if think_cfg is not None:
        try:
            types.GenerateContentConfig(**{**config_kwargs, "thinking_config": think_cfg})
            config_kwargs["thinking_config"] = think_cfg
            thinking_keys = True
        except (TypeError, ValueError):
            pass

    def _generate(cfg: dict[str, Any]):
        return client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg) if cfg else None,
        )

    try:
        resp = _generate(config_kwargs)
    except Exception:
        if thinking_keys:
            config_kwargs.pop("thinking_config", None)
            resp = _generate(config_kwargs)
        else:
            raise

    captured: dict[str, Any] = {
        "content": "",
        "tool_calls": [],
        "gemini_parts": [],
    }
    candidates = getattr(resp, "candidates", None) or []
    finish_reason = None
    for cand in candidates:
        finish_reason = getattr(cand, "finish_reason", None)
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        captured = _capture_gemini_response(parts)
        if captured.get("content") or captured.get("tool_calls"):
            break
    if not captured.get("content") and not captured.get("tool_calls"):
        fallback = ""
        try:
            fallback = (getattr(resp, "text", None) or "").strip()
        except Exception:
            fallback = ""
        if fallback:
            captured["content"] = fallback
        else:
            raise ValueError(
                "Empty reply from model"
                + (f" (finish_reason={finish_reason})" if finish_reason else "")
            )
    captured["usage"] = _gemini_usage(resp)
    return captured
