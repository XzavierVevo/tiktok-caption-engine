"""Perplexity deep research (Agent API high preset, Sonar fallback)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterator

import requests

_log = logging.getLogger(__name__)

AGENT_URL = "https://api.perplexity.ai/v1/agent"
SONAR_ASYNC_URL = "https://api.perplexity.ai/v1/async/sonar"
SONAR_URL = "https://api.perplexity.ai/v1/sonar"

RESEARCH_INSTRUCTIONS = (
    "Conduct exhaustive, source-backed deep research. Search widely, read "
    "primary sources, and write a thorough structured report with inline "
    "citations. Prefer recent, specific facts, numbers, named examples, and "
    "contrasting viewpoints over generic advice. End with a short list of "
    "the most useful sources."
)

POLL_SECONDS = 2.5
ASYNC_MAX_SECONDS = 12 * 60
STREAM_READ_TIMEOUT = 600

# UTF-8 punctuation decoded as Latin-1 (â + C1 controls → boxes in the UI).
_MOJIBAKE_RE = re.compile(r"[\u00c2\u00c3\u00e2][\u0080-\u00bf]")


def repair_mojibake(text: str) -> str:
    """Undo Latin-1 mis-decode of UTF-8 (e.g. â\\x80\\x94 → em dash)."""
    s = text or ""
    for _ in range(3):
        if not _MOJIBAKE_RE.search(s):
            break
        try:
            s = s.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            break
    return s


def api_key() -> str:
    return (os.environ.get("PERPLEXITY_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise RuntimeError(
            "PERPLEXITY_API_KEY is not configured. Set it in .env or as a "
            "server secret, then restart the app."
        )
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _citation_lines(citations: list[Any]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for item in citations:
        title = ""
        url = ""
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or "").strip()
            url = str(item.get("url") or item.get("link") or item.get("uri") or "").strip()
        key = url or title
        if not key or key in seen:
            continue
        seen.add(key)
        if title and url and title != url:
            lines.append(f"- {title} — {url}")
        else:
            lines.append(f"- {key}")
    return lines


def format_report(text: str, citations: list[Any] | None = None) -> str:
    body = repair_mojibake(text or "").strip()
    if "</think>" in body:
        body = body.split("</think>", 1)[-1].strip()
    extra = _citation_lines(citations or [])
    if not extra:
        return body
    lower = body.lower()
    if "sources" in lower[-800:] or "http://" in lower or "https://" in lower:
        return body
    return body + "\n\nSources\n" + "\n".join(extra)


def _collect_citations(*blobs: Any) -> list[Any]:
    out: list[Any] = []
    for blob in blobs:
        if not blob:
            continue
        if isinstance(blob, list):
            out.extend(blob)
            continue
        if not isinstance(blob, dict):
            continue
        for key in ("citations", "search_results", "results", "sources"):
            val = blob.get(key)
            if isinstance(val, list):
                out.extend(val)
        output = blob.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    out.extend(_collect_citations(item))
                    content = item.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                anns = part.get("annotations") or part.get("citations")
                                if isinstance(anns, list):
                                    out.extend(anns)
    return out


def run_deep_research(query: str) -> Iterator[dict[str, Any]]:
    """Yield {type: status|token|done|error, ...} events for one research run."""
    q = (query or "").strip()
    if not q:
        yield {"type": "error", "message": "Usage: /research <what you want to look up>"}
        return
    try:
        _headers()
    except RuntimeError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    yield {"type": "status", "message": "Starting Perplexity deep research… this can take a few minutes."}
    streamed = False

    def _mark(events: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        nonlocal streamed
        for ev in events:
            if ev.get("type") == "token" and ev.get("text"):
                streamed = True
            yield ev

    try:
        yield from _mark(_agent_stream(q))
        return
    except Exception as exc:
        if streamed:
            _log.exception("Agent API failed after streaming")
            yield {"type": "error", "message": f"Perplexity research failed: {exc}"}
            return
        _log.warning("Agent API deep research failed, trying Sonar: %s", exc)
        if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
            yield {
                "type": "error",
                "message": "Perplexity API key was rejected. Check PERPLEXITY_API_KEY.",
            }
            return

    try:
        yield from _mark(_sonar_async(q))
        return
    except Exception as exc:
        if streamed:
            _log.exception("Sonar async failed after streaming")
            yield {"type": "error", "message": f"Perplexity research failed: {exc}"}
            return
        _log.warning("Sonar async failed, trying sync Sonar: %s", exc)

    try:
        yield from _mark(_sonar_sync(q))
    except Exception as exc:
        _log.exception("Perplexity deep research failed")
        yield {"type": "error", "message": f"Perplexity research failed: {exc}"}


def _agent_stream(query: str) -> Iterator[dict[str, Any]]:
    payload = {
        "preset": "high",
        "stream": True,
        "input": query,
        "instructions": RESEARCH_INSTRUCTIONS,
    }
    with requests.post(
        AGENT_URL,
        headers={**_headers(), "Accept": "text/event-stream"},
        json=payload,
        stream=True,
        timeout=(20, STREAM_READ_TIMEOUT),
    ) as res:
        if res.status_code >= 400:
            detail = (res.text or "")[:400]
            raise RuntimeError(f"Agent API HTTP {res.status_code}: {detail}")
        ctype = (res.headers.get("content-type") or "").lower()
        if "application/json" in ctype and "event-stream" not in ctype:
            payload_json = res.json()
            text = _output_text(payload_json) or str(
                (((payload_json.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
            )
            citations = _collect_citations(payload_json)
            report = format_report(text, citations)
            if not report:
                raise RuntimeError("Perplexity returned an empty research report.")
            yield {"type": "token", "text": report}
            yield _done_event(report, citations, _usage_from_payload(payload_json))
            return

        acc = ""
        citations: list[Any] = []
        event_name = "message"
        data_lines: list[str] = []
        usage: dict[str, int] | None = None

        # requests defaults HTTP bodies to ISO-8859-1 when charset is missing,
        # which turns UTF-8 dashes/quotes into â + boxes in the chat UI.
        for raw_line in res.iter_lines(decode_unicode=False):
            if raw_line is None:
                continue
            line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line.strip():
                continue
            raw = "\n".join(data_lines).strip()
            data_lines = []
            if not raw or raw == "[DONE]":
                event_name = "message"
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                event_name = "message"
                continue
            typ = obj.get("type") or event_name
            event_name = "message"
            if typ in ("response.reasoning.started", "response.in_progress"):
                yield {"type": "status", "message": "Searching and reading sources…"}
            elif typ == "response.reasoning.search_queries":
                queries = obj.get("queries") or []
                if queries:
                    shown = ", ".join(str(x) for x in queries[:4])
                    yield {"type": "status", "message": f"Searching: {shown}"}
            elif typ == "response.reasoning.search_results":
                results = obj.get("results") or []
                citations.extend(results)
                n = len(results)
                if n:
                    yield {"type": "status", "message": f"Found {n} source{'s' if n != 1 else ''}…"}
            elif typ == "response.output_text.delta":
                delta = repair_mojibake(obj.get("delta") or "")
                if delta:
                    acc += delta
                    yield {"type": "token", "text": delta}
            elif typ == "response.output_text.done":
                text = repair_mojibake(obj.get("text") or "")
                if text and not acc:
                    acc = text
                    yield {"type": "token", "text": text}
            elif typ == "response.completed":
                resp = obj.get("response") or obj
                citations.extend(_collect_citations(resp, obj))
                usage = _usage_from_payload(resp) or _usage_from_payload(obj) or usage
                if not acc:
                    acc = _output_text(resp)
                    if acc:
                        yield {"type": "token", "text": acc}
            elif typ == "response.failed":
                err = obj.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err or "Research failed")
                raise RuntimeError(msg or "Research failed")

        report = format_report(acc, citations)
        if not report:
            raise RuntimeError("Perplexity returned an empty research report.")
        # If we streamed tokens already, only append sources that weren't streamed.
        extra = ""
        if report != acc.strip():
            extra = report[len(acc.rstrip()) :] if report.startswith(acc.strip()) else ""
            if extra:
                yield {"type": "token", "text": extra}
        yield _done_event(report, citations, usage)


def _output_text(resp: dict[str, Any]) -> str:
    if not isinstance(resp, dict):
        return ""
    for key in ("output_text", "text"):
        val = resp.get(key)
        if isinstance(val, str) and val.strip():
            return repair_mojibake(val)
    chunks: list[str] = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    chunks.append(part)
                elif isinstance(part, dict):
                    t = part.get("text") or part.get("content")
                    if isinstance(t, str):
                        chunks.append(t)
    return repair_mojibake("".join(chunks))


def _usage_from_payload(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    u = payload.get("usage")
    if not isinstance(u, dict) and isinstance(payload.get("response"), dict):
        u = payload["response"].get("usage")
    if not isinstance(u, dict):
        return None
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    cached = 0
    details = u.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    cached = max(0, min(cached, prompt))
    if prompt <= 0 and completion <= 0:
        return None
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": completion,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }


def _done_event(text: str, citations: list[Any], usage: dict[str, int] | None = None) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "type": "done",
        "text": text,
        "citations": _citation_urls(citations),
    }
    if usage:
        ev["usage"] = usage
    return ev


def _citation_urls(citations: list[Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in citations:
        url = ""
        if isinstance(item, str) and item.startswith("http"):
            url = item
        elif isinstance(item, dict):
            url = str(item.get("url") or item.get("link") or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:40]


def _sonar_async(query: str) -> Iterator[dict[str, Any]]:
    yield {"type": "status", "message": "Queued Perplexity Sonar Deep Research…"}
    res = requests.post(
        SONAR_ASYNC_URL,
        headers=_headers(),
        json={
            "request": {
                "model": "sonar-deep-research",
                "messages": [
                    {"role": "system", "content": RESEARCH_INSTRUCTIONS},
                    {"role": "user", "content": query},
                ],
                "reasoning_effort": "high",
            }
        },
        timeout=30,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Sonar async HTTP {res.status_code}: {(res.text or '')[:400]}")
    data = res.json()
    req_id = data.get("id")
    if not req_id:
        raise RuntimeError("Sonar async did not return a request id.")
    deadline = time.time() + ASYNC_MAX_SECONDS
    last_status = ""
    last_ping = 0.0
    while time.time() < deadline:
        poll = requests.get(
            f"{SONAR_ASYNC_URL}/{req_id}",
            headers=_headers(),
            timeout=30,
        )
        if poll.status_code >= 400:
            raise RuntimeError(f"Sonar poll HTTP {poll.status_code}: {(poll.text or '')[:400]}")
        body = poll.json()
        status = str(body.get("status") or "").upper()
        now = time.time()
        if status != last_status or now - last_ping >= 15:
            last_status = status
            last_ping = now
            yield {"type": "status", "message": f"Deep research {status.lower() or 'running'}…"}
        if status in ("COMPLETED", "COMPLETE", "SUCCEEDED", "SUCCESS"):
            inner = body.get("response") or body
            text, citations = _sonar_content(inner)
            report = format_report(text, citations)
            if not report:
                raise RuntimeError("Perplexity returned an empty research report.")
            yield {"type": "token", "text": report}
            yield _done_event(report, citations, _usage_from_payload(inner) or _usage_from_payload(body))
            return
        if status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
            raise RuntimeError(body.get("error_message") or f"Sonar research {status.lower()}")
        time.sleep(POLL_SECONDS)
    raise RuntimeError("Perplexity deep research timed out. Try a narrower question.")


def _sonar_sync(query: str) -> Iterator[dict[str, Any]]:
    yield {"type": "status", "message": "Running Sonar Deep Research…"}
    res = requests.post(
        SONAR_URL,
        headers=_headers(),
        json={
            "model": "sonar-deep-research",
            "messages": [
                {"role": "system", "content": RESEARCH_INSTRUCTIONS},
                {"role": "user", "content": query},
            ],
            "reasoning_effort": "high",
        },
        timeout=(20, STREAM_READ_TIMEOUT),
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Sonar HTTP {res.status_code}: {(res.text or '')[:400]}")
    payload = res.json()
    text, citations = _sonar_content(payload)
    report = format_report(text, citations)
    if not report:
        raise RuntimeError("Perplexity returned an empty research report.")
    yield {"type": "token", "text": report}
    yield _done_event(report, citations, _usage_from_payload(payload))


def _sonar_content(payload: dict[str, Any]) -> tuple[str, list[Any]]:
    if not isinstance(payload, dict):
        return "", []
    inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    choices = inner.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        text = str(msg.get("content") or "")
        # Drop hidden <think> blocks if the model leaked them.
        if "</think>" in text:
            text = text.split("</think>", 1)[-1]
    citations = _collect_citations(inner, payload)
    return repair_mojibake(text.strip()), citations
