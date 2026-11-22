"""Per-user Create/Chat workspace (captions, style, batch options).

Sources stay in session.json; this file holds everything that used to live
only in the browser so the chat agent and Create page share one job.
"""

from __future__ import annotations

import uuid
from typing import Any

from . import userstore
from .config import CAPTION_STYLE, DEFAULT_NAME_TEMPLATE

_DEFAULT_STYLE_KEYS = (
    "fontWeight",
    "strokeRatio",
    "fontSizeRatio",
    "maxWidthRatio",
    "yRatio",
    "lineHeight",
    "maxLines",
    "captionMode",
    "boxPadRatio",
    "boxCornerRatio",
)

VALID_MODES = ("matrix", "pair", "custom")


def _default_style() -> dict:
    out = {k: CAPTION_STYLE[k] for k in _DEFAULT_STYLE_KEYS if k in CAPTION_STYLE}
    out.setdefault("maxLines", 6)
    out.setdefault("captionMode", "outline")
    return out


def empty_workspace() -> dict:
    return {
        "captions": "",
        "preserveBreaks": True,
        "forceLower": False,
        "style": _default_style(),
        "mode": "matrix",
        "nameTemplate": DEFAULT_NAME_TEMPLATE,
        "skipExisting": True,
        "dedupe": True,
        "endCaptionTemplate": None,
        "strokePreset": "thick",
        "positionPreset": "center",
        "renderPairs": [],
        "captionMeta": [],
        "offerId": "",
        "chatProvider": "",
        "chatModels": {},
        "activePlanId": "",
        "createMode": "video",
        "carouselCaptionsByImage": {},
        "carouselCaptions": "",
    }


def _clean_caption_meta(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        sources = item.get("sources") or []
        if not isinstance(sources, list):
            sources = [sources] if sources else []
        row: dict[str, Any] = {
            "text": text,
            "sources": [str(s).strip() for s in sources if str(s).strip()],
        }
        if item.get("origin"):
            row["origin"] = str(item.get("origin") or "").strip()
        if item.get("model"):
            row["model"] = str(item.get("model") or "").strip()
        out.append(row)
    return out


def _clean_slides(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        sid = str(item or "").strip()
        if sid:
            out.append(sid)
        if len(out) >= 5:
            break
    return out


def _clean_render_pairs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cap = str(item.get("caption") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        slides = _clean_slides(item.get("slides"))
        if kind == "carousel" or slides:
            if not cap or not slides:
                continue
            cid = str(item.get("carouselId") or item.get("sourceId") or "").strip()
            if not cid:
                cid = uuid.uuid4().hex[:10]
            row: dict[str, Any] = {
                "kind": "carousel",
                "carouselId": cid,
                "sourceId": cid,
                "slides": slides,
                "caption": cap,
                "sourceName": str(item.get("sourceName") or f"{len(slides)}-slide carousel"),
            }
            _copy_pair_meta(item, row)
            out.append(row)
            continue
        sid = str(item.get("sourceId") or "").strip()
        if not sid or not cap:
            continue
        row = {"sourceId": sid, "caption": cap}
        if item.get("sourceName"):
            row["sourceName"] = str(item.get("sourceName") or "")
        _copy_pair_meta(item, row)
        out.append(row)
    return out


def _copy_pair_meta(item: dict, row: dict[str, Any]) -> None:
    if item.get("pinUrl"):
        row["pinUrl"] = str(item.get("pinUrl") or "")
    if item.get("model"):
        row["model"] = str(item.get("model") or "")
    if item.get("origin"):
        row["origin"] = str(item.get("origin") or "")
    sources = item.get("sources")
    if isinstance(sources, list) and sources:
        row["sources"] = [str(s).strip() for s in sources if str(s).strip()]
    if item.get("tiktokStatus"):
        row["tiktokStatus"] = str(item.get("tiktokStatus") or "")
    if item.get("tiktokAccountName"):
        row["tiktokAccountName"] = str(item.get("tiktokAccountName") or "")
    if item.get("tiktokUploadedAt"):
        row["tiktokUploadedAt"] = str(item.get("tiktokUploadedAt") or "")
    if item.get("outputRel"):
        row["outputRel"] = str(item.get("outputRel") or "")
    if item.get("sparkCode"):
        code = str(item.get("sparkCode") or "").strip()
        if code.lower() != "fail":
            row["sparkCode"] = code
    if item.get("sparkStatus"):
        row["sparkStatus"] = str(item.get("sparkStatus") or "")
    if item.get("sparkError"):
        row["sparkError"] = str(item.get("sparkError") or "")
    if item.get("sparkFetchedAt"):
        row["sparkFetchedAt"] = str(item.get("sparkFetchedAt") or "")
    if item.get("tiktokPostUrl"):
        row["tiktokPostUrl"] = str(item.get("tiktokPostUrl") or "")
    if item.get("sparkGenerated") and str(row.get("sparkCode") or "").strip():
        row["sparkGenerated"] = True


def _clean_carousel_captions(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in raw.items():
        kid = str(key or "").strip()
        if not kid:
            continue
        text = str(val or "")
        if len(text) > 50_000:
            text = text[:50_000]
        out[kid] = text
        if len(out) >= 200:
            break
    return out


def _normalize(data: dict | None) -> dict:
    base = empty_workspace()
    if not isinstance(data, dict):
        return base
    out = {**base, **{k: data[k] for k in base if k in data}}
    if isinstance(data.get("style"), dict):
        out["style"] = {**_default_style(), **data["style"]}
    if data.get("endCaptionTemplate") is not None:
        out["endCaptionTemplate"] = data.get("endCaptionTemplate")
    out["renderPairs"] = _clean_render_pairs(data.get("renderPairs") or out.get("renderPairs"))
    out["captionMeta"] = _clean_caption_meta(
        data.get("captionMeta") if "captionMeta" in (data or {}) else out.get("captionMeta")
    )
    out["carouselCaptionsByImage"] = _clean_carousel_captions(
        data.get("carouselCaptionsByImage")
        if "carouselCaptionsByImage" in (data or {})
        else out.get("carouselCaptionsByImage")
    )
    if not isinstance(out.get("carouselCaptions"), str):
        out["carouselCaptions"] = ""
    else:
        out["carouselCaptions"] = out["carouselCaptions"][:200_000]
    if out.get("createMode") not in ("video", "carousel"):
        out["createMode"] = "video"
    if out.get("mode") not in VALID_MODES:
        out["mode"] = "matrix"
    if not isinstance(out.get("chatProvider"), str):
        out["chatProvider"] = ""
    models = data.get("chatModels") if isinstance(data.get("chatModels"), dict) else out.get("chatModels")
    out["chatModels"] = {
        str(k): str(v)
        for k, v in (models or {}).items()
        if str(k).strip() and str(v).strip()
    }
    return out


def load_workspace(uid: str) -> dict:
    return _normalize(userstore.get_kv(uid, "workspace"))


def save_workspace(uid: str, patch: dict[str, Any] | None) -> dict:
    if not patch:
        return load_workspace(uid)

    def _apply(current: dict) -> dict:
        current = _normalize(current)
        for key in (
            "captions",
            "preserveBreaks",
            "forceLower",
            "mode",
            "nameTemplate",
            "skipExisting",
            "dedupe",
            "strokePreset",
            "positionPreset",
            "offerId",
            "activePlanId",
            "createMode",
            "carouselCaptions",
        ):
            if key in patch:
                current[key] = patch[key]
        if "style" in patch and isinstance(patch["style"], dict):
            current["style"] = {**_default_style(), **patch["style"]}
        if "endCaptionTemplate" in patch:
            current["endCaptionTemplate"] = patch["endCaptionTemplate"]
        if "renderPairs" in patch:
            current["renderPairs"] = _clean_render_pairs(patch["renderPairs"])
        if "captionMeta" in patch:
            current["captionMeta"] = _clean_caption_meta(patch["captionMeta"])
        if "carouselCaptionsByImage" in patch:
            current["carouselCaptionsByImage"] = _clean_carousel_captions(
                patch.get("carouselCaptionsByImage")
            )
        if "chatProvider" in patch:
            current["chatProvider"] = str(patch.get("chatProvider") or "").strip()
        if "chatModels" in patch and isinstance(patch["chatModels"], dict):
            current_models = (
                current.get("chatModels")
                if isinstance(current.get("chatModels"), dict)
                else {}
            )
            incoming = {
                str(k).strip(): str(v).strip()
                for k, v in patch["chatModels"].items()
                if str(k).strip() and str(v).strip()
            }
            current["chatModels"] = {**current_models, **incoming}
        if isinstance(current.get("captions"), str) and len(current["captions"]) > 200_000:
            current["captions"] = current["captions"][:200_000]
        if current.get("mode") not in VALID_MODES:
            current["mode"] = "matrix"
        if current.get("createMode") not in ("video", "carousel"):
            current["createMode"] = "video"
        return current

    return userstore.patch_kv(uid, "workspace", _apply)


def caption_list(workspace: dict) -> list[str]:
    raw = workspace.get("captions") or ""
    force_lower = bool(workspace.get("forceLower"))
    preserve = bool(workspace.get("preserveBreaks"))
    lines: list[str] = []
    for line in str(raw).splitlines():
        text = line if preserve else " ".join(line.split())
        text = text.strip()
        if not text:
            continue
        if force_lower:
            text = text.lower()
        lines.append(text)
    return lines


def unique_captions_from_pairs(pairs: list[dict]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for p in pairs:
        cap = str((p or {}).get("caption") or "").strip()
        if not cap or cap in seen:
            continue
        seen.add(cap)
        lines.append(cap)
    return "\n".join(lines)
