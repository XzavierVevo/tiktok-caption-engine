"""Chat agent loop: message → model → tools (with confirm gate) → SSE events."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterator

from .. import chat_models
from .. import chat_store
from .. import usage as usage_mod
from . import caption_pick as cap_pick
from . import perplexity
from . import providers
from . import tools as toolreg

_log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
BC_TOOL_ROUNDS = 12


def tool_round_limit(tool_names: frozenset[str] | set[str] | None) -> int:
    """BC turns need list → continue → pay → launch → poll in one chat turn."""
    names = tool_names or frozenset()
    if names & toolreg.CHAT_TOOL_GROUPS["business_center"]:
        return BC_TOOL_ROUNDS
    return MAX_TOOL_ROUNDS


def _record_completion(
    uid: str,
    thread_id: str,
    provider: str,
    model_id: str,
    result: dict | None,
    *,
    kind: str = "chat",
) -> None:
    try:
        usage_mod.record_llm(
            uid,
            thread_id=thread_id,
            provider=provider,
            model=model_id,
            usage=(result or {}).get("usage"),
            kind=kind,
        )
    except Exception:
        _log.debug("usage record failed", exc_info=True)


def _saved_chat_models(uid: str) -> dict[str, str]:
    try:
        from .. import workspace as ws

        return chat_models.normalize_provider_models(
            ws.load_workspace(uid).get("chatModels")
        )
    except Exception:
        return {}


def _remember_model_prefs(
    uid: str,
    thread_id: str,
    *,
    model_id: str | None = None,
    provider_models: dict | None = None,
) -> dict | None:
    """Persist the last model per provider on the thread and user workspace."""
    thread = chat_store.get_thread(uid, thread_id)
    prefer, by_provider = chat_models.prefs_for_compare(
        model_id=model_id,
        provider_models=provider_models,
        thread=thread,
        saved_models=_saved_chat_models(uid),
    )
    resolved = None
    if prefer:
        try:
            resolved = chat_models.resolve_model(prefer)
            by_provider[resolved["provider"]] = resolved["id"]
        except ValueError:
            resolved = None
    kwargs: dict = {"provider_models": by_provider}
    if resolved:
        kwargs["model"] = resolved["id"]
        kwargs["provider"] = resolved["provider"]
    chat_store.update_thread(uid, thread_id, **kwargs)
    try:
        from .. import workspace as ws

        patch: dict = {"chatModels": by_provider}
        if resolved:
            patch["chatProvider"] = resolved["provider"]
        ws.save_workspace(uid, patch)
    except Exception:
        _log.debug("chat model prefs save failed", exc_info=True)
    return resolved


SLASH_ALIASES = {
    "research": "research",
    "deep": "research",
    "deepresearch": "research",
    "pplx": "research",
    "perplexity": "research",
    "all": "all",
    "compare": "all",
    "bots": "all",
    "each": "all",
    "multiple": "multiple",
    "multi": "multiple",
    "models": "multiple",
    "pinterest": "pinterest",
    "pintrest": "pinterest",  # common misspelling
    "pintrist": "pinterest",
    "pin": "pinterest",
    "pins": "pinterest",
    "help": "help",
    "preview": "preview",
    "plan": "preview",
    "render": "render",
    "burn": "render",
}

HELP_TEXT = (
    "Slash commands\n"
    "/all <prompt>      — Every configured AI answers with this chat’s context "
    "(shown in rows). Each provider uses the last model you picked for it "
    "in the dropdowns (remembered across chats and /all). "
    "Text-only — cannot save a plan or call tools; pick captions into Plan after. "
    "Aliases: /compare, /bots\n"
    "/multiple <prompt> — Pick specific models (any mix of providers), then run "
    "the same prompt on each in parallel (also text-only). In the UI, choose "
    "models when you insert /multiple; or type ids: "
    "/multiple claude-sonnet-5,gpt-5.4 <prompt>. "
    "Aliases: /multi, /models\n"
    "/research <topic>  — In-depth web research via Perplexity (1–3+ minutes)\n"
    "/pinterest … — Pinterest mode: the bot reads your message and searches, "
    "reopens pins from this chat, or downloads — you don’t pick the tool. "
    "Aliases: /pin, /pins\n"
    "/preview           — Spreadsheet of your render plans across chats (sidebar to "
    "switch between them), then Render or Upload. Alias: /plan.\n"
    "/render            — Same plan sheet, ready to burn-in captions. Alias: /burn\n"
    "/help              — Show this list\n\n"
    "Aliases for research: /deep, /pplx\n\n"
    "Drop videos or images onto the composer (or pick from your library). "
    "Paste a TikTok ad URL to analyze it, find similar Pinterest images, and "
    "write caption variations. "
    "The model gets the first frame plus pin/file info — not the full video. "
    "Works with /all, /multiple, and /pinterest too. Plan rows keep which "
    "model wrote each caption so mixed /multiple + Pinterest batches render fine."
)


def list_slash_commands() -> list[dict]:
    from ..pinterest import configured as pinterest_configured

    return [
        {
            "id": "all",
            "cmd": "/all",
            "hint": "Every AI answers; uses the last model you picked for each provider",
            "enabled": bool(chat_models.compare_models()),
        },
        {
            "id": "multiple",
            "cmd": "/multiple",
            "hint": "Pick specific models from any providers, then run one prompt on all of them",
            "enabled": bool(chat_models.compare_models()),
        },
        {
            "id": "research",
            "cmd": "/research",
            "hint": "Deep research with Perplexity",
            "enabled": perplexity.configured(),
        },
        {
            "id": "pinterest",
            "cmd": "/pinterest",
            "hint": "bot picks search, reopen, or download from your message",
            "enabled": pinterest_configured(),
        },
        {
            "id": "preview",
            "cmd": "/preview",
            "hint": "Spreadsheet of what will render — then Render or Upload",
            "enabled": True,
        },
        {
            "id": "render",
            "cmd": "/render",
            "hint": "Open the render plan and burn-in selected captions",
            "enabled": True,
        },
        {
            "id": "help",
            "cmd": "/help",
            "hint": "List chat commands",
            "enabled": True,
        },
    ]


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def parse_slash(text: str) -> tuple[str, str] | None:
    s = (text or "").strip()
    if not s.startswith("/") or s.startswith("//"):
        return None
    parts = s.split(None, 1)
    cmd = parts[0][1:].lower().rstrip(":")
    arg = parts[1].strip() if len(parts) > 1 else ""
    mapped = SLASH_ALIASES.get(cmd)
    if not mapped:
        return None
    return mapped, arg


_VIDEO_NOUN = r"(?:videos?|pins?|clips?)"


def requested_video_count(text: str) -> int | None:
    m = re.search(
        rf"(?:best|top)?\s*(\d{{1,2}})\s+{_VIDEO_NOUN}\b",
        text or "",
        re.I,
    )
    if not m:
        return None
    n = int(m.group(1))
    if 1 <= n <= 50:
        return n
    return None


def wants_pin_picks(text: str) -> bool:
    """True when the user asked to choose N videos, not just browse inspiration."""
    t = text or ""
    if re.search(rf"\b(?:best|top)\s+\d{{1,2}}\s+{_VIDEO_NOUN}\b", t, re.I):
        return True
    if re.search(
        rf"\b(?:find|pick|choose|select|use|get|need|want)\b.{{0,80}}\b\d{{1,2}}\s+{_VIDEO_NOUN}\b",
        t,
        re.I,
    ):
        return True
    if re.search(
        rf"\b\d{{1,2}}\s+{_VIDEO_NOUN}\b.{{0,80}}\b(?:use|caption|render|approv|download)\b",
        t,
        re.I,
    ):
        return True
    if re.search(
        rf"\b(?:find|show|get|search|pull)\b.{{0,40}}\b(?:the\s+)?{_VIDEO_NOUN}\b",
        t,
        re.I,
    ):
        return True
    return False


_DIRECTIVE_ONLY = re.compile(
    r"^(?:yes|yep|yeah|ok|okay|sure|now|please|do\s+it)"
    r"(?:\s+(?:yes|yep|yeah|ok|okay|sure|now|please|do\s+it))*"
    r"(?:\s*[,.!]*)?$",
    re.I,
)
_FIND_VIDEOS_ONLY = re.compile(
    rf"^(?:yes|yep|yeah|ok|okay|sure|now|please)?"
    rf"(?:\s*(?:yes|ok|okay|sure|now|please))*"
    rf"\s*(?:find|show|get|search|pull)\s+(?:the\s+)?{_VIDEO_NOUN}"
    rf"(?:\s+please)?\s*$",
    re.I,
)


def _strip_pinterest_slash(text: str) -> str:
    return re.sub(
        r"^/(?:pinterest|pintrest|pintrist|pin|pins)\b",
        "",
        (text or "").strip(),
        flags=re.I,
    ).strip(" \t:-")


def _last_pinterest_query(thread: dict | None) -> str:
    for m in reversed((thread or {}).get("messages") or []):
        if m.get("role") != "tool" or m.get("name") != "search_pinterest":
            continue
        try:
            data = json.loads(m.get("content") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        q = str(data.get("query") or "").strip()
        # Skip meta sentences that were wrongly used as queries in older turns.
        if q and not _pinterest_args_are_meta(q):
            return q
    return ""


def _pinterest_args_are_meta(text: str) -> bool:
    """True when args are instructions about history/UI — not a keyword search topic."""
    t = _strip_pinterest_slash(text)
    if not t:
        return False
    if _DIRECTIVE_ONLY.match(t) or _FIND_VIDEOS_ONLY.match(t):
        return True
    # Explicit "search for / find X" with a real topic after → not meta.
    if re.search(
        r"\b(?:search|find|look\s+up)\s+(?:for|about)\s+\S+",
        t,
        re.I,
    ) and not re.search(
        r"\b(?:chat\s+history|same\s+(?:angles?|videos?|pins?|clips?)|"
        r"already\s+(?:found|shown|got|have)|from\s+(?:this\s+)?(?:chat|history)|"
        r"past\s+(?:pins?|videos?|searches?)|previous\s+(?:pins?|videos?|search))\b",
        t,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:"
        r"chat\s+history|message\s+history|this\s+chat|this\s+thread|"
        r"past\s+(?:pins?|videos?|searches?|results?)|"
        r"previous\s+(?:pins?|videos?|searches?|results?)|"
        r"already\s+(?:found|shown|got|have|saved)|"
        r"you(?:'ve| have)\s+(?:got|found|shown|searched)|"
        r"same\s+(?:angles?|videos?|pins?|clips?|results?)|"
        r"from\s+(?:the\s+)?(?:bank|history|grid)|"
        r"pull\s+(?:from|up)\s+(?:history|bank|past)|"
        r"re-?find|reuse|resurfac(?:e|ing)|bring\s+back|"
        r"new\s+system|using\s+the\s+(?:same|chat)|"
        r"show\s+(?:me\s+)?(?:all|those|these|the)|"
        r"what\s+(?:you|we've)\s+(?:got|found)"
        r")\b",
        t,
        re.I,
    ):
        return True
    # Long instructional sentences are almost never a Pinterest keyword.
    if len(t) > 80 and re.search(
        r"\b(?:using|refind|history|system|angles?|same)\b", t, re.I
    ):
        return True
    return False


def _wants_reuse_last_search_query(text: str) -> bool:
    """User explicitly asked to search again with the prior keyword."""
    t = _strip_pinterest_slash(text)
    if not t:
        return False
    return bool(
        re.search(
            r"\b(?:"
            r"(?:search|find)\s+(?:again|that\s+again)|"
            r"(?:same|last|previous)\s+(?:search|query|topic|keyword)|"
            r"search\s+(?:for\s+)?(?:that|it|the\s+same)\s+again|"
            r"use\s+(?:the\s+)?(?:last|previous|same)\s+(?:search|query|topic)"
            r")\b",
            t,
            re.I,
        )
    )


def _pinterest_topic(query: str, user_text: str, thread: dict | None = None) -> str:
    """Turn /pinterest args into a usable search keyword (never meta prose)."""
    raw = (query or "").strip() or _strip_pinterest_slash(user_text)
    raw = raw.strip(" \t:-")
    if _wants_reuse_last_search_query(f"{query or ''} {user_text or ''}"):
        prev = _last_pinterest_query(thread)
        return prev
    if not raw or _DIRECTIVE_ONLY.match(raw) or _FIND_VIDEOS_ONLY.match(raw):
        return ""
    if _pinterest_args_are_meta(raw):
        return ""
    return raw


def wants_pin_show(text: str) -> bool:
    """True when the user wants the accumulated pin grid, not a new search."""
    t = (text or "").strip()
    if not t:
        return False
    if wants_pin_download(t):
        return False
    # Explicit show-all / show-grid style asks
    if re.search(
        r"\b(?:show|list|display|see|view)\b.{0,40}\b(?:all|every|the)?\s*"
        r"(?:the\s+)?(?:pins?|videos?|clips?|grid|results?)\b",
        t,
        re.I,
    ):
        return True
    if re.search(
        r"\b(?:all|every)\s+(?:the\s+)?(?:pins?|videos?)\b.{0,40}\b(?:got|found|have|shown)\b",
        t,
        re.I,
    ):
        return True
    if re.search(
        r"\b(?:pins?|videos?)\s+(?:you(?:'ve| have)?\s+)?(?:got|found|shown)\b",
        t,
        re.I,
    ):
        return True
    # Pull past pins from this chat / bank — not a new Pinterest search.
    if _pinterest_args_are_meta(t) and not _wants_reuse_last_search_query(t):
        return True
    # Bare "/pinterest show" / "/pinterest all" / "/pinterest grid"
    bare = _strip_pinterest_slash(t)
    if re.fullmatch(r"(?:show|all|grid|bank|results?|history)(?:\s+all)?", bare, re.I):
        return True
    return False


def _pinterest_after_search_prompt(topic: str, user_text: str, pin_count: int) -> str:
    n = requested_video_count(user_text) or requested_video_count(topic)
    pick_n = f" the best {n}" if n else " the best"
    return (
        f"{(user_text or '').strip()}\n\n"
        f"search_pinterest already ran for: {topic} ({pin_count} pin(s) in results). "
        "Pins are in the thread bank but NOT yet in the chat grid.\n\n"
        f"You MUST call pinterest_set with action=\"show\" and{pick_n} pins "
        "(copy pinUrl, title, thumbnail, previewUrl, previewKind from the search "
        "result). Use mode=\"replace\". Never invent pins. Never claim the grid "
        "updated without calling pinterest_set.\n"
        "After showing, briefly note angles/patterns if useful. Users click pins "
        "to attach without downloading. "
        "Only call propose_ideas if they asked for captioned approve/deny cards. "
        "Only download via pinterest_set action=\"download\" when they ask."
    )


def _pinterest_search_prompt(topic: str, user_text: str) -> str:
    n = requested_video_count(user_text) or requested_video_count(topic)
    limit_hint = "limit 20" if n or wants_pin_picks(user_text) else "limit 12"
    return (
        f"{(user_text or '').strip()}\n\n"
        f"Search Pinterest about: {topic}\n"
        f"1) Call search_pinterest with {limit_hint}.\n"
        "2) Then call pinterest_set with action=\"show\" and the best pins "
        "(or all if few) — copy pinUrl/title/thumbnail/preview fields from search. "
        "mode=\"replace\".\n"
        "Never invent pins in prose. The grid only updates via pinterest_set show. "
        "propose_ideas only for captioned approve/deny. "
        "Download only with pinterest_set action=\"download\" when asked."
    )


def wants_pin_download(text: str) -> bool:
    """True when the user asked to download/save pins already found."""
    t = text or ""
    if re.search(
        r"\b(don'?t|do\s+not|not\s+yet|later|wait|more\s+ideas|find\s+more|search\s+again|new\s+pins)\b",
        t,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:download|save|import|add)\b.{0,60}\b(?:those|these|them|the|selected|attached|found)?\s*(?:pins?|videos?|clips?)\b",
        t,
        re.I,
    ):
        return True
    if re.search(
        r"\b(?:those|these|them|selected|attached|found)\s+(?:pins?|videos?|clips?)\b.{0,40}\b(?:download|save|import)\b",
        t,
        re.I,
    ):
        return True
    if re.search(r"\b(?:re)?download\b", t, re.I) and re.search(
        rf"\b{_VIDEO_NOUN}\b", t, re.I
    ):
        return True
    return False


def _parse_tool_json(content: str) -> dict:
    try:
        data = json.loads(content or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _pins_from_attachments(attachments: list[dict] | None) -> list[dict]:
    return toolreg.normalize_pins(
        [
            {
                "pinUrl": a.get("pinUrl") or a.get("url"),
                "title": a.get("title") or a.get("name"),
                "thumbnail": a.get("thumbUrl") or a.get("thumbnail"),
                "previewUrl": a.get("previewUrl"),
                "previewKind": a.get("previewKind"),
                "duration": a.get("duration"),
                "width": a.get("width"),
                "height": a.get("height"),
            }
            for a in (attachments or [])
            if isinstance(a, dict)
            and (
                (a.get("kind") or "").lower() == "pin"
                or "pinterest.com" in str(a.get("pinUrl") or a.get("url") or "").lower()
            )
        ]
    )


def _pins_to_download(
    uid: str,
    thread_id: str,
    attachments: list[dict] | None = None,
    explicit_pins: list[dict] | None = None,
) -> list[dict]:
    if explicit_pins:
        pins = toolreg.normalize_pins(explicit_pins)
        if pins:
            return pins
    attached = _pins_from_attachments(attachments)
    if attached:
        return attached
    shown = chat_store.get_pinterest_pins(uid, thread_id, which="shown")
    if shown:
        return toolreg.normalize_pins(shown)
    bank = chat_store.get_pinterest_pins(uid, thread_id, which="bank")
    return toolreg.normalize_pins(bank)


def _emit_pinterest_show(
    *,
    query: str = "",
    pins: list[dict],
    mode: str = "replace",
) -> Iterator[str]:
    yield _sse(
        "pinterest_results",
        {
            "query": query or "",
            "pins": pins,
            "count": len(pins),
            "mode": mode,
            "source": "pinterest_set",
        },
    )


def _run_pinterest_show(
    uid: str,
    thread_id: str,
    user_text: str,
    *,
    ctx: toolreg.ToolContext,
    which: str = "bank",
) -> Iterator[str]:
    """Show accumulated pins in the grid — no new search."""
    yield _sse("meta", {"command": "pinterest", "action": "show"})
    chat_store.append_message(uid, thread_id, {"role": "user", "content": user_text})
    pins = toolreg.normalize_pins(
        chat_store.get_pinterest_pins(uid, thread_id, which=which)
    )
    if not pins:
        msg = (
            "No Pinterest pins saved in this chat yet. Run /pinterest <topic> to "
            "search, then ask again to show all."
        )
        yield _sse("token", {"text": msg})
        chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
        yield _sse("done", {"threadId": thread_id})
        return

    call_id = uuid.uuid4().hex[:10]
    args = {
        "action": "show",
        "mode": "replace",
        "query": "all pins in this chat",
        "pins": pins,
    }
    yield _sse("status", {"message": f"Showing {len(pins)} pin(s)…"})
    yield _sse(
        "tool_start", {"name": "pinterest_set", "id": call_id, "arguments": args}
    )
    out = toolreg.execute(uid, "pinterest_set", args, ctx)
    chat_store.append_message(
        uid,
        thread_id,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "name": "pinterest_set", "arguments": args}
            ],
        },
    )
    slim = _persist_tool_result(
        uid, thread_id, name="pinterest_set", call_id=call_id, result=out
    )
    yield _sse("tool_result", {"name": "pinterest_set", "id": call_id, "result": slim})
    yield from _emit_pinterest_show(
        query=slim.get("query") or "all pins",
        pins=slim.get("pins") or pins,
        mode="replace",
    )
    msg = f"Showing {len(pins)} pin(s) from this chat. Click any to attach."
    chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
    yield _sse("token", {"text": msg})
    yield _sse("done", {"threadId": thread_id})


def _run_pinterest_download(
    uid: str,
    thread_id: str,
    user_text: str,
    *,
    ctx: toolreg.ToolContext,
    attachments: list[dict] | None = None,
    explicit_pins: list[dict] | None = None,
    append_user: bool = True,
) -> Iterator[str]:
    """Download pins (attachments → shown → bank)."""
    yield _sse("meta", {"command": "pinterest", "action": "download"})
    if append_user:
        chat_store.append_message(uid, thread_id, {"role": "user", "content": user_text})
    pins = _pins_to_download(
        uid, thread_id, attachments=attachments, explicit_pins=explicit_pins
    )
    if not pins:
        msg = (
            "No Pinterest videos to download yet. Search with /pinterest, click the "
            "pins you want (they attach to chat), then ask me to download — or say "
            "download after pins are shown in the grid."
        )
        yield _sse("token", {"text": msg})
        chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
        yield _sse("done", {"threadId": thread_id})
        return

    n = len(pins)
    yield _sse(
        "status",
        {"message": f"Downloading {n} Pinterest video{'s' if n != 1 else ''}…"},
    )
    call_id = uuid.uuid4().hex[:10]
    set_args = {
        "action": "download",
        "pins": pins,
        "query": "download",
    }
    yield _sse(
        "tool_start",
        {"name": "pinterest_set", "id": call_id, "arguments": set_args},
    )
    # Persist the pinterest_set call, then perform downloads.
    toolreg.execute(uid, "pinterest_set", set_args, ctx)
    ideas = [
        {"pinUrl": p["pinUrl"], "caption": p.get("caption") or p.get("title") or ""}
        for p in pins
    ]
    dl = _download_approved_ideas(
        uid, ideas, ctx, prepare_render=False, add_to_job=False
    )
    sources = [s for s in (dl.get("sources") or []) if s.get("id")]
    errors = dl.get("errors") or []
    slim = {
        "ok": bool(sources),
        "action": "download",
        "downloaded": len(sources),
        "count": len(pins),
        "pins": pins,
        "sourceIds": [s["id"] for s in sources],
        "errors": errors,
    }
    chat_store.append_message(
        uid,
        thread_id,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "name": "pinterest_set", "arguments": set_args}
            ],
        },
    )
    _persist_tool_result(
        uid, thread_id, name="pinterest_set", call_id=call_id, result=slim
    )
    yield _sse("tool_result", {"name": "pinterest_set", "id": call_id, "result": slim})

    if not sources:
        detail = errors[0].get("error") if errors else "No videos could be downloaded"
        yield _sse("error", {"message": str(detail)})
        yield _sse("token", {"text": str(detail)})
        chat_store.append_message(
            uid, thread_id, {"role": "assistant", "content": str(detail)}
        )
        yield _sse("done", {"threadId": thread_id})
        return

    got = len(sources)
    fail_bit = f" {len(errors)} failed." if errors else ""
    msg = (
        f"Downloaded {got} video{'s' if got != 1 else ''} to your library.{fail_bit} "
        "A zip of the files should start downloading."
    )
    chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
    yield _sse("token", {"text": msg})
    yield _sse(
        "saved_videos",
        {"sourceIds": [s["id"] for s in sources], "count": got, "errors": errors},
    )
    yield _sse("done", {"threadId": thread_id})


def _latest_user_attachments(uid: str, thread_id: str) -> list[dict]:
    thread = chat_store.get_thread(uid, thread_id) or {}
    for m in reversed(thread.get("messages") or []):
        if m.get("role") == "user":
            atts = m.get("attachments")
            return list(atts) if isinstance(atts, list) else []
    return []


def _slim_pins_for_prompt(pins: list[dict], *, limit: int = 40) -> list[dict]:
    out: list[dict] = []
    for p in toolreg.normalize_pins(pins)[:limit]:
        out.append(
            {
                "pinUrl": p.get("pinUrl") or "",
                "title": (p.get("title") or "")[:120],
                "thumbnail": p.get("thumbnail") or "",
                "previewUrl": p.get("previewUrl") or "",
                "previewKind": p.get("previewKind") or "",
            }
        )
    return out


def _pinterest_mode_prompt(
    user_text: str,
    *,
    bank: list[dict],
    shown: list[dict],
    search_ready: bool,
) -> str:
    bank_slim = _slim_pins_for_prompt(bank)
    shown_slim = _slim_pins_for_prompt(shown, limit=24)
    bank_json = json.dumps(bank_slim, ensure_ascii=False)
    shown_json = json.dumps(shown_slim, ensure_ascii=False)
    search_note = (
        "search_pinterest is available."
        if search_ready
        else "search_pinterest is NOT configured — you can only show/download from the bank."
    )
    return (
        f"{(user_text or '').strip()}\n\n"
        "=== /pinterest mode ===\n"
        "The slash only means: handle this as a Pinterest request. "
        "YOU read the user's message and choose tools. Do not treat the whole "
        "message as a search keyword.\n\n"
        f"{search_note}\n\n"
        "Decide intent:\n"
        "• Example TikTok ad/post URL + “find N images like this” / “M captions” → "
        "analyze_example_ad(url), invent short visual queries from the cover+desc, "
        "search_pinterest (mediaType=image unless they want video), pinterest_set show "
        "with N pins, then write M finished captions via set_render_plan / "
        "set_carousel_plan / propose_ideas.\n"
        "• New videos / images / a topic / “search for X” → call search_pinterest with a "
        "SHORT keyword query (2–6 words of visual topic, e.g. \"quiet luxury flex\"). "
        "Use mediaType=\"image\" when they want stills for carousels. "
        "Never paste instructions, “chat history”, “same angles”, or “new system\" "
        "into the query. Then call pinterest_set action=\"show\" with the pins to "
        "put in the grid (copy pinUrl/title/thumbnail/preview* from search).\n"
        "• Reopen / reuse / same videos / past pins / chat history / already found → "
        "call pinterest_set action=\"show\" using pins from the bank JSON below "
        "(mode=\"replace\"). Do NOT call search_pinterest.\n"
        "• Download / save → pinterest_set action=\"download\" (listed pins, or empty "
        "pins to use attachments → shown → bank).\n"
        "• Captioned approve/deny cards only → propose_ideas after pins are shown.\n\n"
        f"Thread pin bank ({len(bank)} pin(s)) — use these for reopen/history:\n"
        f"{bank_json}\n\n"
        f"Last shown set ({len(shown)} pin(s)):\n"
        f"{shown_json}\n"
    )


def _run_pinterest(
    uid: str,
    thread_id: str,
    user_text: str,
    query: str,
    *,
    model_id: str | None,
    ctx: toolreg.ToolContext,
    attachments: list[dict] | None = None,
) -> Iterator[str]:
    """Enter Pinterest mode: model reads the user message and chooses tools."""
    from ..pinterest import configured

    try:
        model = chat_models.resolve_model(model_id)
    except ValueError as exc:
        yield _sse("error", {"message": str(exc)})
        return

    chat_store.update_thread(
        uid, thread_id, model=model["id"], provider=model["provider"]
    )
    bank = chat_store.get_pinterest_pins(uid, thread_id, which="bank")
    shown = chat_store.get_pinterest_pins(uid, thread_id, which="shown")
    # Prefer exact "shown" list for the prompt (not bank fallback).
    thread = chat_store.get_thread(uid, thread_id) or {}
    shown_exact = list(thread.get("pinterestShown") or [])
    search_ready = bool(configured())

    prompt = _pinterest_mode_prompt(
        user_text,
        bank=bank,
        shown=shown_exact or shown,
        search_ready=search_ready,
    )
    chat_store.append_message(
        uid,
        thread_id,
        _user_message(
            user_text,
            attachments=attachments,
            pinterestPrompt=prompt,
        ),
    )
    yield _sse(
        "meta",
        {
            "command": "pinterest",
            "action": "agent",
            "model": model["id"],
            "provider": model["provider"],
            "bankCount": len(bank),
        },
    )
    if not search_ready and not bank:
        msg = (
            "Pinterest search is not configured (set SCRAPE_CREATORS_API_KEY), "
            "and this chat has no saved pins yet."
        )
        yield _sse("token", {"text": msg})
        chat_store.append_message(
            uid, thread_id, {"role": "assistant", "content": msg}
        )
        yield _sse("done", {"threadId": thread_id})
        return

    yield _sse("status", {"message": "Reading your Pinterest request…"})
    yield from _agent_rounds(uid, thread_id, ctx, model=model)


def _user_wants_pin_picks(uid: str, thread_id: str) -> bool:
    thread = chat_store.get_thread(uid, thread_id)
    for m in reversed(thread.get("messages") or []):
        if m.get("role") != "user":
            continue
        blob = " ".join(
            str(x)
            for x in (m.get("content") or "", m.get("pinterestPrompt") or "")
            if x
        )
        return wants_pin_picks(blob)
    return False


def _user_message(
    content: str,
    *,
    attachments: list[dict] | None = None,
    attachment_note: str = "",
    **extra,
) -> dict:
    msg = {"role": "user", "content": content, **extra}
    if attachments:
        msg["attachments"] = attachments
    note = (attachment_note or "").strip()
    if note:
        msg["attachmentNote"] = note
    return msg


def _run_help(uid: str, thread_id: str, user_text: str) -> Iterator[str]:
    chat_store.append_message(uid, thread_id, {"role": "user", "content": user_text})
    yield _sse("meta", {"command": "help"})
    yield _sse("token", {"text": HELP_TEXT})
    chat_store.append_message(
        uid, thread_id, {"role": "assistant", "content": HELP_TEXT}
    )
    yield _sse("done", {"threadId": thread_id})


def _plan_model_label(uid: str, thread_id: str) -> dict[str, str]:
    """Infer how captions were produced for the plan spreadsheet."""
    thread = chat_store.get_thread(uid, thread_id) or {}
    messages = thread.get("messages") or []
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        if m.get("source") in ("compare", "multiple") or m.get("bots"):
            bots = m.get("bots") or []
            labels = []
            for b in bots:
                if isinstance(b, dict):
                    labels.append(
                        str(b.get("label") or b.get("model") or b.get("provider") or "")
                    )
            labels = [x for x in labels if x]
            origin = "/multiple" if m.get("source") == "multiple" else "/all"
            default = "Selected models" if origin == "/multiple" else "All models"
            return {
                "origin": origin,
                "modelLabel": " / ".join(labels) if labels else default,
            }
        if m.get("source") == "perplexity":
            return {"origin": "research", "modelLabel": "Perplexity"}
        break
    model_id = str(thread.get("model") or "").strip()
    provider = str(thread.get("provider") or "").strip()
    label = model_id or provider or "Chat"
    if model_id:
        try:
            resolved = chat_models.resolve_model(model_id)
            label = str(resolved.get("label") or resolved.get("id") or label)
        except Exception:
            pass
    return {"origin": "chat", "modelLabel": label}


def _run_plan(
    uid: str,
    thread_id: str,
    user_text: str,
    ctx: toolreg.ToolContext,
    *,
    intent: str,
) -> Iterator[str]:
    chat_store.append_message(uid, thread_id, {"role": "user", "content": user_text})
    yield _sse("meta", {"command": intent})
    result = toolreg.execute(uid, "preview_plan", {}, ctx)
    if not result.get("ok"):
        msg = result.get("error") or "Could not build a render plan."
        yield _sse("token", {"text": msg})
        chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
        yield _sse("done", {"threadId": thread_id})
        return

    meta = _plan_model_label(uid, thread_id)
    count = int(result.get("count") or 0)
    mode = result.get("mode") or "matrix"
    videos = int(result.get("videoCount") or 0)
    caps = int(result.get("captionCount") or 0)
    if count < 1:
        msg = (
            "Nothing to preview yet. Add videos and captions first "
            "(or ask me to set captions for the clips in this chat)."
        )
        yield _sse("token", {"text": msg})
        chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
        yield _sse("done", {"threadId": thread_id})
        return

    origin = meta.get("origin") or "chat"
    model_label = meta.get("modelLabel") or ""
    pair_origins = {
        str(p.get("origin") or "")
        for p in (result.get("pairs") or [])
        if isinstance(p, dict)
    }
    if "/multiple" in pair_origins:
        origin = "/multiple"
        if not model_label or model_label in ("Chat", "All models"):
            model_label = "Selected models"
    elif "/all" in pair_origins:
        origin = "/all"
        if not model_label or model_label == "Chat":
            model_label = "All models"

    payload = {
        **result,
        "intent": intent,
        "origin": origin,
        "modelLabel": model_label,
        "openPreviewPopup": True,
    }
    yield _sse("open_plan", payload)

    bits = [f"{count} row(s)"]
    if videos and caps:
        pair_kinds = {
            str(p.get("kind") or "")
            for p in (result.get("pairs") or [])
            if isinstance(p, dict)
        }
        if pair_kinds == {"carousel"}:
            bits.append(f"{videos} carousel(s) · {caps} caption(s)")
        else:
            bits.append(f"{videos} video(s) × {caps} caption(s)")
    bits.append(f"mode {mode}")
    if origin in ("/all", "/multiple"):
        bits.append(origin + (f" · {model_label}" if model_label else ""))
    elif model_label:
        bits.append(model_label)
    verb = "Render plan" if intent == "render" else "Preview plan"
    msg = (
        f"{verb}: {', '.join(bits)}. "
        "Opening the spreadsheet — pick rows, then Render or Upload."
    )
    yield _sse("token", {"text": msg})
    chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
    yield _sse("done", {"threadId": thread_id})


def _run_research(
    uid: str, thread_id: str, user_text: str, query: str
) -> Iterator[str]:
    chat_store.append_message(uid, thread_id, {"role": "user", "content": user_text})
    yield _sse("meta", {"command": "research", "provider": "perplexity"})
    if not query:
        msg = (
            "Usage: /research <what you want to look up>\n"
            "Example: /research viral TikTok hooks for standing desks 2026"
        )
        yield _sse("token", {"text": msg})
        chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
        yield _sse("done", {"threadId": thread_id})
        return

    acc = ""
    final = ""
    citations: list[str] = []
    try:
        for ev in perplexity.run_deep_research(query):
            kind = ev.get("type")
            if kind == "status":
                yield _sse("status", {"message": ev.get("message") or "Researching…"})
            elif kind == "token":
                chunk = ev.get("text") or ""
                if chunk:
                    acc += chunk
                    yield _sse("token", {"text": chunk})
            elif kind == "error":
                yield _sse("error", {"message": ev.get("message") or "Research failed"})
                saved = acc or (ev.get("message") or "Research failed")
                chat_store.append_message(
                    uid,
                    thread_id,
                    {
                        "role": "assistant",
                        "content": saved,
                        "source": "perplexity",
                    },
                )
                yield _sse("done", {"threadId": thread_id})
                return
            elif kind == "done":
                final = (ev.get("text") or acc).strip()
                citations = ev.get("citations") or []
                try:
                    usage_mod.record_research(
                        uid, thread_id=thread_id, usage=ev.get("usage")
                    )
                except Exception:
                    _log.debug("research usage record failed", exc_info=True)
    except Exception as exc:
        _log.exception("Deep research failed")
        yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        if acc:
            chat_store.append_message(
                uid,
                thread_id,
                {"role": "assistant", "content": acc, "source": "perplexity"},
            )
        yield _sse("done", {"threadId": thread_id})
        return

    content = (final or acc).strip()
    if content:
        if not acc:
            yield _sse("token", {"text": content})
        chat_store.append_message(
            uid,
            thread_id,
            {
                "role": "assistant",
                "content": content,
                "source": "perplexity",
                "citations": citations,
            },
        )
    yield _sse("done", {"threadId": thread_id})


def _format_compare_content(bots: list[dict]) -> str:
    parts = []
    for bot in bots:
        label = bot.get("label") or bot.get("provider") or "Model"
        body = (bot.get("content") or bot.get("error") or "").strip()
        parts.append(f"### {label}\n{body}")
    return "\n\n".join(parts)


def _call_compare_bot(
    uid: str,
    thread_id: str,
    model: dict,
    messages: list[dict],
) -> dict:
    try:
        # Prefer images already on the last user message; also pass as kwarg so
        # providers that only look at vision_images still get frames.
        vision = None
        for m in reversed(messages or []):
            if m.get("role") == "user" and m.get("_vision"):
                vision = list(m.get("_vision") or [])
                break
        result = providers.chat_completion(
            provider=model["provider"],
            model=model["id"],
            messages=messages,
            allow_tools=False,
            vision_images=vision,
            system=providers.COMPARE_SYSTEM,
            max_tokens=16384,
        )
        _record_completion(
            uid, thread_id, model["provider"], model["id"], result, kind="compare"
        )
        content = (result.get("content") or "").strip()
        # Models sometimes emit tool_calls anyway; never surface that as the answer.
        if not content:
            calls = result.get("tool_calls") or []
            names = [
                str(c.get("name") or "").strip()
                for c in calls
                if isinstance(c, dict) and c.get("name")
            ]
            if names:
                raise ValueError(
                    "This model tried to use tools ("
                    + ", ".join(names[:6])
                    + "). /all and /multiple are text-only — retry, or ask in "
                    "normal chat (not /all) so one model can save a plan."
                )
            raise ValueError("Empty reply from model")
        # Strip roleplay like "(called list_sources)" if it somehow got into text.
        if re.match(r"^\(called\s+[\w,\s]+\)\s*$", content, re.I):
            raise ValueError(
                "Model returned a tool stub instead of captions. Retry, or use "
                "normal chat (not /all) when you need a plan saved."
            )
        return {
            "provider": model["provider"],
            "model": model["id"],
            "label": model.get("label") or model["id"],
            "content": content,
            "error": None,
        }
    except Exception as exc:
        _log.exception("Compare call failed for %s", model.get("id"))
        return {
            "provider": model["provider"],
            "model": model["id"],
            "label": model.get("label") or model["id"],
            "content": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _messages_for_compare(
    thread: dict,
    model: dict,
    prompt: str,
    *,
    attachment_note: str = "",
    vision_images: list[dict] | None = None,
) -> list[dict]:
    history = chat_store.messages_for_model(
        thread, model_id=model["id"], provider=model["provider"]
    )
    # Drop the just-appended slash line so models see the prompt, not the slash.
    if history and history[-1].get("role") == "user":
        history = history[:-1]
    # Compare is text-only — strip tool machinery so APIs don't hang on broken
    # tool_use chains and models don't try to "continue" tool calls.
    slim: list[dict] = []
    for m in history:
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant" and m.get("tool_calls"):
            text = (m.get("content") or "").strip()
            slim.append(
                {
                    "role": "assistant",
                    "content": text or "(Prior tool turn omitted for /all.)",
                }
            )
            continue
        if role in ("user", "assistant", "system"):
            slim.append({"role": role, "content": m.get("content") or ""})
    user_content = prompt
    note = (attachment_note or "").strip()
    if note:
        user_content = f"{prompt}\n\n{note}".strip() if prompt else note
    user_msg: dict = {"role": "user", "content": user_content}
    if vision_images:
        # Keep enough frames for multi-video /all (cap avoids runaway payloads).
        user_msg["_vision"] = list(vision_images)[:8]
    return slim + [user_msg]


COMPARE_BOT_TIMEOUT_S = 300


def _compare_prompt_hint(prompt: str) -> str:
    """Nudge models away from tool roleplay / angle brainstorms."""
    p = (prompt or "").strip()
    if not p:
        return p
    notes: list[str] = []
    if re.search(
        r"\b(new\s+plan|add(?:\s+them)?\s+to\s+(?:a\s+)?plan|set_render_plan|"
        r"save\s+(?:to\s+)?(?:the\s+)?plan|preview_plan)\b",
        p,
        re.I,
    ):
        notes.append(
            "Do not mention /preview, /render, tools, or how to save a plan. "
            "Only write the caption lists — the app saves them into a plan for you."
        )
    if re.search(
        r"\b(captions?|hooks?|on[- ]?screen|write|generate|create)\b",
        p,
        re.I,
    ):
        notes.append(
            "Write finished on-screen caption lines only — not angles, themes, "
            "concepts, or labels like 'Social proof angle'. Example of a real "
            "caption: 'This side hustle paid my rent'. "
            "If multiple videos/frames are attached, put exactly the requested "
            "count under each ### Frame N heading (with sourceId)."
        )
    if not notes:
        return p
    return p + "\n\n[System note: " + " ".join(notes) + "]"


def _run_compare_models(
    uid: str,
    thread_id: str,
    user_text: str,
    prompt: str,
    models: list[dict],
    *,
    command: str,
    source: str,
    empty_usage: str,
    vision_images: list[dict] | None = None,
    attachments: list[dict] | None = None,
    attachment_note: str = "",
    ctx: toolreg.ToolContext | None = None,
) -> Iterator[str]:
    """Parallel text replies from an explicit model list (tools off)."""
    chat_store.append_message(
        uid,
        thread_id,
        _user_message(
            user_text,
            attachments=attachments,
            attachment_note=attachment_note or "",
        ),
    )
    prefer = (models[0]["id"] if models else "") or ""
    yield _sse("meta", {"command": command, "model": prefer})

    if not models:
        yield _sse("token", {"text": empty_usage})
        chat_store.append_message(
            uid, thread_id, {"role": "assistant", "content": empty_usage}
        )
        yield _sse("done", {"threadId": thread_id})
        return

    if not prompt:
        yield _sse("token", {"text": empty_usage})
        chat_store.append_message(
            uid, thread_id, {"role": "assistant", "content": empty_usage}
        )
        yield _sse("done", {"threadId": thread_id})
        return

    thread = chat_store.get_thread(uid, thread_id)
    prompt = _compare_prompt_hint(prompt)
    n_vision = len(vision_images or [])
    if n_vision:
        prompt = (
            prompt
            + f"\n\n[You are receiving {n_vision} first-frame image(s), in Frame order "
            "from the attachment note. Write finished captions grouped under each Frame.]"
        )

    bots_meta = [
        {
            "provider": m["provider"],
            "model": m["id"],
            "label": m.get("label") or m["id"],
        }
        for m in models
    ]
    yield _sse("compare_start", {"bots": bots_meta, "command": command})
    yield _sse(
        "status",
        {
            "message": f"Asking {len(models)} model(s)…"
            + (f" ({n_vision} frame(s))" if n_vision else ""),
        },
    )

    results: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=max(len(models), 1))
    try:
        futs = {
            pool.submit(
                _call_compare_bot,
                uid,
                thread_id,
                m,
                _messages_for_compare(
                    thread,
                    m,
                    prompt,
                    attachment_note=attachment_note or "",
                    vision_images=vision_images,
                ),
            ): m
            for m in models
        }
        pending = set(futs.keys())
        started = time.monotonic()
        while pending:
            elapsed = time.monotonic() - started
            if elapsed >= COMPARE_BOT_TIMEOUT_S:
                for fut in list(pending):
                    model = futs[fut]
                    mid = model["id"]
                    if mid not in results:
                        bot = {
                            "provider": model["provider"],
                            "model": mid,
                            "label": model.get("label") or mid,
                            "content": "",
                            "error": f"Timed out after {COMPARE_BOT_TIMEOUT_S}s",
                        }
                        results[mid] = bot
                        payload = {
                            "provider": bot["provider"],
                            "model": bot["model"],
                            "label": bot["label"],
                        }
                        yield _sse(
                            "compare_error",
                            {**payload, "message": bot["error"]},
                        )
                        yield _sse("compare_bot_done", payload)
                    fut.cancel()
                pending.clear()
                break
            done_set, pending = wait(
                pending,
                timeout=1.0,
                return_when=FIRST_COMPLETED,
            )
            if not done_set:
                yield _sse(
                    "status",
                    {"message": f"Still waiting on {len(pending)} model(s)…"},
                )
                continue
            for fut in done_set:
                model = futs[fut]
                try:
                    bot = fut.result()
                except Exception as exc:
                    bot = {
                        "provider": model["provider"],
                        "model": model["id"],
                        "label": model.get("label") or model["id"],
                        "content": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                key = bot.get("model") or bot.get("provider") or ""
                results[key] = bot
                payload = {
                    "provider": bot["provider"],
                    "model": bot.get("model") or "",
                    "label": bot.get("label") or "",
                }
                if bot.get("error"):
                    yield _sse(
                        "compare_error",
                        {**payload, "message": bot["error"]},
                    )
                else:
                    yield _sse(
                        "compare_token",
                        {**payload, "text": bot.get("content") or ""},
                    )
                yield _sse("compare_bot_done", payload)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    ordered = []
    for m in models:
        mid = m["id"]
        if mid in results:
            ordered.append(results[mid])
        elif m["provider"] in results and results[m["provider"]].get("model") == mid:
            ordered.append(results[m["provider"]])
    content = _format_compare_content(ordered)
    bots_out = [
        {
            "provider": b["provider"],
            "model": b["model"],
            "label": b["label"],
            "content": b.get("content") or "",
            "error": b.get("error"),
        }
        for b in ordered
    ]
    chat_store.append_message(
        uid,
        thread_id,
        {
            "role": "assistant",
            "content": content,
            "source": source,
            "bots": bots_out,
        },
    )

    origin = "/multiple" if command == "multiple" else "/all"
    plan_opened = False
    if ctx is not None and cap_pick.looks_like_caption_prompt(prompt):
        videos_meta = cap_pick._videos_from_attachments(attachments)
        if videos_meta:
            yield _sse("status", {"message": "Building render plan from captions…"})
            try:
                videos_out, details = cap_pick.merge_bot_plans(
                    bots_out, videos_meta, origin=origin
                )
                if videos_out:
                    want_new = True
                    labels = [
                        str(b.get("label") or b.get("model") or "")
                        for b in bots_out
                        if not b.get("error")
                    ]
                    labels = [x for x in labels if x]
                    result = toolreg.apply_render_plan(
                        uid,
                        ctx,
                        {
                            "videos": videos_out,
                            "origin": origin,
                            "captionDetails": details,
                            "newPlan": want_new,
                            "title": (
                                f"{origin} · {len(videos_out)} video(s)"
                            ),
                            "showUi": True,
                        },
                    )
                    if result.get("ok") and result.get("pairs"):
                        payload = {
                            **result,
                            "intent": "preview",
                            "origin": origin,
                            "modelLabel": " / ".join(labels) if labels else origin,
                            "openPreviewPopup": True,
                        }
                        yield _sse("open_plan", payload)
                        n = int(result.get("count") or 0)
                        msg = (
                            f"Saved {n} caption(s) across {len(videos_out)} video(s) "
                            f"from {origin} into a new plan. Opening the spreadsheet."
                        )
                        yield _sse("token", {"text": msg})
                        chat_store.append_message(
                            uid,
                            thread_id,
                            {"role": "assistant", "content": msg},
                        )
                        plan_opened = True
            except Exception as exc:
                _log.exception("Auto plan from compare failed")
                yield _sse(
                    "status",
                    {"message": f"Could not auto-build plan: {exc}"},
                )

    if not plan_opened:
        # No attached library videos — do not open the pick popup; user asked for plan only.
        pick_items = cap_pick.collect_caption_items(bots_out, prompt)
        if pick_items and not attachments:
            yield _sse(
                "token",
                {
                    "text": (
                        f"Got {len(pick_items)} caption line(s). Attach library videos "
                        "and re-run, or ask in normal chat to save a plan with sourceIds."
                    )
                },
            )
    yield _sse("done", {"threadId": thread_id})


def _run_all(
    uid: str,
    thread_id: str,
    user_text: str,
    prompt: str,
    *,
    model_id: str | None = None,
    provider_models: dict | None = None,
    vision_images: list[dict] | None = None,
    attachments: list[dict] | None = None,
    attachment_note: str = "",
    ctx: toolreg.ToolContext | None = None,
) -> Iterator[str]:
    thread = chat_store.get_thread(uid, thread_id)
    prefer, by_provider = chat_models.prefs_for_compare(
        model_id=model_id,
        provider_models=provider_models,
        thread=thread,
        saved_models=_saved_chat_models(uid),
    )
    try:
        selected = chat_models.resolve_model(prefer)
        chat_store.update_thread(
            uid,
            thread_id,
            model=selected["id"],
            provider=selected["provider"],
            provider_models=by_provider,
        )
        prefer = selected["id"]
        by_provider[selected["provider"]] = selected["id"]
    except ValueError:
        if by_provider:
            chat_store.update_thread(
                uid, thread_id, provider_models=by_provider
            )

    models = chat_models.compare_models(
        prefer_id=prefer, prefer_by_provider=by_provider
    )
    empty = (
        "No chat models are configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
        "XAI_API_KEY, and/or GEMINI_API_KEY."
        if not models
        else (
            "Usage: /all <prompt>\n"
            "Each configured AI (Claude, GPT, Grok, Gemini) answers with this "
            "chat’s history included. Each provider uses the last model you "
            "picked for it in the dropdowns; others fall back to that "
            "provider’s default.\n"
            "Example: /all write 8 short standing-desk hooks\n"
            "Attach library videos so captions are saved straight into a Plan."
        )
    )
    yield from _run_compare_models(
        uid,
        thread_id,
        user_text,
        prompt,
        models,
        command="all",
        source="compare",
        empty_usage=empty,
        vision_images=vision_images,
        attachments=attachments,
        attachment_note=attachment_note,
        ctx=ctx,
    )


def _run_multiple(
    uid: str,
    thread_id: str,
    user_text: str,
    arg: str,
    *,
    model_id: str | None = None,
    provider_models: dict | None = None,
    models: list[str] | None = None,
    vision_images: list[dict] | None = None,
    attachments: list[dict] | None = None,
    attachment_note: str = "",
    ctx: toolreg.ToolContext | None = None,
) -> Iterator[str]:
    """Run a prompt on an explicit multi-model selection."""
    thread = chat_store.get_thread(uid, thread_id)
    prefer, by_provider = chat_models.prefs_for_compare(
        model_id=model_id,
        provider_models=provider_models,
        thread=thread,
        saved_models=_saved_chat_models(uid),
    )
    try:
        selected = chat_models.resolve_model(prefer)
        chat_store.update_thread(
            uid,
            thread_id,
            model=selected["id"],
            provider=selected["provider"],
            provider_models=by_provider,
        )
        by_provider[selected["provider"]] = selected["id"]
    except ValueError:
        if by_provider:
            chat_store.update_thread(uid, thread_id, provider_models=by_provider)

    inline_ids, prompt = chat_models.parse_model_list_prefix(arg)
    chosen_ids = [str(x).strip() for x in (models or []) if str(x).strip()]
    if not chosen_ids:
        chosen_ids = inline_ids
    resolved = chat_models.resolve_models(chosen_ids)

    # Remember picks per provider for follow-up chat / /all.
    if resolved:
        for m in resolved:
            by_provider[m["provider"]] = m["id"]
        chat_store.update_thread(
            uid,
            thread_id,
            model=resolved[0]["id"],
            provider=resolved[0]["provider"],
            provider_models=by_provider,
        )

    empty = (
        "Usage: /multiple <prompt>\n"
        "Pick two or more models in the picker (any providers), or pass ids:\n"
        "  /multiple claude-sonnet-5,gpt-5.4,grok-4.6 write 8 standing-desk hooks\n"
        "Each selected model answers in its own row. Caption lists open the "
        "picker so you can save mixed-model hooks into this chat’s plan."
        if not resolved or not prompt
        else (
            "No usable models from that selection. Check API keys and model ids "
            f"({', '.join(chosen_ids) or 'none'})."
            if chosen_ids and not resolved
            else (
                "No chat models are configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                "XAI_API_KEY, and/or GEMINI_API_KEY."
            )
        )
    )
    if chosen_ids and not resolved:
        empty = (
            "None of those models are available. Check ids and that the provider "
            f"API keys are set. Got: {', '.join(chosen_ids)}"
        )
    elif not chosen_ids:
        empty = (
            "Usage: /multiple <prompt>\n"
            "Pick two or more models in the picker (any providers), or pass ids:\n"
            "  /multiple claude-sonnet-5,gpt-5.4,grok-4.6 write 8 standing-desk hooks\n"
            "Each selected model answers in its own row. Caption lists open the "
            "picker so you can save mixed-model hooks into this chat’s plan."
        )
    elif not prompt:
        empty = (
            "Add a prompt after your model picks.\n"
            "Example: /multiple claude-sonnet-5,gpt-5.4 write 8 standing-desk hooks"
        )

    yield from _run_compare_models(
        uid,
        thread_id,
        user_text,
        prompt if resolved else "",
        resolved,
        command="multiple",
        source="multiple",
        empty_usage=empty,
        vision_images=vision_images,
        attachments=attachments,
        attachment_note=attachment_note,
        ctx=ctx,
    )


def _slim_idea(idea: dict) -> dict:
    return {
        "id": str(idea.get("id") or ""),
        "pinUrl": idea.get("pinUrl") or "",
        "caption": idea.get("caption") or "",
        "title": idea.get("title") or "",
    }


def _target_count(pending: dict) -> int:
    try:
        target = int(pending.get("targetCount") or 1)
    except (TypeError, ValueError):
        target = 1
    return max(1, min(50, target))


def _idea_is_denied(
    iid: str,
    *,
    approved_set: set[str],
    denied_set: set[str],
    saved: dict,
) -> bool:
    """Deny is opt-out. Unmarked ideas stay approved."""
    if iid and iid in denied_set:
        return True
    if iid and iid in approved_set:
        return False
    return str(saved.get(iid) or "") == "no"


def _sync_round_choices(
    round_ideas: list,
    existing: dict | None,
    *,
    approved_ids: list[str] | None = None,
    denied_ids: list[str] | None = None,
) -> dict[str, str]:
    """Apply a client snapshot onto this round. Missing ideas stay approved."""
    choices: dict[str, str] = dict(existing or {})
    round_ids = [str(i.get("id") or "") for i in round_ideas if i.get("id")]
    for iid in round_ids:
        if iid not in choices:
            choices[iid] = "yes"
    denied_set = {str(x) for x in (denied_ids or []) if str(x)}
    approved_set = {str(x) for x in (approved_ids or []) if str(x)}
    mentioned = (approved_set | denied_set) & set(round_ids)
    if not mentioned:
        return {iid: choices.get(iid) or "yes" for iid in round_ids}
    for iid in round_ids:
        if iid in denied_set:
            choices[iid] = "no"
        elif iid in approved_set:
            choices[iid] = "yes"
    return {iid: choices.get(iid) or "yes" for iid in round_ids}


def _selector_payload(
    pending: dict,
    *,
    selected: list[dict],
    denied_keys: list[str],
    user_text: str = "",
    interrupted: bool = False,
) -> dict:
    target = _target_count(pending)
    round_ideas = list(pending.get("roundIdeas") or [])
    need = max(0, target - len(selected))
    payload = {
        "ok": True,
        "selectorStillOpen": bool(round_ideas),
        "approved": len(selected),
        "target": target,
        "need": need,
        "deniedKeys": denied_keys,
        "approvedIdeas": [_slim_idea(i) for i in selected],
    }
    if interrupted:
        payload["interrupted"] = True
        payload["userFeedback"] = (user_text or "")[:1500]
        payload["message"] = (
            "User sent a chat message during idea review. "
            f"The pin selector is STILL on screen with {len(round_ideas)} idea(s). "
            f"{len(selected)} are currently approved (see approvedIdeas); "
            "only Deny drops a pin.\n"
            f"New instruction: {(user_text or '')[:1500]}\n"
            "Do NOT ask which pins to use in text and do not list pin numbers. "
            "They pick on the selector. If you need them to choose a different set, "
            "call propose_ideas with those exact ideas so the selector updates. "
            "If they asked to preview, render, or upload the current selection, "
            "use approvedIdeas now. If they asked only to download or save the "
            "selected videos, do not render or upload."
        )
    return payload


def _append_selector_note(uid: str, thread_id: str, payload: dict, *, submitted: bool) -> None:
    lead = (
        "I submitted the pin selector. Do not ask which pins in chat."
        if submitted
        else (
            "[Pin selector is still on screen. Do not ask which pins in chat. "
            "Current selection follows.]"
        )
    )
    chat_store.append_message(
        uid,
        thread_id,
        {
            "role": "user",
            "content": lead + "\n" + json.dumps(payload, ensure_ascii=False)[:6000],
        },
    )


def _emit_review_followup(
    uid: str,
    thread_id: str,
    *,
    call_id: str,
    payload: dict,
    gate_closed: bool,
) -> Iterator[str]:
    if gate_closed:
        _append_selector_note(uid, thread_id, payload, submitted=True)
    else:
        _persist_tool_result(
            uid, thread_id, name="propose_ideas", call_id=call_id, result=payload
        )
    yield _sse("tool_result", {"name": "propose_ideas", "id": call_id, "result": payload})


def _apply_round_choices(
    pending: dict,
    *,
    approved_ids: list[str] | None = None,
    denied_ids: list[str] | None = None,
    limit_to_target: bool = True,
) -> tuple[list[dict], list[str]]:
    """Fold the current round into kept approvals. Default is approve."""
    round_ideas = pending.get("roundIdeas") or []
    approved_set = {str(x) for x in (approved_ids or []) if str(x)}
    denied_set = {str(x) for x in (denied_ids or []) if str(x)}
    saved = pending.get("roundChoices") if isinstance(pending.get("roundChoices"), dict) else {}
    approved = list(pending.get("approved") or [])
    denied_keys = list(pending.get("deniedKeys") or [])
    target = _target_count(pending)
    for idea in round_ideas:
        iid = str(idea.get("id") or "")
        key = toolreg.idea_deny_key(idea.get("pinUrl") or "", idea.get("caption") or "")
        if _idea_is_denied(
            iid, approved_set=approved_set, denied_set=denied_set, saved=saved
        ):
            if key and key not in denied_keys:
                denied_keys.append(key)
            continue
        if limit_to_target and len(approved) >= target:
            continue
        approved.append(idea)
    return approved, denied_keys


def save_review_choices(
    uid: str,
    thread_id: str,
    *,
    approved_ids: list[str] | None = None,
    denied_ids: list[str] | None = None,
) -> dict:
    """Persist approve/deny toggles without closing the review or running the agent."""
    thread = chat_store.get_thread(uid, thread_id)
    pending = thread.get("pendingReview")
    if not isinstance(pending, dict) or not (pending.get("roundIdeas") or []):
        raise KeyError("No pending idea review")
    call_id = str(pending.get("toolCallId") or "")
    denied_set = {str(x) for x in (denied_ids or []) if str(x)}
    choices: dict[str, str] = {}
    for idea in pending.get("roundIdeas") or []:
        iid = str(idea.get("id") or "")
        if not iid:
            continue
        choices[iid] = "no" if iid in denied_set else "yes"
    latest_thread = chat_store.get_thread(uid, thread_id)
    latest = latest_thread.get("pendingReview")
    if not isinstance(latest, dict) or not (latest.get("roundIdeas") or []):
        return {"ok": True, "ignored": True}
    if call_id and str(latest.get("toolCallId") or "") != call_id:
        return {"ok": True, "ignored": True}
    latest_ids = {str(i.get("id") or "") for i in (latest.get("roundIdeas") or [])}
    choices = {iid: choice for iid, choice in choices.items() if iid in latest_ids}
    chat_store.set_pending_review(
        uid, thread_id, {**latest, "roundChoices": choices}
    )
    return {
        "ok": True,
        "roundChoices": choices,
        "approved": len([c for c in choices.values() if c == "yes"]),
        "denied": len([c for c in choices.values() if c == "no"]),
    }


def _close_open_gates(
    uid: str,
    thread_id: str,
    user_text: str,
    *,
    approved_ids: list[str] | None = None,
    denied_ids: list[str] | None = None,
) -> str:
    """Close paused tool_use rows so the user can chat mid-review.

    propose_ideas / confirm tools persist the assistant tool_use first and wait.
    A new chat line used to land right after that, which Anthropic rejects.
    The pin selector stays on screen; current approve/deny rides with the chat.
    Returns extra context to prepend to the user message on a later chat.
    """
    extra = ""

    def _apply(thread: dict) -> dict:
        nonlocal extra
        pending_review = thread.get("pendingReview")
        msgs = list(thread.get("messages") or [])
        if isinstance(pending_review, dict) and pending_review.get("toolCallId"):
            call_id = str(pending_review.get("toolCallId"))
            round_ideas = list(pending_review.get("roundIdeas") or [])
            already_closed = bool(pending_review.get("gateClosed"))
            choices = _sync_round_choices(
                round_ideas,
                pending_review.get("roundChoices")
                if isinstance(pending_review.get("roundChoices"), dict)
                else {},
                approved_ids=approved_ids,
                denied_ids=denied_ids,
            )
            snapshot_pending = {**pending_review, "roundChoices": choices}
            selected, denied_keys = _apply_round_choices(
                snapshot_pending,
                approved_ids=approved_ids,
                denied_ids=denied_ids,
            )
            payload = _selector_payload(
                snapshot_pending,
                selected=selected,
                denied_keys=denied_keys,
                user_text=user_text,
                interrupted=True,
            )
            if already_closed:
                extra = (
                    "[Pin selector is still on screen. Do not ask which pins in chat. "
                    "Current selection follows.]\n"
                    + json.dumps(
                        {
                            "selectorStillOpen": True,
                            "approved": payload["approved"],
                            "target": payload["target"],
                            "need": payload["need"],
                            "deniedKeys": denied_keys,
                            "approvedIdeas": payload["approvedIdeas"],
                        },
                        ensure_ascii=False,
                    )[:5000]
                )
            else:
                msgs = chat_store.place_tool_result(
                    msgs, "propose_ideas", call_id, payload
                )
            if round_ideas:
                thread["pendingReview"] = {
                    **pending_review,
                    "roundChoices": choices,
                    "gateClosed": True,
                }
            elif payload["need"] > 0:
                thread["pendingReview"] = {
                    **pending_review,
                    "approved": selected,
                    "deniedKeys": denied_keys,
                    "roundIdeas": [],
                    "roundChoices": {},
                    "gateClosed": True,
                }
            else:
                thread["pendingReview"] = None

        pending_confirm = thread.get("pendingConfirm")
        if isinstance(pending_confirm, dict) and pending_confirm.get("toolCallId"):
            call_id = str(pending_confirm.get("toolCallId"))
            name = str(pending_confirm.get("name") or "tool")
            msgs = chat_store.place_tool_result(
                msgs,
                name,
                call_id,
                {
                    "ok": False,
                    "declined": True,
                    "interrupted": True,
                    "userFeedback": (user_text or "")[:1500],
                    "error": "User sent a chat message instead of confirming this action.",
                },
            )
            thread["pendingConfirm"] = None

        thread["messages"] = chat_store.ensure_tool_results(msgs)
        return thread

    chat_store.mutate_thread(uid, thread_id, _apply)
    return extra


_RENDER_PROCEED_RE = re.compile(
    r"""
    \b(render(?:s|ing)?|upload(?:s|ing)?|burn(?:s|ing)?)\b
    .{0,48}
    \b(approved|selected|these|those|them|the\s+\d+|videos?|pins?|ideas?|now|please)\b
    |
    \b(approved|selected|these|those|them|the\s+\d+|videos?|pins?|ideas?)\b
    .{0,48}
    \b(render(?:s|ing)?|upload(?:s|ing)?|burn(?:s|ing)?)\b
    """,
    re.I | re.S | re.X,
)
_RENDER_PROCEED_NEG_RE = re.compile(
    r"""
    \b(don'?t|do\s+not|not\s+yet|later|wait|more\s+ideas|find\s+more|search\s+again|new\s+pins)\b
    """,
    re.I | re.X,
)
_SAVE_PROCEED_RE = re.compile(
    r"""
    \bjust\s+(download|save)\b
    |
    \bsave\s+to\s+(the\s+)?library\b
    |
    \b(download|save)\b
    .{0,48}
    \b(approved|selected|these|those|them|the\s+\d+|videos?|pins?|ideas?|now|please|library)\b
    |
    \b(approved|selected|these|those|them|the\s+\d+|videos?|pins?|ideas?)\b
    .{0,48}
    \b(download|save)\b
    """,
    re.I | re.S | re.X,
)


def _wants_render_selection(text: str) -> bool:
    t = (text or "").strip()
    if not t or _RENDER_PROCEED_NEG_RE.search(t):
        return False
    return bool(_RENDER_PROCEED_RE.search(t))


def _wants_save_selection(text: str) -> bool:
    t = (text or "").strip()
    if not t or _RENDER_PROCEED_NEG_RE.search(t):
        return False
    if _wants_render_selection(t):
        return False
    return bool(_SAVE_PROCEED_RE.search(t))


def _pending_review_has_selection(pending: dict | None) -> bool:
    if not isinstance(pending, dict):
        return False
    if pending.get("roundIdeas") or pending.get("approved"):
        return True
    choices = pending.get("roundChoices")
    return isinstance(choices, dict) and any(v == "yes" for v in choices.values())


def run_turn(
    uid: str,
    thread_id: str,
    user_text: str,
    *,
    model_id: str | None,
    ctx: toolreg.ToolContext,
    approved_ids: list[str] | None = None,
    denied_ids: list[str] | None = None,
    provider_models: dict | None = None,
    models: list[str] | None = None,
    vision_images: list[dict] | None = None,
    attachment_note: str = "",
    attachments: list[dict] | None = None,
) -> Iterator[str]:
    """Yield SSE chunks for one user message turn."""
    _remember_model_prefs(
        uid, thread_id, model_id=model_id, provider_models=provider_models
    )
    thread = chat_store.get_thread(uid, thread_id)
    pending_review = thread.get("pendingReview") if isinstance(thread, dict) else None
    if _pending_review_has_selection(pending_review) and _wants_render_selection(user_text):
        try:
            model = chat_models.resolve_model(model_id)
        except ValueError as exc:
            yield _sse("error", {"message": str(exc)})
            return
        chat_store.update_thread(
            uid, thread_id, model=model["id"], provider=model["provider"]
        )
        yield _sse("meta", {"model": model["id"], "provider": model["provider"]})
        yield from review_and_continue(
            uid,
            thread_id,
            approved_ids=list(approved_ids or []),
            denied_ids=list(denied_ids or []),
            ctx=ctx,
            proceed=True,
            user_text=user_text,
        )
        return
    if _pending_review_has_selection(pending_review) and _wants_save_selection(user_text):
        try:
            model = chat_models.resolve_model(model_id)
        except ValueError as exc:
            yield _sse("error", {"message": str(exc)})
            return
        chat_store.update_thread(
            uid, thread_id, model=model["id"], provider=model["provider"]
        )
        yield _sse("meta", {"model": model["id"], "provider": model["provider"]})
        yield from review_and_continue(
            uid,
            thread_id,
            approved_ids=list(approved_ids or []),
            denied_ids=list(denied_ids or []),
            ctx=ctx,
            save_only=True,
            user_text=user_text,
        )
        return

    gate_note = _close_open_gates(
        uid,
        thread_id,
        user_text,
        approved_ids=approved_ids,
        denied_ids=denied_ids,
    )
    slash = parse_slash(user_text)
    if slash:
        cmd, arg = slash
        if cmd == "help":
            yield from _run_help(uid, thread_id, user_text)
            return
        if cmd == "research":
            yield from _run_research(uid, thread_id, user_text, arg)
            return
        if cmd == "pinterest":
            yield from _run_pinterest(
                uid,
                thread_id,
                user_text,
                arg,
                model_id=model_id,
                ctx=ctx,
                attachments=attachments,
            )
            return
        if cmd == "all":
            yield from _run_all(
                uid,
                thread_id,
                user_text,
                arg,
                model_id=model_id,
                provider_models=provider_models,
                vision_images=vision_images,
                attachments=attachments,
                attachment_note=attachment_note or "",
                ctx=ctx,
            )
            return
        if cmd == "multiple":
            yield from _run_multiple(
                uid,
                thread_id,
                user_text,
                arg,
                model_id=model_id,
                provider_models=provider_models,
                models=models,
                vision_images=vision_images,
                attachments=attachments,
                attachment_note=attachment_note or "",
                ctx=ctx,
            )
            return
        if cmd == "preview":
            yield from _run_plan(uid, thread_id, user_text, ctx, intent="preview")
            return
        if cmd == "render":
            yield from _run_plan(uid, thread_id, user_text, ctx, intent="render")
            return

    # Fast path: plain-language download of pins already in this chat.
    if wants_pin_download(user_text):
        if _pins_to_download(uid, thread_id, attachments=attachments):
            yield from _run_pinterest_download(
                uid, thread_id, user_text, ctx=ctx, attachments=attachments
            )
            return

    try:
        model = chat_models.resolve_model(model_id)
    except ValueError as exc:
        yield _sse("error", {"message": str(exc)})
        return

    chat_store.update_thread(
        uid, thread_id, model=model["id"], provider=model["provider"]
    )
    content = user_text
    if gate_note:
        content = gate_note + "\n\n" + user_text
    try:
        from ..tiktok_analyze import is_tiktok_url

        urls = re.findall(r"https?://[^\s<>\"']+", user_text or "")
        tiktok_urls = [u.rstrip(").,]") for u in urls if is_tiktok_url(u)]
        if tiktok_urls:
            content += (
                "\n\n[Example-ad hint: this message includes a TikTok URL. "
                "If they want similar Pinterest images and/or caption variations, "
                f"call analyze_example_ad on {tiktok_urls[0]} first, then search + plan.]"
            )
    except Exception:
        pass
    chat_store.append_message(
        uid,
        thread_id,
        _user_message(
            content,
            attachments=attachments,
            attachment_note=attachment_note or "",
        ),
    )

    yield _sse("meta", {"model": model["id"], "provider": model["provider"]})
    yield from _agent_rounds(
        uid,
        thread_id,
        ctx,
        model=model,
        vision_images=vision_images,
    )


def _persist_tool_result(
    uid: str,
    thread_id: str,
    *,
    name: str,
    call_id: str,
    result: dict,
) -> dict:
    slim = dict(result) if isinstance(result, dict) else {"ok": False, "error": "bad result"}
    slim.pop("_visionUrls", None)
    slim.pop("_visionPaths", None)
    chat_store.insert_tool_result(
        uid,
        thread_id,
        name=name,
        call_id=call_id,
        result=slim,
        flush_index_now=False,
    )
    return slim


def _jpeg_thumb_b64(blob: bytes, max_edge: int = 400) -> dict | None:
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(blob))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_edge, max_edge))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return {
        "media_type": "image/jpeg",
        "data": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


def _collect_vision(result: dict) -> list[dict]:
    if not isinstance(result, dict):
        return []
    images: list[dict] = []
    urls = [u for u in (result.get("_visionUrls") or []) if u]
    cap = 16 if result.get("pins") else 8
    if urls:
        try:
            from ..pinterest import fetch_thumbnail_jpegs

            images.extend(fetch_thumbnail_jpegs(urls, max_n=cap))
        except Exception:
            _log.debug("Vision thumbnail fetch failed", exc_info=True)
    for raw_path in (result.get("_visionPaths") or [])[:8]:
        if len(images) >= cap:
            break
        try:
            blob = Path(raw_path).read_bytes()
            if not blob:
                continue
            thumb = _jpeg_thumb_b64(blob)
            if thumb:
                images.append(thumb)
        except Exception:
            continue
    return images[:cap]


def _norm_pin_url(url: str) -> str:
    s = (url or "").strip().split("?")[0].rstrip("/").lower()
    for prefix in ("https://www.", "http://www.", "https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return s


def _hydrate_ideas_from_pins(ideas: list[dict], pins: list | None) -> list[dict]:
    by_url: dict[str, dict] = {}
    for pin in pins or []:
        if not isinstance(pin, dict):
            continue
        key = _norm_pin_url(pin.get("url") or pin.get("pinUrl") or "")
        if key:
            by_url[key] = pin
    out: list[dict] = []
    for idea in ideas:
        pin = by_url.get(_norm_pin_url(idea.get("pinUrl") or "")) or {}
        merged = dict(idea)
        if pin:
            merged["thumbnail"] = (
                idea.get("thumbnail") or pin.get("thumbnail") or ""
            ).strip()
            merged["title"] = (idea.get("title") or pin.get("title") or "").strip()
            merged["previewUrl"] = (
                pin.get("previewUrl") or idea.get("previewUrl") or ""
            ).strip()
            merged["previewKind"] = (
                pin.get("previewKind") or idea.get("previewKind") or ""
            ).strip()
            if pin.get("duration") and not merged.get("duration"):
                merged["duration"] = pin.get("duration")
            if pin.get("width") and not merged.get("width"):
                merged["width"] = pin.get("width")
            if pin.get("height") and not merged.get("height"):
                merged["height"] = pin.get("height")
        out.append(merged)
    return out


def _pause_review(
    uid: str,
    thread_id: str,
    *,
    call_id: str,
    args: dict,
    ctx: toolreg.ToolContext | None = None,
) -> tuple[dict | None, dict | None]:
    """Return (pending, skip_payload). skip_payload means do not pause the UI."""
    ideas = toolreg.normalize_ideas(args.get("ideas") or [])
    pins = list(getattr(ctx, "last_pinterest_pins", None) or [])
    if pins:
        ideas = _hydrate_ideas_from_pins(ideas, pins)
    try:
        target = int(args.get("targetCount") or len(ideas) or 1)
    except (TypeError, ValueError):
        target = max(len(ideas), 1)
    target = max(1, min(50, target))
    thread = chat_store.get_thread(uid, thread_id)
    prev = thread.get("pendingReview") if isinstance(thread.get("pendingReview"), dict) else {}
    approved = list(prev.get("approved") or [])
    denied = list(prev.get("deniedKeys") or [])
    denied_set = set(denied)
    ideas = [
        i
        for i in ideas
        if toolreg.idea_deny_key(i.get("pinUrl") or "", i.get("caption") or "") not in denied_set
    ]
    if prev:
        target = int(prev.get("targetCount") or target)
    revision = prev.get("revision") if prev else None
    if isinstance(revision, dict) and revision.get("ideaIds"):
        return _merge_review_revision(
            uid,
            thread_id,
            call_id=call_id,
            incoming=ideas,
            prev=prev,
            revision=revision,
        )
    need = max(0, target - len(approved))
    if need > 0:
        ideas = ideas[:need]
    if not ideas:
        return None, {
            "ok": False,
            "error": (
                "No new ideas to review (duplicates or previously denied). "
                "Call search_pinterest with excludeUrls, then propose_ideas "
                f"with only {max(need, 1)} new idea(s)."
            ),
            "approved": len(approved),
            "target": target,
            "need": need,
            "deniedKeys": denied,
        }
    rounds = int(prev.get("rounds") or 0) + 1
    pending = {
        "toolCallId": call_id,
        "name": "propose_ideas",
        "targetCount": target,
        "approved": approved,
        "deniedKeys": denied,
        "roundIdeas": ideas,
        "roundChoices": {str(i.get("id")): "yes" for i in ideas if i.get("id")},
        "rounds": rounds,
        "gateClosed": False,
    }
    chat_store.set_pending_review(uid, thread_id, pending)
    return pending, None


def _merge_review_revision(
    uid: str,
    thread_id: str,
    *,
    call_id: str,
    incoming: list[dict],
    prev: dict,
    revision: dict,
) -> tuple[dict | None, dict | None]:
    mode = revision.get("mode") or "rewrite"
    idea_ids = [str(x) for x in (revision.get("ideaIds") or [])]
    id_set = set(idea_ids)
    round_ideas = list(prev.get("roundIdeas") or [])
    unused = list(incoming)
    replaced = 0
    new_round: list[dict] = []

    def take_match(old: dict) -> dict | None:
        pin = (old.get("pinUrl") or "").strip()
        cap = (old.get("caption") or "").strip()
        if mode == "rewrite":
            for i, n in enumerate(unused):
                if (n.get("pinUrl") or "").strip() == pin:
                    return unused.pop(i)
        else:
            for i, n in enumerate(unused):
                n_pin = (n.get("pinUrl") or "").strip()
                if n_pin == pin:
                    continue
                if (n.get("caption") or "").strip() == cap:
                    return unused.pop(i)
            for i, n in enumerate(unused):
                if (n.get("pinUrl") or "").strip() != pin:
                    return unused.pop(i)
        if unused:
            cand = unused[0]
            if mode == "replace_video" and (cand.get("pinUrl") or "").strip() == pin:
                return None
            return unused.pop(0)
        return None

    for old in round_ideas:
        oid = str(old.get("id") or "")
        if oid not in id_set:
            new_round.append(old)
            continue
        match = take_match(old)
        if not match:
            new_round.append(old)
            continue
        merged = dict(match)
        merged["id"] = oid
        if mode == "rewrite":
            merged["pinUrl"] = old.get("pinUrl") or merged.get("pinUrl") or ""
            merged["thumbnail"] = old.get("thumbnail") or merged.get("thumbnail") or ""
            merged["title"] = old.get("title") or merged.get("title") or ""
            merged["previewUrl"] = old.get("previewUrl") or merged.get("previewUrl") or ""
            merged["previewKind"] = old.get("previewKind") or merged.get("previewKind") or ""
        else:
            merged["caption"] = old.get("caption") or merged.get("caption") or ""
        new_round.append(merged)
        replaced += 1

    if replaced == 0:
        return None, {
            "ok": False,
            "error": (
                "No usable replacements for the current review. "
                "Keep the same pinUrls for rewrite, or a different pin for new video, "
                "then call propose_ideas with only the updated idea(s)."
            ),
            "approved": len(prev.get("approved") or []),
            "target": prev.get("targetCount"),
            "deniedKeys": prev.get("deniedKeys") or [],
        }

    old_choices = prev.get("roundChoices") if isinstance(prev.get("roundChoices"), dict) else {}
    round_choices = {}
    for idea in new_round:
        iid = str(idea.get("id") or "")
        if not iid:
            continue
        if iid in id_set:
            round_choices[iid] = "yes"
        else:
            round_choices[iid] = "no" if old_choices.get(iid) == "no" else "yes"
    pending = {
        "toolCallId": call_id,
        "name": "propose_ideas",
        "targetCount": int(prev.get("targetCount") or len(new_round) or 1),
        "approved": list(prev.get("approved") or []),
        "deniedKeys": list(prev.get("deniedKeys") or []),
        "roundIdeas": new_round,
        "roundChoices": round_choices,
        "rounds": int(prev.get("rounds") or 1),
        "gateClosed": False,
    }
    chat_store.set_pending_review(uid, thread_id, pending)
    return pending, None


def _agent_rounds(
    uid: str,
    thread_id: str,
    ctx: toolreg.ToolContext,
    *,
    model: dict,
    vision_images: list[dict] | None = None,
) -> Iterator[str]:
    pending_vision = list(vision_images or [])
    for _round in range(BC_TOOL_ROUNDS):
        thread = chat_store.get_thread(uid, thread_id)
        messages = chat_store.messages_for_model(
            thread, model_id=model["id"], provider=model["provider"]
        )
        tool_names = toolreg.chat_tool_names(thread)
        if _round >= tool_round_limit(tool_names):
            break
        try:
            result = providers.chat_completion(
                provider=model["provider"],
                model=model["id"],
                messages=messages,
                allow_tools=True,
                vision_images=pending_vision or None,
                tool_names=tool_names,
            )
        except Exception as exc:
            _log.exception("Provider call failed")
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
            return
        pending_vision = []
        _record_completion(uid, thread_id, model["provider"], model["id"], result)

        content = (result.get("content") or "").strip()
        tool_calls = result.get("tool_calls") or []

        if content:
            yield _sse("token", {"text": content})

        gemini_parts = result.get("gemini_parts") or []

        if not tool_calls:
            if content:
                saved: dict = {"role": "assistant", "content": content}
                if gemini_parts:
                    saved["gemini_parts"] = gemini_parts
                chat_store.append_message(uid, thread_id, saved)
            chat_store.flush_index(uid)
            usage_mod.flush_now(uid)
            yield _sse("done", {"threadId": thread_id})
            return

        saved = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }
        if gemini_parts:
            saved["gemini_parts"] = gemini_parts
        chat_store.append_message(uid, thread_id, saved)

        for tc in tool_calls:
            name = tc.get("name") or ""
            args = tc.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            call_id = tc.get("id") or uuid.uuid4().hex[:10]

            if name in toolreg.CONFIRM_TOOLS:
                pending = {
                    "id": uuid.uuid4().hex[:12],
                    "toolCallId": call_id,
                    "name": name,
                    "arguments": args,
                    "summary": toolreg.confirm_summary(name, args),
                }
                chat_store.set_pending_confirm(uid, thread_id, pending)
                chat_store.flush_index(uid)
                usage_mod.flush_now(uid)
                yield _sse("tool_confirm", pending)
                yield _sse("done", {"threadId": thread_id, "awaitingConfirm": True})
                return

            if name in toolreg.REVIEW_TOOLS:
                pending, skip = _pause_review(
                    uid, thread_id, call_id=call_id, args=args, ctx=ctx
                )
                if skip:
                    slim = _persist_tool_result(
                        uid, thread_id, name=name, call_id=call_id, result=skip
                    )
                    yield _sse("tool_result", {"name": name, "id": call_id, "result": slim})
                    continue
                yield _sse("idea_review", pending)
                chat_store.flush_index(uid)
                usage_mod.flush_now(uid)
                yield _sse("done", {"threadId": thread_id, "awaitingReview": True})
                return

            if name == "pinterest_set":
                action = str(args.get("action") or "show").strip().lower()
                yield _sse(
                    "tool_start",
                    {"name": name, "id": call_id, "arguments": args},
                )
                if action == "download":
                    pins = toolreg.normalize_pins(args.get("pins") or [])
                    toolreg.execute(uid, "pinterest_set", args, ctx)
                    ideas_pins = _pins_to_download(
                        uid,
                        thread_id,
                        attachments=_latest_user_attachments(uid, thread_id),
                        explicit_pins=pins or None,
                    )
                    if not ideas_pins:
                        slim = {
                            "ok": False,
                            "action": "download",
                            "error": "No pins to download",
                            "pins": [],
                            "count": 0,
                        }
                        _persist_tool_result(
                            uid,
                            thread_id,
                            name=name,
                            call_id=call_id,
                            result=slim,
                        )
                        yield _sse(
                            "tool_result",
                            {"name": name, "id": call_id, "result": slim},
                        )
                        yield _sse("token", {"text": slim["error"]})
                        chat_store.flush_index(uid)
                        usage_mod.flush_now(uid)
                        yield _sse("done", {"threadId": thread_id})
                        return
                    ideas = [
                        {
                            "pinUrl": p["pinUrl"],
                            "caption": p.get("caption") or p.get("title") or "",
                            "title": p.get("title") or "",
                            "kind": p.get("kind") or "",
                            "imageUrl": p.get("imageUrl") or "",
                            "previewUrl": p.get("previewUrl") or "",
                            "previewKind": p.get("previewKind") or "",
                        }
                        for p in ideas_pins
                    ]
                    dl = _download_approved_ideas(
                        uid, ideas, ctx, prepare_render=False, add_to_job=False
                    )
                    sources = [s for s in (dl.get("sources") or []) if s.get("id")]
                    errors = dl.get("errors") or []
                    slim = {
                        "ok": bool(sources),
                        "action": "download",
                        "downloaded": len(sources),
                        "count": len(ideas_pins),
                        "pins": ideas_pins,
                        "sourceIds": [s["id"] for s in sources],
                        "errors": errors,
                    }
                    _persist_tool_result(
                        uid, thread_id, name=name, call_id=call_id, result=slim
                    )
                    yield _sse(
                        "tool_result",
                        {"name": name, "id": call_id, "result": slim},
                    )
                    if sources:
                        got = len(sources)
                        fail_bit = f" {len(errors)} failed." if errors else ""
                        msg = (
                            f"Downloaded {got} video"
                            f"{'s' if got != 1 else ''} to your library.{fail_bit}"
                        )
                        yield _sse("token", {"text": msg})
                        yield _sse(
                            "saved_videos",
                            {
                                "sourceIds": [s["id"] for s in sources],
                                "count": got,
                                "errors": errors,
                            },
                        )
                    else:
                        detail = (
                            errors[0].get("error")
                            if errors
                            else "No videos could be downloaded"
                        )
                        yield _sse("error", {"message": str(detail)})
                        yield _sse("token", {"text": str(detail)})
                    chat_store.flush_index(uid)
                    usage_mod.flush_now(uid)
                    yield _sse("done", {"threadId": thread_id})
                    return

                out = toolreg.execute(uid, name, args, ctx)
                pending_vision.extend(_collect_vision(out))
                slim = _persist_tool_result(
                    uid, thread_id, name=name, call_id=call_id, result=out
                )
                yield _sse(
                    "tool_result", {"name": name, "id": call_id, "result": slim}
                )
                pins = slim.get("pins") or []
                if pins:
                    yield from _emit_pinterest_show(
                        query=slim.get("query") or "",
                        pins=pins,
                        mode=slim.get("mode") or "replace",
                    )
                continue

            yield _sse("tool_start", {"name": name, "id": call_id, "arguments": args})
            out = toolreg.execute(uid, name, args, ctx)
            pending_vision.extend(_collect_vision(out))
            slim = _persist_tool_result(uid, thread_id, name=name, call_id=call_id, result=out)
            yield _sse("tool_result", {"name": name, "id": call_id, "result": slim})
            # search_pinterest only fills the bank — grid updates via pinterest_set.

        chat_store.flush_index(uid)
        usage_mod.flush_now(uid)

    chat_store.flush_index(uid)
    usage_mod.flush_now(uid)
    yield _sse("done", {"threadId": thread_id, "truncated": True})


def confirm_and_continue(
    uid: str,
    thread_id: str,
    *,
    accepted: bool,
    ctx: toolreg.ToolContext,
) -> Iterator[str]:
    thread = chat_store.get_thread(uid, thread_id)
    pending = thread.get("pendingConfirm")
    if not pending:
        yield _sse("error", {"message": "No pending tool confirmation"})
        return

    chat_store.set_pending_confirm(uid, thread_id, None)
    call_id = pending.get("toolCallId") or uuid.uuid4().hex[:10]
    name = pending.get("name") or ""
    args = pending.get("arguments") or {}

    vision: list[dict] = []
    if not accepted:
        slim = _persist_tool_result(
            uid,
            thread_id,
            name=name,
            call_id=call_id,
            result={
                "ok": False,
                "declined": True,
                "error": "User declined this action.",
            },
        )
        yield _sse("tool_result", {"name": name, "id": call_id, "result": slim})
    else:
        yield _sse("tool_start", {"name": name, "id": call_id})
        out = toolreg.execute(uid, name, args, ctx)
        vision = _collect_vision(out)
        slim = _persist_tool_result(
            uid, thread_id, name=name, call_id=call_id, result=out
        )
        yield _sse("tool_result", {"name": name, "id": call_id, "result": slim})

    try:
        model = chat_models.resolve_model(thread.get("model"))
    except ValueError as exc:
        yield _sse("error", {"message": str(exc)})
        return

    # Keep tools on so the original ask (captions / render / upload) can continue.
    yield from _agent_rounds(
        uid, thread_id, ctx, model=model, vision_images=vision or None
    )


def _ensure_vertical_crop(uid: str, src: dict, ctx: toolreg.ToolContext) -> dict:
    existing = src.get("crop") if isinstance(src.get("crop"), dict) else None
    if existing and existing.get("enabled"):
        return src
    setter = getattr(ctx, "set_source_crop", None)
    if not callable(setter):
        return src
    from ..encode import centered_max_916_crop

    auto = centered_max_916_crop(src.get("width") or 0, src.get("height") or 0)
    if not auto:
        return src
    try:
        updated = setter(uid, src["id"], auto)
        return updated if isinstance(updated, dict) else src
    except Exception:
        _log.debug("Auto 9:16 crop failed for %s", src.get("id"), exc_info=True)
        return src


def _download_approved_ideas(
    uid: str,
    ideas: list[dict],
    ctx: toolreg.ToolContext,
    *,
    prepare_render: bool = True,
    add_to_job: bool = True,
) -> dict:
    from ..ingest import IngestError, download_url
    from .. import workspace as ws

    def _key(url: str) -> str:
        return (url or "").strip().rstrip("/").lower()

    sources_by_url: dict[str, dict] = {}
    existing = ctx.list_sources(uid)
    if not add_to_job:
        lib_fn = getattr(ctx, "list_library", None)
        if callable(lib_fn):
            existing = lib_fn(uid) or existing
    for src in existing:
        origin = _key(src.get("url") or "")
        if origin:
            sources_by_url[origin] = src

    errors = []
    pairs = []
    sources_out: list[dict] = []
    seen_ids: set[str] = set()
    for idea in ideas:
        pin_url = (idea.get("pinUrl") or "").strip()
        caption = (idea.get("caption") or "").strip()
        if not pin_url:
            continue
        if prepare_render and not caption:
            continue
        src = sources_by_url.get(_key(pin_url))
        is_image = str(idea.get("kind") or idea.get("previewKind") or "").lower() in (
            "image",
        ) or bool(idea.get("imageUrl"))
        if not src and is_image:
            try:
                from .. import images as image_lib

                image_url = str(idea.get("imageUrl") or idea.get("previewUrl") or "").strip()
                if not image_url:
                    errors.append({"url": pin_url, "error": "Image pin has no imageUrl"})
                    continue
                img = image_lib.register_from_url(
                    uid, image_url, origin="pinterest", title=idea.get("title") or ""
                )
                img["url"] = pin_url
                img["pinUrl"] = pin_url
                st = image_lib.state(uid)
                if img.get("id") in st["images"]:
                    st["images"][img["id"]]["url"] = pin_url
                    st["images"][img["id"]]["pinUrl"] = pin_url
                    image_lib.save_state(uid)
                src = img
                sources_by_url[_key(pin_url)] = src
            except IngestError as exc:
                errors.append({"url": pin_url, "error": str(exc)})
                continue
        if not src and is_image:
            continue
        if not src:
            try:
                path = download_url(uid, pin_url)
                src = ctx.register_downloaded(
                    uid, path, "url", pin_url, add_to_job=add_to_job
                )
                sources_by_url[_key(pin_url)] = src
            except IngestError as exc:
                errors.append({"url": pin_url, "error": str(exc)})
                continue
        if prepare_render and not is_image:
            src = _ensure_vertical_crop(uid, src, ctx)
        sources_by_url[_key(pin_url)] = src
        sid = str(src.get("id") or "")
        if sid and sid not in seen_ids:
            seen_ids.add(sid)
            sources_out.append({"id": sid, "name": src.get("name")})
        if caption:
            pairs.append(
                {
                    "sourceId": src["id"],
                    "sourceName": src.get("name"),
                    "caption": caption,
                    "pinUrl": pin_url,
                }
            )
    if prepare_render and pairs:
        caps = ws.unique_captions_from_pairs(pairs)
        patch = {"renderPairs": pairs, "captions": caps}
        if ctx.thread_id:
            from .. import chat_store

            chat_store.save_thread_plan(uid, ctx.thread_id, patch)
        else:
            ws.save_workspace(uid, patch)
        try:
            from .. import hooks as hooks_mod

            unique = []
            seen: set[str] = set()
            for p in pairs:
                cap = str((p or {}).get("caption") or "").strip()
                if cap and cap.lower() not in seen:
                    seen.add(cap.lower())
                    unique.append(cap)
            hooks_mod.record_generated_hooks(
                uid,
                unique,
                offer_id=hooks_mod.resolve_offer_id(uid),
                mode="chat",
                extra="Approved ideas",
                thread_id=ctx.thread_id or "",
            )
        except Exception:
            _log.debug("Hook history record failed", exc_info=True)
    return {
        "pairs": pairs,
        "errors": errors,
        "downloaded": len(sources_out),
        "sources": sources_out,
    }


def _save_approved_videos(
    uid: str,
    thread_id: str,
    *,
    approved: list[dict],
    denied_keys: list[str],
    call_id: str,
    gate_closed: bool,
    target: int,
    ctx: toolreg.ToolContext,
    user_text: str = "",
) -> Iterator[str]:
    """Download selected pins into the library without render/upload."""
    dl = {"pairs": [], "errors": [], "downloaded": 0, "sources": []}
    if approved:
        yield _sse("tool_start", {"name": "add_source_urls", "id": call_id})
        dl = _download_approved_ideas(
            uid, approved, ctx, prepare_render=False, add_to_job=False
        )
    sources = [s for s in (dl.get("sources") or []) if s.get("id")]
    errors = dl.get("errors") or []
    if not sources:
        detail = errors[0].get("error") if errors else "No videos could be downloaded"
        yield _sse("error", {"message": str(detail)})
        yield _sse("token", {"text": str(detail)})
        yield _sse("done", {"threadId": thread_id})
        return

    chat_store.set_pending_review(uid, thread_id, None)
    n = len(sources)
    payload = {
        "ok": True,
        "approved": len(approved),
        "target": target,
        "need": 0,
        "filled": True,
        "savedOnly": True,
        "sourceIds": [s["id"] for s in sources],
        "downloadErrors": errors,
        "deniedKeys": denied_keys,
        "message": (
            f"User asked to download and save the {n} selected video(s) without "
            "rendering or uploading"
            + (
                f': "{(user_text or "").strip()[:500]}"'
                if (user_text or "").strip()
                else ""
            )
            + ". Pins are in the library. Do not preview, render, or upload."
        ),
    }
    yield from _emit_review_followup(
        uid, thread_id, call_id=call_id, payload=payload, gate_closed=gate_closed
    )
    note = (user_text or "").strip()
    if note:
        chat_store.append_message(
            uid, thread_id, {"role": "user", "content": note[:4000]}
        )
    fail_bit = f" {len(errors)} failed to download." if errors else ""
    msg = (
        f"Saved {n} video{'s' if n != 1 else ''} to your library.{fail_bit} "
        "A zip of the files should start downloading."
    )
    chat_store.append_message(uid, thread_id, {"role": "assistant", "content": msg})
    yield _sse("token", {"text": msg})
    yield _sse(
        "saved_videos",
        {"sourceIds": [s["id"] for s in sources], "count": n, "errors": errors},
    )
    yield _sse("done", {"threadId": thread_id})


def review_and_continue(
    uid: str,
    thread_id: str,
    *,
    approved_ids: list[str],
    denied_ids: list[str],
    ctx: toolreg.ToolContext,
    proceed: bool = False,
    save_only: bool = False,
    user_text: str = "",
) -> Iterator[str]:
    thread = chat_store.get_thread(uid, thread_id)
    pending = thread.get("pendingReview")
    if not isinstance(pending, dict):
        yield _sse("error", {"message": "No pending idea review"})
        return

    call_id = pending.get("toolCallId") or uuid.uuid4().hex[:10]
    gate_closed = bool(pending.get("gateClosed"))
    target = _target_count(pending)
    skip_refill = proceed or save_only
    approved, denied_keys = _apply_round_choices(
        pending,
        approved_ids=approved_ids,
        denied_ids=denied_ids,
        limit_to_target=not skip_refill,
    )

    need = max(0, target - len(approved))
    rounds = int(pending.get("rounds") or 1)
    maxed = rounds >= toolreg.MAX_REVIEW_ROUNDS and need > 0

    if skip_refill and not approved:
        yield _sse("error", {"message": "Approve at least one idea first"})
        return

    if need > 0 and not maxed and not skip_refill:
        chat_store.set_pending_review(
            uid,
            thread_id,
            {
                **pending,
                "approved": approved,
                "deniedKeys": denied_keys,
                "roundIdeas": [],
                "roundChoices": {},
                "gateClosed": True,
            },
        )
        payload = {
            "ok": True,
            "approved": len(approved),
            "target": target,
            "need": need,
            "deniedKeys": denied_keys,
            "approvedIdeas": [_slim_idea(i) for i in approved],
            "message": (
                f"User approved {len(approved)}/{target}. Find {need} more new ideas. "
                "Do not reuse deniedKeys. Call search_pinterest then propose_ideas "
                f"with targetCount={target} and only {need} new ideas. "
                "Do not ask which pins in chat — show them with propose_ideas."
            ),
        }
        yield from _emit_review_followup(
            uid, thread_id, call_id=call_id, payload=payload, gate_closed=gate_closed
        )
        try:
            model = chat_models.resolve_model(thread.get("model"))
        except ValueError as exc:
            yield _sse("error", {"message": str(exc)})
            return
        yield from _agent_rounds(uid, thread_id, ctx, model=model)
        return

    if save_only:
        yield from _save_approved_videos(
            uid,
            thread_id,
            approved=approved,
            denied_keys=denied_keys,
            call_id=call_id,
            gate_closed=gate_closed,
            target=target,
            ctx=ctx,
            user_text=user_text,
        )
        return

    # Quota met, or refill cap hit — download approved pins and persist pairs.
    chat_store.set_pending_review(uid, thread_id, None)
    dl = {"pairs": [], "errors": [], "downloaded": 0}
    if approved:
        yield _sse("tool_start", {"name": "add_source_urls", "id": call_id})
        dl = _download_approved_ideas(
            uid, approved, ctx, prepare_render=True, add_to_job=False
        )
    payload = {
        "ok": True,
        "approved": len(approved),
        "target": target,
        "need": 0 if proceed or not need else need,
        "filled": need == 0 or proceed,
        "maxedRounds": maxed,
        "proceededEarly": bool(proceed),
        "renderPairs": dl.get("pairs") or [],
        "downloadErrors": dl.get("errors") or [],
        "message": (
            (
                f"User asked to render and upload the {len(approved)} selected idea(s) now"
                + (
                    f': "{(user_text or "").strip()[:500]}"'
                    if (user_text or "").strip()
                    else ""
                )
                + ". Do not find more ideas. Pins downloaded. Call set_render_plan or "
                "preview_plan with the "
                "renderPairs, then start_render with the same pairs, then "
                "start_tiktok_upload with the batchId from start_render after it finishes. "
                "Do not ask which pins in chat. Do not use other session videos."
            )
            if proceed
            else (
                "Quota filled. Pins downloaded. Call set_render_plan or preview_plan "
                "with the renderPairs, "
                "then start_render with the same pairs after the user can review."
                if need == 0
                else (
                    f"Stopped after {rounds} review rounds with {len(approved)}/{target} approved. "
                    "Call set_render_plan or preview_plan with renderPairs for what was "
                    "approved, or search again."
                )
            )
        ),
    }
    yield from _emit_review_followup(
        uid, thread_id, call_id=call_id, payload=payload, gate_closed=gate_closed
    )
    note = (user_text or "").strip()
    if note:
        chat_store.append_message(
            uid, thread_id, {"role": "user", "content": note[:4000]}
        )
    try:
        model = chat_models.resolve_model(thread.get("model"))
    except ValueError as exc:
        yield _sse("error", {"message": str(exc)})
        return
    yield from _agent_rounds(uid, thread_id, ctx, model=model)


def revise_ideas_and_continue(
    uid: str,
    thread_id: str,
    *,
    action: str,
    idea_ids: list[str],
    feedback: str,
    ctx: toolreg.ToolContext,
) -> Iterator[str]:
    thread = chat_store.get_thread(uid, thread_id)
    pending = thread.get("pendingReview")
    if not isinstance(pending, dict):
        yield _sse("error", {"message": "No pending idea review"})
        return

    round_ideas = pending.get("roundIdeas") or []
    if not round_ideas:
        yield _sse("error", {"message": "No ideas in the current review round"})
        return

    mode = "replace_video" if action == "replace_video" else "rewrite"
    if idea_ids:
        id_set = {str(x) for x in idea_ids}
        targets = [i for i in round_ideas if str(i.get("id") or "") in id_set]
    elif mode == "rewrite":
        targets = list(round_ideas)
    else:
        yield _sse("error", {"message": "Pick an idea to find a new video for"})
        return
    if not targets:
        yield _sse("error", {"message": "Those ideas are not in the current round"})
        return

    target_ids = [str(i.get("id") or "") for i in targets]
    pin_by_id = {str(i.get("id") or ""): i.get("pinUrl") or "" for i in targets}
    caption_by_id = {str(i.get("id") or ""): i.get("caption") or "" for i in targets}
    denied_keys = list(pending.get("deniedKeys") or [])
    exclude_urls: list[str] = []
    if mode == "replace_video":
        for idea in targets:
            pin = (idea.get("pinUrl") or "").strip()
            if pin:
                exclude_urls.append(pin)
            key = toolreg.idea_deny_key(pin, idea.get("caption") or "")
            if key and key not in denied_keys:
                denied_keys.append(key)

    call_id = pending.get("toolCallId") or uuid.uuid4().hex[:10]
    fb = (feedback or "").strip()[:1500]
    try:
        target_count = int(pending.get("targetCount") or 1)
    except (TypeError, ValueError):
        target_count = 1

    slim_targets = [
        {
            "id": i.get("id"),
            "pinUrl": i.get("pinUrl") or "",
            "caption": i.get("caption") or "",
            "title": i.get("title") or "",
            "thumbnail": i.get("thumbnail") or "",
        }
        for i in targets
    ]
    if mode == "rewrite":
        message = (
            f"User asked to REWRITE captions for {len(targets)} idea(s). "
            "Keep each pinUrl, thumbnail, and title exactly. Only change caption text. "
            f"Call propose_ideas with ONLY these {len(targets)} updated idea(s) "
            f"and targetCount={target_count}. Do not search Pinterest. Do not download."
        )
    else:
        message = (
            f"User asked to FIND A NEW VIDEO for {len(targets)} idea(s). "
            "Keep each caption exactly. Call search_pinterest excluding excludeUrls "
            "and deniedKeys, then propose_ideas with ONLY the replacement idea(s) "
            f"(same count: {len(targets)}) and targetCount={target_count}. "
            "Do not reuse deniedKeys or the excluded pin URLs. Do not download."
        )
    if fb:
        message += f" Feedback: {fb}"

    chat_store.set_pending_review(
        uid,
        thread_id,
        {
            **pending,
            "deniedKeys": denied_keys,
            "revision": {
                "mode": mode,
                "ideaIds": target_ids,
                "pinById": pin_by_id,
                "captionById": caption_by_id,
            },
        },
    )
    payload = {
        "ok": True,
        "revision": mode,
        "count": len(targets),
        "ideas": slim_targets,
        "excludeUrls": exclude_urls,
        "deniedKeys": denied_keys,
        "feedback": fb,
        "targetCount": target_count,
        "message": message,
    }
    yield _sse("tool_start", {"name": "propose_ideas", "id": call_id})
    yield from _emit_review_followup(
        uid,
        thread_id,
        call_id=call_id,
        payload=payload,
        gate_closed=bool(pending.get("gateClosed")),
    )
    try:
        model = chat_models.resolve_model(thread.get("model"))
    except ValueError as exc:
        yield _sse("error", {"message": str(exc)})
        return
    yield from _agent_rounds(uid, thread_id, ctx, model=model)
