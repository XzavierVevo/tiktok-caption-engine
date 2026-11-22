"""USD cost estimates for chat models, Pinterest credits, and Perplexity research.

Rates are $/1M tokens unless noted. Cost is computed at record time so later
rate edits do not rewrite history. Unknown models still count tokens at $0.
"""

from __future__ import annotations

import os
from typing import Any

# (input, output, cache_read, cache_write) per 1M tokens.
# Anthropic cache write ≈ 5-minute write (1.25× input). Cache read is the hit rate.
Rate = tuple[float, float, float, float]

_RATES: dict[str, Rate] = {
    # Anthropic — https://platform.claude.com/docs/en/about-claude/pricing
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-5.5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-5": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-5": (3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
    # xAI — <200k prompt band (chat threads stay well under)
    "grok-4.6": (2.0, 6.0, 0.5, 2.0),
    "grok-4.5": (2.0, 6.0, 0.3, 2.0),
    "grok-4.3": (1.25, 2.5, 0.2, 1.25),
    "grok-4.20-0309-reasoning": (1.25, 2.5, 0.2, 1.25),
    "grok-4.20-0309-non-reasoning": (1.25, 2.5, 0.2, 1.25),
    "grok-4.20-multi-agent-0309": (1.25, 2.5, 0.2, 1.25),
    "grok-build-0.1": (1.0, 2.0, 0.2, 1.0),
    # OpenAI — short-context list prices
    "gpt-5.6-sol": (5.0, 30.0, 0.5, 6.25),
    "gpt-5.6-terra": (2.0, 12.0, 0.2, 2.5),
    "gpt-5.6-luna": (0.2, 1.2, 0.02, 0.25),
    "gpt-5.5": (5.0, 30.0, 0.5, 6.25),
    "gpt-5.4-nano": (0.2, 1.25, 0.02, 0.25),
    "gpt-5.4": (2.5, 15.0, 0.25, 3.125),
    "gpt-5.2": (2.5, 15.0, 0.25, 3.125),
    "gpt-5.1": (1.25, 10.0, 0.125, 1.5625),
    "gpt-5-nano": (0.05, 0.4, 0.005, 0.0625),
    "gpt-5-mini": (0.25, 2.0, 0.025, 0.3125),
    "gpt-5": (1.25, 10.0, 0.125, 1.5625),
    "gpt-4.1-nano": (0.1, 0.4, 0.025, 0.125),
    "gpt-4.1-mini": (0.4, 1.6, 0.1, 0.5),
    "gpt-4.1": (2.0, 8.0, 0.5, 2.5),
    # Gemini
    "gemini-3.7-flash": (0.75, 3.75, 0.075, 0.75),
    "gemini-3.6-flash": (0.5, 3.0, 0.05, 0.5),
    "gemini-3.5-flash-lite": (0.1, 0.4, 0.01, 0.1),
    "gemini-3.5-flash": (0.5, 3.0, 0.05, 0.5),
    "gemini-3.1-pro-preview": (2.0, 12.0, 0.2, 2.0),
    "gemini-3.1-flash-lite": (0.1, 0.4, 0.01, 0.1),
    "gemini-3-flash-preview": (0.5, 3.0, 0.05, 0.5),
    # Perplexity Sonar Deep Research
    "sonar-deep-research": (2.0, 8.0, 0.2, 2.0),
    "perplexity-research": (2.0, 8.0, 0.2, 2.0),
}

# Longest prefix first, for ids not listed exactly.
_PREFIXES: list[tuple[str, Rate]] = sorted(
    (
        ("claude-fable", (10.0, 50.0, 1.0, 12.5)),
        ("claude-opus-5.5", (5.0, 25.0, 0.5, 6.25)),
        ("claude-opus-5", (5.0, 25.0, 0.5, 6.25)),
        ("claude-opus-4", (5.0, 25.0, 0.5, 6.25)),
        ("claude-sonnet-5", (2.0, 10.0, 0.2, 2.5)),
        ("claude-sonnet-4", (3.0, 15.0, 0.3, 3.75)),
        ("claude-haiku", (1.0, 5.0, 0.1, 1.25)),
        ("grok-4.6", (2.0, 6.0, 0.5, 2.0)),
        ("grok-4.5", (2.0, 6.0, 0.3, 2.0)),
        ("grok-4.20", (1.25, 2.5, 0.2, 1.25)),
        ("grok-build", (1.0, 2.0, 0.2, 1.0)),
        ("grok-4", (1.25, 2.5, 0.2, 1.25)),
        ("gpt-5.6-sol", (5.0, 30.0, 0.5, 6.25)),
        ("gpt-5.6-terra", (2.0, 12.0, 0.2, 2.5)),
        ("gpt-5.6-luna", (0.2, 1.2, 0.02, 0.25)),
        ("gpt-5.6", (5.0, 30.0, 0.5, 6.25)),
        ("gpt-5.4-nano", (0.2, 1.25, 0.02, 0.25)),
        ("gpt-5-nano", (0.05, 0.4, 0.005, 0.0625)),
        ("gpt-5-mini", (0.25, 2.0, 0.025, 0.3125)),
        ("gpt-4.1-nano", (0.1, 0.4, 0.025, 0.125)),
        ("gpt-4.1-mini", (0.4, 1.6, 0.1, 0.5)),
        ("gpt-4.1", (2.0, 8.0, 0.5, 2.5)),
        ("gpt-5", (1.25, 10.0, 0.125, 1.5625)),
        ("gemini-3.1-pro", (2.0, 12.0, 0.2, 2.0)),
        ("gemini-3.7", (0.75, 3.75, 0.075, 0.75)),
        ("flash-lite", (0.1, 0.4, 0.01, 0.1)),
        ("gemini-3", (0.5, 3.0, 0.05, 0.5)),
        ("sonar-deep-research", (2.0, 8.0, 0.2, 2.0)),
    ),
    key=lambda item: len(item[0]),
    reverse=True,
)

PINTEREST_MODEL = "pinterest-search"
RESEARCH_MODEL = "sonar-deep-research"


def _pinterest_credit_usd() -> float:
    raw = (os.environ.get("PINTEREST_CREDIT_USD") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 0.00188  # Scrape Creators Freelance pack ($47 / 25k credits)


def _research_flat_usd() -> float:
    raw = (os.environ.get("PERPLEXITY_RESEARCH_FLAT_USD") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 0.50


def lookup(model_id: str | None) -> Rate | None:
    mid = (model_id or "").strip().lower()
    if not mid:
        return None
    if ":" in mid:
        mid = mid.split(":", 1)[-1]
    if mid in _RATES:
        return _RATES[mid]
    for prefix, rate in _PREFIXES:
        if mid.startswith(prefix):
            return rate
    return None


def cost_usd(
    model_id: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[float, bool]:
    """Return (usd, priced). priced is False when the model has no rate row."""
    rate = lookup(model_id)
    if rate is None:
        return 0.0, False
    inp, out, cache_r, cache_w = rate
    usd = (
        max(0, int(input_tokens or 0)) * inp
        + max(0, int(output_tokens or 0)) * out
        + max(0, int(cache_read_tokens or 0)) * cache_r
        + max(0, int(cache_write_tokens or 0)) * cache_w
    ) / 1_000_000.0
    return usd, True


def pinterest_cost_usd(credits: int) -> float:
    return max(0, int(credits or 0)) * _pinterest_credit_usd()


def research_flat_usd() -> float:
    return _research_flat_usd()


def research_cost_usd(usage: dict[str, Any] | None) -> tuple[float, bool]:
    """Token-priced when usage is present; otherwise the documented flat estimate."""
    blob = usage or {}
    inp = int(blob.get("input_tokens") or 0)
    out = int(blob.get("output_tokens") or 0)
    cache_r = int(blob.get("cache_read_tokens") or 0)
    cache_w = int(blob.get("cache_write_tokens") or 0)
    if inp or out or cache_r or cache_w:
        return cost_usd(
            RESEARCH_MODEL,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache_r,
            cache_write_tokens=cache_w,
        )
    return research_flat_usd(), True
