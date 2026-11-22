"""Curated chat model allowlist + which providers have server keys."""

from __future__ import annotations

import os
from typing import Any

# Newest first within each provider. Chat/text models only (no image, TTS,
# live-audio, video, embeddings). Gemini 2.5 is omitted: Google now returns
# 404 for new API keys ("no longer available to new users").
MODELS: list[dict[str, str]] = [
    # Anthropic — https://platform.claude.com/docs/en/about-claude/models/overview
    {"id": "claude-fable-5", "provider": "anthropic", "label": "Claude Fable 5"},
    {"id": "claude-opus-5-5", "provider": "anthropic", "label": "Claude Opus 5.5"},
    {"id": "claude-opus-5.5", "provider": "anthropic", "label": "Claude Opus 5.5"},
    {"id": "claude-opus-5", "provider": "anthropic", "label": "Claude Opus 5"},
    {"id": "claude-sonnet-5", "provider": "anthropic", "label": "Claude Sonnet 5"},
    {"id": "claude-opus-4-8", "provider": "anthropic", "label": "Claude Opus 4.8"},
    {"id": "claude-opus-4-7", "provider": "anthropic", "label": "Claude Opus 4.7"},
    {"id": "claude-opus-4-6", "provider": "anthropic", "label": "Claude Opus 4.6"},
    {"id": "claude-sonnet-4-6", "provider": "anthropic", "label": "Claude Sonnet 4.6"},
    {"id": "claude-haiku-4-5", "provider": "anthropic", "label": "Claude Haiku 4.5"},
    {"id": "claude-opus-4-5", "provider": "anthropic", "label": "Claude Opus 4.5"},
    {"id": "claude-sonnet-4-5", "provider": "anthropic", "label": "Claude Sonnet 4.5"},
    # xAI Grok — https://docs.x.ai/developers/models
    {"id": "grok-4.6", "provider": "xai", "label": "Grok 4.6"},
    {"id": "grok-4.5", "provider": "xai", "label": "Grok 4.5"},
    {"id": "grok-4.3", "provider": "xai", "label": "Grok 4.3"},
    {"id": "grok-4.20-0309-reasoning", "provider": "xai", "label": "Grok 4.20 Reasoning"},
    {"id": "grok-4.20-0309-non-reasoning", "provider": "xai", "label": "Grok 4.20"},
    {"id": "grok-4.20-multi-agent-0309", "provider": "xai", "label": "Grok 4.20 Multi-Agent"},
    {"id": "grok-build-0.1", "provider": "xai", "label": "Grok Build 0.1"},
    # OpenAI — https://developers.openai.com/api/docs/models
    {"id": "gpt-5.6-sol", "provider": "openai", "label": "GPT-5.6 Sol"},
    {"id": "gpt-5.6-terra", "provider": "openai", "label": "GPT-5.6 Terra"},
    {"id": "gpt-5.6-luna", "provider": "openai", "label": "GPT-5.6 Luna"},
    {"id": "gpt-5.5", "provider": "openai", "label": "GPT-5.5"},
    {"id": "gpt-5.4", "provider": "openai", "label": "GPT-5.4"},
    {"id": "gpt-5.4-nano", "provider": "openai", "label": "GPT-5.4 nano"},
    {"id": "gpt-5.2", "provider": "openai", "label": "GPT-5.2"},
    {"id": "gpt-5.1", "provider": "openai", "label": "GPT-5.1"},
    {"id": "gpt-5", "provider": "openai", "label": "GPT-5"},
    {"id": "gpt-5-mini", "provider": "openai", "label": "GPT-5 mini"},
    {"id": "gpt-5-nano", "provider": "openai", "label": "GPT-5 nano"},
    {"id": "gpt-4.1", "provider": "openai", "label": "GPT-4.1"},
    {"id": "gpt-4.1-mini", "provider": "openai", "label": "GPT-4.1 mini"},
    {"id": "gpt-4.1-nano", "provider": "openai", "label": "GPT-4.1 nano"},
    # Google Gemini — https://ai.google.dev/gemini-api/docs/models
    {"id": "gemini-3.7-flash", "provider": "gemini", "label": "Gemini 3.7 Flash"},
    {"id": "gemini-3.6-flash", "provider": "gemini", "label": "Gemini 3.6 Flash"},
    {"id": "gemini-3.5-flash", "provider": "gemini", "label": "Gemini 3.5 Flash"},
    {"id": "gemini-3.5-flash-lite", "provider": "gemini", "label": "Gemini 3.5 Flash-Lite"},
    {"id": "gemini-3.1-pro-preview", "provider": "gemini", "label": "Gemini 3.1 Pro"},
    {"id": "gemini-3.1-flash-lite", "provider": "gemini", "label": "Gemini 3.1 Flash-Lite"},
    {"id": "gemini-3-flash-preview", "provider": "gemini", "label": "Gemini 3 Flash"},
]

# Retired / new-user-blocked IDs → current replacements so old threads still run.
MODEL_ALIASES: dict[str, str] = {
    "claude-opus-5.5": "claude-opus-5-5",
    "claude-opus-5.5-medium": "claude-opus-5-5",
    "claude-opus-5-5-medium": "claude-opus-5-5",
    "gemini-2.5-pro": "gemini-3.1-pro-preview",
    "gemini-2.5-flash": "gemini-3.6-flash",
    "gemini-2.5-flash-lite": "gemini-3.1-flash-lite",
}

PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "xai": "XAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
}

# Chat LLMs that /all can run in parallel (not Perplexity research).
COMPARE_PROVIDERS = ("anthropic", "openai", "xai", "gemini")


def _key(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def provider_status() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for provider, env_name in PROVIDER_ENV.items():
        configured = bool(_key(env_name))
        out[provider] = {
            "configured": configured,
            "env": env_name,
            "label": {
                "anthropic": "Claude",
                "xai": "Grok",
                "openai": "OpenAI",
                "gemini": "Gemini",
                "perplexity": "Perplexity",
            }.get(provider, provider),
        }
    return out


def list_models() -> list[dict[str, Any]]:
    status = provider_status()
    out = []
    for m in MODELS:
        enabled = bool(status.get(m["provider"], {}).get("configured"))
        out.append({**m, "enabled": enabled})
    return out


def canonicalize_model_id(model_id: str | None) -> str:
    mid = (model_id or "").strip()
    if not mid:
        return ""
    return MODEL_ALIASES.get(mid, mid)


def normalize_provider_models(raw: Any) -> dict[str, str]:
    """Map provider → known model id. Unknown IDs are dropped."""
    by_id = {m["id"]: m for m in MODELS}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for _key, val in raw.items():
        mid = canonicalize_model_id(str(val or ""))
        m = by_id.get(mid)
        if not m:
            continue
        out[m["provider"]] = m["id"]
    return out


def prefs_for_compare(
    *,
    model_id: str | None = None,
    provider_models: dict | None = None,
    thread: dict | None = None,
    saved_models: dict | None = None,
) -> tuple[str | None, dict[str, str]]:
    """Merge saved / thread / request model picks for /all and follow-ups."""
    by_id = {m["id"]: m for m in MODELS}
    by_p = normalize_provider_models(saved_models)
    if thread:
        by_p.update(normalize_provider_models(thread.get("providerModels")))
        mid = canonicalize_model_id(thread.get("model"))
        m = by_id.get(mid) if mid else None
        if m:
            by_p[m["provider"]] = m["id"]
    by_p.update(normalize_provider_models(provider_models))
    prefer = canonicalize_model_id(model_id)
    if prefer:
        m = by_id.get(prefer)
        if m:
            by_p[m["provider"]] = m["id"]
    elif thread:
        prefer = canonicalize_model_id(thread.get("model"))
    return prefer or None, by_p


def resolve_model(model_id: str | None) -> dict[str, Any]:
    """Return model dict or raise ValueError."""
    models = {m["id"]: m for m in MODELS}
    model_id = canonicalize_model_id(model_id)
    if model_id and model_id in models:
        m = models[model_id]
    else:
        # Prefer Claude Sonnet 5, else first enabled
        enabled = [m for m in list_models() if m["enabled"]]
        if not enabled:
            raise ValueError(
                "No AI providers configured. Set ANTHROPIC_API_KEY, XAI_API_KEY, "
                "OPENAI_API_KEY, and/or GEMINI_API_KEY on the server."
            )
        prefer = next((m for m in enabled if m["id"] == "claude-sonnet-5"), None)
        m = prefer or enabled[0]
    status = provider_status()
    if not status.get(m["provider"], {}).get("configured"):
        raise ValueError(
            f"Provider '{m['provider']}' is not configured "
            f"(missing {PROVIDER_ENV.get(m['provider'])})."
        )
    return {**m, "enabled": True}


def get_api_key(provider: str) -> str:
    env = PROVIDER_ENV.get(provider)
    if not env:
        raise ValueError(f"Unknown provider: {provider}")
    key = _key(env)
    if not key:
        raise ValueError(f"Missing {env}")
    return key


def compare_models(
    prefer_id: str | None = None,
    prefer_by_provider: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One enabled model per configured chat provider, for /all.

    Defaults to the first listed model per provider. prefer_by_provider is the
    last model chosen inside each provider (dropdown / saved prefs). prefer_id
    (the model currently selected at the top) overrides that provider only.
    """
    status = provider_status()
    by_id = {m["id"]: m for m in MODELS}

    def _known(mid: str | None) -> dict[str, str] | None:
        cid = canonicalize_model_id(mid)
        m = by_id.get(cid) if cid else None
        if not m:
            return None
        if not status.get(m["provider"], {}).get("configured"):
            return None
        return m

    by_provider: dict[str, dict[str, str]] = {}
    for mid in (prefer_by_provider or {}).values():
        preferred = _known(mid)
        if preferred:
            by_provider[preferred["provider"]] = preferred
    selected = _known(prefer_id)
    if selected:
        by_provider[selected["provider"]] = selected

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in MODELS:
        provider = m["provider"]
        if provider not in COMPARE_PROVIDERS or provider in seen:
            continue
        if not status.get(provider, {}).get("configured"):
            continue
        seen.add(provider)
        pick = by_provider.get(provider) or m
        out.append({**pick, "enabled": True})
    return out


def resolve_models(model_ids: list[str] | None) -> list[dict[str, Any]]:
    """Resolve an explicit list of model ids for /multiple (same provider OK).

    Duplicates are dropped. Unknown or unconfigured ids are skipped. Order
    follows the request.
    """
    status = provider_status()
    by_id = {m["id"]: m for m in MODELS}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in model_ids or []:
        mid = canonicalize_model_id(str(raw or "").strip())
        if not mid or mid in seen:
            continue
        m = by_id.get(mid)
        if not m:
            continue
        if m["provider"] not in COMPARE_PROVIDERS:
            continue
        if not status.get(m["provider"], {}).get("configured"):
            continue
        seen.add(mid)
        out.append({**m, "enabled": True})
    return out


def parse_model_list_prefix(text: str) -> tuple[list[str], str]:
    """Split leading model ids from a /multiple argument.

    Accepts:
      claude-sonnet-5,gpt-5.4 write hooks
      claude-sonnet-5 gpt-5.4 — write hooks
    Returns (model_ids, remaining_prompt).
    """
    s = (text or "").strip()
    if not s:
        return [], ""
    by_id = {m["id"]: m for m in MODELS}
    # First token may be a comma-separated id list.
    first, _, rest = s.partition(" ")
    ids: list[str] = []
    if "," in first:
        for part in first.split(","):
            mid = canonicalize_model_id(part.strip())
            if mid and mid in by_id:
                ids.append(mid)
            elif part.strip():
                # Not a model list — treat whole string as prompt.
                return [], s
        return ids, rest.strip()
    # Space-separated ids until a non-id token (or em dash).
    tokens = s.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip().rstrip(",;")
        if tok in ("—", "-", "--"):
            i += 1
            break
        mid = canonicalize_model_id(tok)
        if mid and mid in by_id:
            ids.append(mid)
            i += 1
            continue
        break
    if not ids:
        return [], s
    return ids, " ".join(tokens[i:]).strip()
