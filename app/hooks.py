"""Affiliate hook generator: offers, winners, likes, caption bank, Opus generation.

Per-user state lives in hooks_offers.json and hooks_history.json under the
user root (R2 write-through via app.storage). Every plan/chat hook is stored
on the active offer so Opus can match that offer's TikTok voice. Generation
runs Opus through the Claude CLI subscription, not the Anthropic API.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import requests

from . import storage
from . import userstore
from .caption_markers import HOOK_CREATIVE_RULES, MARKER_RULES
from .config import user_hooks_history_path, user_hooks_offers_path
from .random_word import pick_random_words, random_word_brief

_log = logging.getLogger(__name__)
_lock = threading.Lock()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL") or "").strip() or "gpt-4.1-mini"
CAPTION_BANK_CAP = 120

ALLOWED_COUNTS = frozenset({2, 5, 10, 20})
ALLOWED_MODES = frozenset({"variations", "mix", "new_angles"})
HISTORY_MODES = ALLOWED_MODES | frozenset({"chat"})
HISTORY_CAP = 200
MAX_WINNERS = 200
MAX_LIKED = 200
MAX_PAGE_CHARS = 6000
MAX_HOOK_LEN = 280
MAX_NOTES_LEN = 2000
MAX_NAME_LEN = 120
MAX_URL_LEN = 2000
MAX_EXTRA_LEN = 1000
ALLOWED_PAYOUT = frozenset({"flat", "cpa", "rev_share", "unknown"})


class HooksError(Exception):
    """User-facing hooks error."""


# ---------------------------------------------------------------------------
# HTML → plain text (for offer URL fetch)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def _strip_html(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def fetch_offer_page_text(url: str) -> str:
    """Fetch offer URL and return truncated plain text. Empty on failure."""
    if not url or not url.strip():
        return ""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    try:
        resp = requests.get(
            url.strip(),
            timeout=12,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; CaptionToolHooks/1.0; +https://localhost)"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return ""
        text = _strip_html(resp.text or "")
        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS] + "…"
        return text
    except Exception as exc:
        _log.info("Offer page fetch failed for %s: %s", url, exc)
        return ""


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def _empty_offers() -> dict:
    return {"offers": []}


def _empty_history() -> dict:
    return {"runs": []}


def _load_offers(uid: str) -> dict:
    """Load offers from Firestore docs, repairing empty migrations from local JSON."""
    migrated = userstore.get_kv(uid, "_migrated_offers")
    if migrated and migrated.get("done"):
        docs = userstore.list_docs(uid, "offers")
        offers = [d for d in docs if isinstance(d, dict) and d.get("id")]
        if offers:
            return {"offers": offers}
        # Migration flagged done but collection empty — re-seed from local file.
    data = storage.load_json(uid, user_hooks_offers_path(uid), skip_firestore=True)
    if not isinstance(data, dict):
        data = _empty_offers()
    if not isinstance(data.get("offers"), list):
        data["offers"] = []
    for offer in data["offers"]:
        if isinstance(offer, dict) and offer.get("id"):
            userstore.set_doc(uid, "offers", offer["id"], offer)
    userstore.set_kv(uid, "_migrated_offers", {"done": True})
    # Prefer the freshly written docs (Firestore) when available.
    docs = userstore.list_docs(uid, "offers")
    written = [d for d in docs if isinstance(d, dict) and d.get("id")]
    if written:
        return {"offers": written}
    return data


def sync_offers_to_firestore(uid: str) -> dict:
    """Force-push local hooks_offers.json (+ any docs) into Firestore offers collection."""
    with _lock:
        data = storage.load_json(uid, user_hooks_offers_path(uid), skip_firestore=True)
        if not isinstance(data, dict):
            data = _empty_offers()
        if not isinstance(data.get("offers"), list):
            data["offers"] = []
        # Merge any docs already in store that aren't in the file.
        existing = {
            str(d.get("id")): d
            for d in userstore.list_docs(uid, "offers")
            if isinstance(d, dict) and d.get("id")
        }
        by_id: dict[str, dict] = dict(existing)
        for offer in data["offers"]:
            if isinstance(offer, dict) and offer.get("id"):
                by_id[str(offer["id"])] = {**existing.get(str(offer["id"]), {}), **offer}
        merged = {"offers": list(by_id.values())}
        _save_offers(uid, merged)
        return {
            "ok": True,
            "count": len(merged["offers"]),
            "ids": [str(o.get("id")) for o in merged["offers"]],
        }


def list_offers(uid: str) -> list[dict]:
    with _lock:
        data = _load_offers(uid)
        return [_public_offer(o) for o in data["offers"]]


def _save_offers(uid: str, data: dict) -> None:
    offers = [o for o in (data.get("offers") or []) if isinstance(o, dict) and o.get("id")]
    existing = {d.get("id") for d in userstore.list_docs(uid, "offers")}
    keep = {o["id"] for o in offers}
    for oid in existing - keep:
        if oid:
            userstore.delete_doc(uid, "offers", str(oid))
    for offer in offers:
        userstore.set_doc(uid, "offers", offer["id"], offer)
    userstore.set_kv(uid, "_migrated_offers", {"done": True})


def _load_history(uid: str) -> dict:
    migrated = userstore.get_kv(uid, "_migrated_hook_runs")
    if migrated and migrated.get("done"):
        docs = userstore.list_docs(uid, "hookRuns")
        return {"runs": [d for d in docs if isinstance(d, dict) and d.get("id")]}
    data = storage.load_json(uid, user_hooks_history_path(uid), skip_firestore=True)
    if not isinstance(data, dict):
        data = _empty_history()
    if not isinstance(data.get("runs"), list):
        data["runs"] = []
    for run in data["runs"]:
        if isinstance(run, dict) and run.get("id"):
            userstore.set_doc(uid, "hookRuns", run["id"], run)
    userstore.set_kv(uid, "_migrated_hook_runs", {"done": True})
    return data


def _save_history(uid: str, data: dict) -> None:
    runs = [r for r in (data.get("runs") or []) if isinstance(r, dict) and r.get("id")]
    if len(runs) > HISTORY_CAP:
        runs = sorted(runs, key=lambda r: r.get("createdAt") or 0)[-HISTORY_CAP:]
        data["runs"] = runs
    existing = {d.get("id") for d in userstore.list_docs(uid, "hookRuns")}
    keep = {r["id"] for r in runs}
    for rid in existing - keep:
        if rid:
            userstore.delete_doc(uid, "hookRuns", str(rid))
    for run in runs:
        userstore.set_doc(uid, "hookRuns", run["id"], run)
    userstore.set_kv(uid, "_migrated_hook_runs", {"done": True})


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    if not cleaned:
        raise HooksError("Offer name is required")
    if len(cleaned) > MAX_NAME_LEN:
        cleaned = cleaned[:MAX_NAME_LEN]
    return cleaned


def _normalize_url(url: str | None) -> str:
    cleaned = (url or "").strip()
    if len(cleaned) > MAX_URL_LEN:
        cleaned = cleaned[:MAX_URL_LEN]
    if cleaned:
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HooksError("Offer URL must be http(s)")
    return cleaned


def _normalize_notes(notes: str | None) -> str:
    cleaned = (notes or "").strip()
    if len(cleaned) > MAX_NOTES_LEN:
        cleaned = cleaned[:MAX_NOTES_LEN]
    return cleaned


def _normalize_payout(
    payout_type: str | None,
    payout_amount: float | str | None,
    payout_share_percent: float | str | None,
    payout_note: str | None,
    currency: str | None,
) -> dict:
    kind = (payout_type or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    if kind in ("revshare", "share", "percent"):
        kind = "rev_share"
    if kind not in ALLOWED_PAYOUT:
        kind = "unknown"
    amount = ""
    if payout_amount not in (None, ""):
        try:
            amount = round(float(payout_amount), 4)
        except (TypeError, ValueError):
            amount = ""
    share = ""
    if payout_share_percent not in (None, ""):
        try:
            share = max(0.0, min(100.0, float(payout_share_percent)))
        except (TypeError, ValueError):
            share = ""
    note = (payout_note or "").strip()[:500]
    cur = (currency or "USD").strip().upper()[:8] or "USD"
    return {
        "payoutType": kind,
        "payoutAmount": amount,
        "payoutSharePercent": share,
        "payoutNote": note,
        "currency": cur,
    }


def _normalize_hook_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        raise HooksError("Hook text is required")
    if len(cleaned) > MAX_HOOK_LEN:
        cleaned = cleaned[:MAX_HOOK_LEN]
    return cleaned


def _public_hook(item: dict) -> dict:
    return {
        "id": item["id"],
        "text": item["text"],
        "createdAt": item.get("createdAt"),
    }


def _public_offer(offer: dict) -> dict:
    winners = offer.get("winners") or []
    liked = offer.get("liked") or []
    return {
        "id": offer["id"],
        "name": offer["name"],
        "url": offer.get("url") or "",
        "notes": offer.get("notes") or "",
        "createdAt": offer.get("createdAt"),
        "updatedAt": offer.get("updatedAt"),
        "winners": [_public_hook(w) for w in winners if isinstance(w, dict)],
        "liked": [_public_hook(w) for w in liked if isinstance(w, dict)],
        "winnerCount": len(winners),
        "likedCount": len(liked),
        "payoutType": offer.get("payoutType") or "unknown",
        "payoutAmount": offer.get("payoutAmount") if offer.get("payoutAmount") not in (None, "") else "",
        "payoutSharePercent": offer.get("payoutSharePercent") if offer.get("payoutSharePercent") not in (None, "") else "",
        "payoutNote": offer.get("payoutNote") or "",
        "currency": offer.get("currency") or "USD",
        "landers": list(offer.get("landers") or []),
        "networks": list(offer.get("networks") or []),
        "activeLanderId": offer.get("activeLanderId") or "",
        "activeNetworkId": offer.get("activeNetworkId") or "",
        "routingStatus": offer.get("routingStatus") or "",
    }


def _find_offer(data: dict, offer_id: str) -> dict | None:
    for offer in data["offers"]:
        if offer["id"] == offer_id:
            return offer
    return None


# ---------------------------------------------------------------------------
# Offer CRUD
# ---------------------------------------------------------------------------

def get_offer(uid: str, offer_id: str) -> dict:
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        pub = _public_offer(offer)
    bank = list_caption_bank(uid, offer_id)
    pub["pastCaptions"] = bank
    pub["pastCaptionCount"] = len(bank)
    return pub


def offer_snapshot(uid: str, offer_id: str | None) -> dict:
    """Payout + name copied onto a render so history stays accurate."""
    if not (offer_id or "").strip():
        return {}
    try:
        o = get_offer(uid, offer_id.strip())
    except HooksError:
        return {"offer_id": offer_id.strip()}
    return {
        "offer_id": o["id"],
        "offer_name": o.get("name") or "",
        "payout_type": o.get("payoutType") or "unknown",
        "payout_amount": o.get("payoutAmount") if o.get("payoutAmount") not in (None, "") else "",
        "payout_share_percent": o.get("payoutSharePercent")
        if o.get("payoutSharePercent") not in (None, "")
        else "",
        "payout_currency": o.get("currency") or "USD",
        "payout_note": o.get("payoutNote") or "",
    }


def create_offer(
    uid: str,
    name: str,
    url: str | None = None,
    notes: str | None = None,
    payout_type: str | None = None,
    payout_amount: float | str | None = None,
    payout_share_percent: float | str | None = None,
    payout_note: str | None = None,
    currency: str | None = None,
) -> dict:
    with _lock:
        data = _load_offers(uid)
        now = time.time()
        pay = _normalize_payout(
            payout_type, payout_amount, payout_share_percent, payout_note, currency
        )
        offer = {
            "id": uuid.uuid4().hex[:12],
            "name": _normalize_name(name),
            "url": _normalize_url(url),
            "notes": _normalize_notes(notes),
            "winners": [],
            "liked": [],
            "createdAt": now,
            "updatedAt": now,
            **pay,
        }
        data["offers"].append(offer)
        _save_offers(uid, data)
        return _public_offer(offer)


def update_offer(
    uid: str,
    offer_id: str,
    name: str | None = None,
    url: str | None = None,
    notes: str | None = None,
    payout_type: str | None = None,
    payout_amount: float | str | None = None,
    payout_share_percent: float | str | None = None,
    payout_note: str | None = None,
    currency: str | None = None,
) -> dict:
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        if name is not None:
            offer["name"] = _normalize_name(name)
        if url is not None:
            offer["url"] = _normalize_url(url)
        if notes is not None:
            offer["notes"] = _normalize_notes(notes)
        if any(
            v is not None
            for v in (payout_type, payout_amount, payout_share_percent, payout_note, currency)
        ):
            pay = _normalize_payout(
                payout_type if payout_type is not None else offer.get("payoutType"),
                payout_amount if payout_amount is not None else offer.get("payoutAmount"),
                payout_share_percent
                if payout_share_percent is not None
                else offer.get("payoutSharePercent"),
                payout_note if payout_note is not None else offer.get("payoutNote"),
                currency if currency is not None else offer.get("currency"),
            )
            offer.update(pay)
        offer["updatedAt"] = time.time()
        _save_offers(uid, data)
        return _public_offer(offer)


def patch_offer(uid: str, offer_id: str, fields: dict) -> dict:
    """Update routing fields on an offer. Unknown keys are ignored."""
    allowed = {
        "landers",
        "networks",
        "activeLanderId",
        "activeNetworkId",
        "routingStatus",
    }
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        for key, value in (fields or {}).items():
            if key in allowed:
                offer[key] = value
        offer["updatedAt"] = time.time()
        _save_offers(uid, data)
        return _public_offer(offer)


def set_offer_routing(
    uid: str,
    offer_id: str,
    *,
    landers: list | None = None,
    networks: list | None = None,
    active_lander_id: str | None = None,
    active_network_id: str | None = None,
) -> dict:
    from .affiliate.routing import normalize_routing

    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        draft = dict(offer)
        if landers is not None:
            draft["landers"] = landers
        if networks is not None:
            draft["networks"] = networks
        if active_lander_id is not None:
            draft["activeLanderId"] = active_lander_id
        if active_network_id is not None:
            draft["activeNetworkId"] = active_network_id
        routing = normalize_routing(draft)
        offer.update(routing)
        offer["updatedAt"] = time.time()
        _save_offers(uid, data)
        return _public_offer(offer)


def delete_offer(uid: str, offer_id: str) -> None:
    with _lock:
        data = _load_offers(uid)
        before = len(data["offers"])
        data["offers"] = [o for o in data["offers"] if o["id"] != offer_id]
        if len(data["offers"]) == before:
            raise HooksError("Offer not found")
        _save_offers(uid, data)
        # Drop history for this offer
        hist = _load_history(uid)
        hist["runs"] = [r for r in hist["runs"] if r.get("offerId") != offer_id]
        _save_history(uid, hist)


# ---------------------------------------------------------------------------
# Winner hooks
# ---------------------------------------------------------------------------

def add_winner(uid: str, offer_id: str, text: str) -> dict:
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        winners = offer.setdefault("winners", [])
        if len(winners) >= MAX_WINNERS:
            raise HooksError(f"Max {MAX_WINNERS} winner hooks per offer")
        hook = {
            "id": uuid.uuid4().hex[:12],
            "text": _normalize_hook_text(text),
            "createdAt": time.time(),
        }
        winners.append(hook)
        offer["updatedAt"] = time.time()
        _save_offers(uid, data)
        return _public_hook(hook)


def update_winner(uid: str, offer_id: str, winner_id: str, text: str) -> dict:
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        for hook in offer.get("winners") or []:
            if hook["id"] == winner_id:
                hook["text"] = _normalize_hook_text(text)
                offer["updatedAt"] = time.time()
                _save_offers(uid, data)
                return _public_hook(hook)
        raise HooksError("Winner hook not found")


def delete_winner(uid: str, offer_id: str, winner_id: str) -> None:
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        winners = offer.get("winners") or []
        before = len(winners)
        offer["winners"] = [w for w in winners if w["id"] != winner_id]
        if len(offer["winners"]) == before:
            raise HooksError("Winner hook not found")
        offer["updatedAt"] = time.time()
        _save_offers(uid, data)


# ---------------------------------------------------------------------------
# Liked hooks (soft positive signal — not winners)
# ---------------------------------------------------------------------------

def add_like(uid: str, offer_id: str, text: str) -> dict:
    """Add a liked hook if not already present (by normalized text)."""
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        cleaned = _normalize_hook_text(text)
        liked = offer.setdefault("liked", [])
        for item in liked:
            if (item.get("text") or "").strip().lower() == cleaned.lower():
                return _public_hook(item)
        if len(liked) >= MAX_LIKED:
            # Drop oldest
            liked.pop(0)
        hook = {
            "id": uuid.uuid4().hex[:12],
            "text": cleaned,
            "createdAt": time.time(),
        }
        liked.append(hook)
        offer["updatedAt"] = time.time()
        _save_offers(uid, data)
        return _public_hook(hook)


def remove_like(uid: str, offer_id: str, like_id: str | None = None, text: str | None = None) -> None:
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        liked = offer.get("liked") or []
        if like_id:
            offer["liked"] = [x for x in liked if x["id"] != like_id]
        elif text:
            needle = _normalize_hook_text(text).lower()
            offer["liked"] = [
                x for x in liked if (x.get("text") or "").strip().lower() != needle
            ]
        else:
            raise HooksError("likeId or text is required")
        offer["updatedAt"] = time.time()
        _save_offers(uid, data)


def is_liked(offer: dict, text: str) -> bool:
    needle = (text or "").strip().lower()
    if not needle:
        return False
    for item in offer.get("liked") or []:
        if (item.get("text") or "").strip().lower() == needle:
            return True
    return False


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def list_history(uid: str, offer_id: str | None = None) -> list[dict]:
    with _lock:
        hist = _load_history(uid)
        runs = hist["runs"]
        if offer_id:
            runs = [r for r in runs if r.get("offerId") == offer_id]
        # Newest first
        runs = sorted(runs, key=lambda r: r.get("createdAt") or 0, reverse=True)
        out = []
        for run in runs:
            out.append({
                "id": run["id"],
                "offerId": run.get("offerId"),
                "mode": run.get("mode"),
                "count": run.get("count"),
                "extraInstructions": run.get("extraInstructions") or "",
                "createdAt": run.get("createdAt"),
                "hooks": list(run.get("hooks") or []),
            })
        return out


def _normalize_hook_list(hooks: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in hooks or []:
        s = re.sub(r"\s+", " ", str(item or "").strip().strip('"').strip("'"))
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:MAX_HOOK_LEN])
    return out


def _dedupe_caption_bank(
    items: list[tuple[float, str]], *, limit: int = CAPTION_BANK_CAP
) -> list[str]:
    """Newest first, unique by lowercase text."""
    seen: set[str] = set()
    out: list[str] = []
    for _ts, text in sorted(items, key=lambda row: row[0], reverse=True):
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned[:MAX_HOOK_LEN])
        if len(out) >= max(1, int(limit)):
            break
    return out


def list_caption_bank(
    uid: str, offer_id: str, *, limit: int = CAPTION_BANK_CAP
) -> list[str]:
    """All past on-screen lines for an offer: winners, likes, generation history."""
    oid = (offer_id or "").strip()
    if not oid:
        return []
    stamped: list[tuple[float, str]] = []
    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, oid)
        if not offer:
            raise HooksError("Offer not found")
        for row in list(offer.get("winners") or []) + list(offer.get("liked") or []):
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if text:
                stamped.append((float(row.get("createdAt") or 0), text))
    for run in list_history(uid, oid):
        ts = float(run.get("createdAt") or 0)
        for hook in run.get("hooks") or []:
            stamped.append((ts, str(hook)))
    return _dedupe_caption_bank(stamped, limit=limit)


def pin_active_offer(uid: str, offer_id: str) -> str:
    """Remember which offer new chat/plan captions belong to."""
    oid = (offer_id or "").strip()
    if not oid:
        return ""
    with _lock:
        data = _load_offers(uid)
        if not _find_offer(data, oid):
            raise HooksError("Offer not found")
    from . import workspace as ws

    ws.save_workspace(uid, {"offerId": oid})
    return oid


def set_active_offer(uid: str, offer_id: str) -> dict:
    pin_active_offer(uid, offer_id)
    return get_offer(uid, offer_id)


def resolve_offer_id(uid: str) -> str:
    from . import workspace as ws

    oid = (ws.load_workspace(uid).get("offerId") or "").strip()
    if oid:
        return oid
    offers = list_offers(uid)
    if len(offers) == 1:
        return str(offers[0].get("id") or "").strip()
    return ""


def record_generated_hooks(
    uid: str,
    hooks: list[str],
    *,
    offer_id: str = "",
    mode: str = "chat",
    extra: str = "",
    thread_id: str = "",
) -> dict | None:
    """Append hooks to generation history (chat plans, captions, approved ideas, etc.)."""
    cleaned = _normalize_hook_list(hooks)
    if not cleaned:
        return None
    mode = (mode or "chat").strip() or "chat"
    if mode not in HISTORY_MODES:
        mode = "chat"
    offer_id = (offer_id or resolve_offer_id(uid) or "").strip()
    extra = (extra or "").strip()
    if thread_id and not extra:
        extra = f"Chat · {thread_id[:8]}"

    with _lock:
        hist = _load_history(uid)
        runs = list(hist.get("runs") or [])
        if runs:
            last = max(runs, key=lambda r: r.get("createdAt") or 0)
            last_hooks = _normalize_hook_list(last.get("hooks") or [])
            if (
                last.get("offerId") == offer_id
                and last.get("mode") == mode
                and last_hooks == cleaned
                and (time.time() - float(last.get("createdAt") or 0)) < 120
            ):
                return None
        run = {
            "id": uuid.uuid4().hex[:12],
            "offerId": offer_id,
            "mode": mode,
            "count": len(cleaned),
            "extraInstructions": extra[:MAX_EXTRA_LEN],
            "createdAt": time.time(),
            "hooks": cleaned,
        }
        runs.append(run)
        if len(runs) > HISTORY_CAP:
            runs = sorted(runs, key=lambda r: r.get("createdAt") or 0)[-HISTORY_CAP:]
        hist["runs"] = runs
        _save_history(uid, hist)
    return run


def _append_history(
    uid: str,
    offer_id: str,
    mode: str,
    count: int,
    extra: str,
    hooks: list[str],
) -> dict:
    hist = _load_history(uid)
    run = {
        "id": uuid.uuid4().hex[:12],
        "offerId": offer_id,
        "mode": mode,
        "count": count,
        "extraInstructions": extra,
        "createdAt": time.time(),
        "hooks": hooks,
    }
    hist["runs"].append(run)
    # Cap — keep newest
    if len(hist["runs"]) > HISTORY_CAP:
        hist["runs"] = sorted(
            hist["runs"], key=lambda r: r.get("createdAt") or 0
        )[-HISTORY_CAP:]
    _save_history(uid, hist)
    return run


# ---------------------------------------------------------------------------
# Opus generation (OpenAI fallback)
# ---------------------------------------------------------------------------

def _require_api_key() -> str:
    key = OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY") or ""
    if not key.strip():
        raise HooksError(
            "OPENAI_API_KEY is not configured. Set it as an environment variable "
            "(or wrangler secret) to generate hooks."
        )
    return key.strip()


def _decision_gate(offer: dict) -> str:
    """Force a connect + decide verdict before a line can ship.

    An uncalibrated model will pass a line that only mentions the product.
    The jury has to say how the moment and the offer are the same fact,
    and what the viewer does next. Vague answers stay out.
    """
    reco = _reco_product_phrase(offer)
    if reco:
        goal = (
            "GOAL: create interest in paid fyp. Do not explain the real steps "
            "(no download, no signup, no offer). Do not invent fake steps either.\n"
            "The background clip is a vibe, not the instruction. You do not "
            "have to dance to turn paid fyp on. You do not have to run to turn "
            "it on. A line that says they danced or ran JUST TO turn it on is "
            "dead, because that is not how it works and it sounds like a requirement.\n"
            "A frame is one original picture that makes paid fyp interesting. "
            "Everybody running to turn it on is ONE frame. The dance once it "
            "is on is a DIFFERENT frame. Writing the run eight ways is a failure. "
            "Each line is a new frame that can sit on a clip we already have.\n"
            "People running because they want paid fyp fits a running clip. "
            "A dance clip is the mood once paid fyp is on. The dance is not "
            "the button.\n"
            "Use the clips we already have. A new idea does not need new footage. "
            "Same run, new line. Same dance, new line.\n"
            "No \"but me\", \"before me\", "
            "or \"skipped me\". One punch per hook. Never say tiktok. "
            "Do not end on a bare paid fyp.\n"
            "CRAZY GEN Z TIKTOK. Brainrot that flows as one spoken comment. "
            "A swear is allowed. Not a normal sentence. Not modest.\n"
            "THIS IS THE LEVEL. Do not copy. Do not shrink it.\n"
            "\"no bc it's 3am, my face is blue, and paid to scroll just paid me like I robbed somebody\"\n"
            "\"the light been green and I stayed on the phone. paid to scroll paid me more than that whole car\"\n"
            "\"cereal is soup. I would not put the fucking phone down. the scroll was printing\"\n"
            "\"I passed out mid scroll and woke up to this fan. paid to scroll worked while I was dead\"\n"
            "\"forty five minutes in the bathroom and paid to scroll paid the rent. I'm nasty for this\"\n"
            "\"grandma has not moved since sunrise and she out-earned the whole house. paid to scroll. I'm sick\"\n"
            "\"one percent battery and I kept going. paid to scroll was throwing money and I got greedy\"\n"
            "\"whole bus I never looked up. the scroll paid more than the driver. be so serious\"\n"
            "\"I showed them what one night of scrolling pays and they lunged like they were gonna snatch it\"\n"
            "\"my mom made me count it twice. she thought I stole it. all I did was scroll\"\n"
            "\"no bc i showed the whole house paid fyp paying rent\" is dead. Too small.\n"
            "\"grandma outdid the whole house\" is dead. What did she do? Say out-earned.\n"
            "THE CLIP CARRIES THE FEELING. Do not narrate the action on screen. "
            "Do not name the emotion. A fan of cash already feels arrogant. "
            "Never say arrogance, flex, fanning, or stack. The line is "
            "the thought that feeling makes true, and it still says the scroll "
            "paid.\n"
            "ONE PUNCH. Two hits are two hooks. Do not stack them. "
            "\"my mom made me count it twice. she thought I stole it. all I "
            "did was scroll\" splits into \"my mom made me count it twice. "
            "all I did was scroll\" and \"she thought I stole it. all I did "
            "was scroll\".\n"
            "WRITE AS CRAZY AS YOU WANT. Say the money. Keep money, cash, paid, "
            "pay, scroll, earn in the line. Do not swap them for lame synonyms. "
            "Do not soften the line and do not rewrite it to be compliant. Wrap "
            "ONLY the money phrase in [[uncompliant:income]]...[[/uncompliant]]. "
            "The tags are a warning for a later pass. Leave the words. Burn "
            "hides one letter (m*ney, c*sh, p*id, scr*ll) so a person still "
            "reads them.\n"
            "The viewer does not already know paid fyp. The line has to show it "
            "to them. \"Somebody said turn on paid fyp\" fails, because nobody "
            "learns what it is.\n"
            "Show them paid fyp, or show them how to turn it on, or show them "
            "the money it makes. The money is the reason they move. Keep that "
            "short. Do not write a paragraph about income.\n"
            "DRAG THEM IN. Half of this is an extreme reaction. Use a nagger: "
            "a short setup that yanks the next thought out of the reader before "
            "they can scroll. Shock, greed, a snatch, a freeze, somebody grabbing "
            "the phone. The reaction happens to the person reading. A caption "
            "that only reports the reaction is dead. Do not use a slur, and do "
            "not write a puzzle whose answer is a slur."
        )
        weak = (
            '"blue shirt man hit the whole dance just to turn on paid fyp"\n'
            '"the whole room lunged the second somebody said turn on paid fyp"\n'
            '"cab driver had paid fyp on gettin paid"\n'
            '"I showed the couch you get paid to play and they lunged"\n'
            '"fanning this fat stack cause paid fyp paid every scroll"\n'
            "Dead. The dance is not the button. The second line assumes they already know paid fyp. The third is just a person who had a setting. The fourth reports a reaction. The fifth narrates the clip. The picture already has the feeling. Do not name it."
        )
        strong = (
            '"wait. she read what paid fyp [[uncompliant:income]]pays[[/uncompliant]] and snatched the phone"\n'
            '"the whole room lunged the second I showed them paid fyp"\n'
            "The first one yanks. You feel the snatch. The second only works if the reader is already inside the moment. Prefer the yank.\n"
            '"this is the dance once paid fyp is on"\n'
            "Dance clip only. The dance is the mood, not the button.\n"
            "Each line is a different frame. Do not copy these."
        )
    else:
        goal = (
            "GOAL: a stranger learns what this offer does and why they would do "
            "the next step, from the on-screen line alone. Mentioning the name "
            "is not the goal."
        )
        weak = '"this app changed my life"\n"get paid to scroll"'
        strong = (
            "a moment they already know, crossed with what the offer actually "
            "does, so the next tap is obvious"
        )
    return (
        "DECISION GATE — say it like a TikTok comment. connect and decide stay "
        "off screen, and they cannot save a dead line.\n"
        "An idea is two things people already know, combined in a new way. "
        "The combination has to feel like a comment you'd stop on, not a sentence "
        "that explains a feature.\n"
        f"{goal}\n"
        "- connect: What two real things did this line cross? If one of them is "
        "a definition of the product, it does not connect.\n"
        "- decide: Quote the bit a person would actually feel. If the quote is "
        "\"was off\" or \"makes those minutes pay\", throw the line out.\n"
        "No pass for explaining the product. No pass for a setting with nobody in it.\n"
        f"WEAK:\n{weak}\n"
        f"STRONG:\n{strong}\n"
        "If the line is just a person who had paid fyp on, throw it out. Find the action.\n"
        'JSON: {"hooks":[{"text":"<on-screen line>","connect":"<one sentence>",'
        '"decide":"<one sentence>"}]}'
    )


def _reco_product_phrase(offer: dict) -> bool:
    blob = " ".join(
        [
            str(offer.get("name") or ""),
            str(offer.get("notes") or ""),
            str(offer.get("url") or ""),
        ]
    ).lower()
    return "reco" in blob or "rico" in blob or "paid fyp" in blob or "per minute" in blob


def _build_prompt(
    offer: dict,
    count: int,
    mode: str,
    extra: str,
    page_text: str,
    past_captions: list[str] | None = None,
    random_words: list[str] | None = None,
) -> tuple[str, str]:
    winners = [w.get("text") or "" for w in (offer.get("winners") or []) if w.get("text")]
    liked = [w.get("text") or "" for w in (offer.get("liked") or []) if w.get("text")]
    bank = [str(t).strip() for t in (past_captions or []) if str(t).strip()]

    system = (
        HOOK_CREATIVE_RULES
        + " You are Claude Opus writing TikTok affiliate on-screen hooks. "
        "Each on-screen hook is one punch. Long enough to say the moment and "
        "the money. If a second punch shows up, it is the next hook. "
        "No hashtags, no quotation marks around the whole hook. "
        + MARKER_RULES
        + " Do not rewrite the line to be compliant. Mark the risk and leave the words. "
        "Return ONLY valid JSON: "
        '{"hooks":[{"text":"...","connect":"...","decide":"..."}]} '
        "with exactly the requested count. text is the only on-screen line. "
        "connect and decide are the jury and never go on screen. "
        "CLIP/SEARCH stay out of JSON."
    )

    parts: list[str] = [
        f"Offer name: {offer.get('name') or ''}",
        f"Offer URL: {offer.get('url') or '(none)'}",
        f"Offer notes: {offer.get('notes') or '(none)'}",
        f"Generate exactly {count} hooks.",
    ]

    if bank:
        parts.append(
            "PAST CAPTIONS already made for this offer (voice bible — "
            "match energy, do not clone or slogan-ify):"
        )
        for i, t in enumerate(bank, 1):
            parts.append(f"  P{i}. {t}")
    else:
        parts.append(
            "No past captions in the bank yet. Be extra TikTok-ey. "
            "Do not invent a slogan."
        )

    if winners:
        parts.append("Proven WINNER hooks (they convert — same energy, new lines):")
        for i, t in enumerate(winners, 1):
            parts.append(f"  W{i}. {t}")

    if liked:
        parts.append("LIKED generations (good results — aim for this quality/style):")
        for i, t in enumerate(liked, 1):
            parts.append(f"  L{i}. {t}")

    if mode == "variations":
        parts.append(
            "MODE: VARIATIONS ONLY. Create close variations of the winner / past "
            "hooks — same angle and voice, different wording. Do not invent "
            "unrelated angles. Do not write a cleaner slogan of the same line."
        )
    elif mode == "mix":
        parts.append(
            "MODE: MIX. About half should be tight variations of winners/past; "
            "the other half should be fresh but still TikTok-ey for this offer."
        )
    else:  # new_angles
        parts.append(
            "MODE: NEW ANGLES. Invent completely new hooks for this offer. "
            "Do NOT clone or lightly rephrase the past captions. Stay TikTok-ey."
        )

    if page_text:
        parts.append("Offer page excerpt (for context):")
        parts.append(page_text[:MAX_PAGE_CHARS])

    if extra:
        parts.append(f"Extra instructions from the user: {extra}")

    parts.append(_decision_gate(offer))

    if random_words:
        parts.append(random_word_brief(list(random_words)))

    return system, "\n".join(parts)


def _usage_from_anthropic(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
    cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    if not cache_write:
        creation = getattr(u, "cache_creation", None)
        if creation is not None:
            cache_write = int(getattr(creation, "ephemeral_5m_input_tokens", 0) or 0)
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": cache_write,
    }


def _usage_from_openai(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
    details = getattr(u, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
    return {
        "input_tokens": int(getattr(u, "prompt_tokens", 0) or getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "completion_tokens", 0) or getattr(u, "output_tokens", 0) or 0),
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }


def _anthropic_hooks_chat(system: str, user: str) -> tuple[str, str, dict[str, int]]:
    """Opus hooks via the Claude CLI subscription. No Anthropic API call."""
    from .claude_cli import CLAUDE_CLI_MODEL, ClaudeCliError, complete

    try:
        text = complete(system, user)
    except ClaudeCliError as exc:
        raise HooksError(str(exc)) from exc
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    return text, CLAUDE_CLI_MODEL, usage


def _writer_chat(
    system: str, user: str, *, use_web_search: bool
) -> tuple[str, str, str, dict[str, int]]:
    """Opus on the Claude CLI subscription. No Anthropic or OpenAI fallback."""
    del use_web_search
    out, model, usage = _anthropic_hooks_chat(system, user)
    if not (out or "").strip():
        raise HooksError("claude CLI returned empty hook text")
    return out, "claude-cli", model, usage


def _jury_sentence(value: Any) -> str:
    """A real connect/decide sentence. 'yes' and 'it mentions paid fyp' fail."""
    s = re.sub(r"\s+", " ", str(value or "").strip())
    if len(s.split()) < 6:
        return ""
    if re.fullmatch(
        r"(yes|no|n/?a|none|it connects|it mentions( paid fyp)?|because paid fyp)\.?",
        s,
        flags=re.I,
    ):
        return ""
    return s


def _on_screen_line(value: Any) -> str:
    s = re.sub(r"\s+", " ", str(value or "").strip().strip('"').strip("'"))
    return s[:MAX_HOOK_LEN]


_EXPLAINER = re.compile(
    r"(is what makes|what it does|those minutes pay|the switch|makes those minutes|that's what paid fyp)",
    re.I,
)
def _word_count(line: str) -> int:
    bare = re.sub(r"\[\[/?uncompliant(?::[^\]]*)?\]\]", " ", line or "", flags=re.I)
    return len(re.findall(r"[a-z0-9']+", bare.lower()))


# "X had paid fyp on gettin paid" is a person, not a mechanism.
_PERSON_SETTING = re.compile(r"had paid fyp on", re.I)
_DANGLING = re.compile(r"(?:^|[.!?])\s*paid fyp\s*[.!?]*$", re.I)
_LEFT_OUT = re.compile(
    r"\b(but me|before me|skipped me|except me|everybody but me|nobody tapped)\b",
    re.I,
)
_RUN_FRAME = re.compile(
    r"\b(run|running|runnin|sprint|bolt|bolted|racing|tore off|took off|tearing off)\b",
    re.I,
)


def _frame_key(line: str) -> str:
    """One rush is one frame. Eight sprints are the same ad."""
    low = (line or "").lower()
    if _RUN_FRAME.search(low):
        return "run"
    if re.search(r"\b(dance|dancing|two step)\b", low):
        return "dance"
    return re.sub(r"[^a-z0-9]+", " ", low).strip()


def _reco_line_sells(line: str) -> bool:
    """A frame, not a person who had the setting and not a name taped on the end."""
    low = (line or "").lower().strip()
    if not any(
        phrase in low
        for phrase in (
            "paid fyp",
            "paid to scroll",
            "the scroll",
            "scrolling pays",
            "all i did was scroll",
            "fyp",
            "the setting",
        )
    ):
        return False
    if _word_count(low) > 32:
        return False
    if (
        _EXPLAINER.search(low)
        or _DANGLING.search(low)
        or _PERSON_SETTING.search(low)
        or _LEFT_OUT.search(low)
    ):
        return False
    return True


def _parse_hooks_json(raw: str, count: int, *, reco: bool = False) -> list[str]:
    text = (raw or "").strip()
    if not text:
        raise HooksError("Model returned empty response")
    # Strip markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object in the string
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise HooksError("Model returned non-JSON response") from None
        data = json.loads(m.group(0))
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, list):
        raise HooksError("Model JSON missing hooks array")
    cleaned: list[str] = []
    for item in hooks:
        if not isinstance(item, dict):
            continue
        connect = _jury_sentence(item.get("connect"))
        decide = _jury_sentence(item.get("decide"))
        line = _on_screen_line(item.get("text") or item.get("hook"))
        if not line or not connect or not decide:
            continue
        if reco and not _reco_line_sells(line):
            continue
        cleaned.append(line)
    if reco:
        seen: set[str] = set()
        unique: list[str] = []
        for line in cleaned:
            key = _frame_key(line)
            if key in seen:
                continue
            seen.add(key)
            unique.append(line)
        cleaned = unique
    if not cleaned:
        raise HooksError(
            "Jury rejected the draft: each line needs its own frame. "
            "The run is one frame. The dance is another. "
            "Do not write the run again."
        )
    if len(cleaned) > count:
        cleaned = cleaned[:count]
    return cleaned


def _openai_chat(
    system: str, user: str, use_web_search: bool
) -> tuple[str, str, dict[str, int]]:
    """Call OpenAI; prefer Responses API with web_search for new_angles."""
    from openai import OpenAI

    key = _require_api_key()
    client = OpenAI(api_key=key)
    model = (os.environ.get("OPENAI_MODEL") or "").strip() or OPENAI_MODEL or "gpt-4.1-mini"
    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }

    if use_web_search:
        try:
            resp = client.responses.create(
                model=model,
                tools=[{"type": "web_search"}],
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            out = getattr(resp, "output_text", None) or ""
            if out.strip():
                return out, model, _usage_from_openai(resp)
        except Exception as exc:
            _log.warning("Responses API / web_search failed, falling back: %s", exc)

    # Chat Completions fallback (variations/mix, or web_search unavailable)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.9,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    text = (resp.choices[0].message.content or "") if resp.choices else ""
    return text, model, _usage_from_openai(resp) if resp else empty


def generate_hooks(
    uid: str,
    offer_id: str,
    count: int,
    mode: str,
    extra_instructions: str | None = None,
    *,
    thread_id: str = "",
    random_words: bool = False,
) -> dict:
    """Generate hooks for an offer and append to history. Blocking (run in threadpool)."""
    try:
        count_i = int(count)
    except (TypeError, ValueError) as exc:
        raise HooksError("count must be 2, 5, 10, or 20") from exc
    if count_i not in ALLOWED_COUNTS:
        raise HooksError("count must be 2, 5, 10, or 20")
    if mode not in ALLOWED_MODES:
        raise HooksError("mode must be variations, mix, or new_angles")

    extra = (extra_instructions or "").strip()
    if len(extra) > MAX_EXTRA_LEN:
        extra = extra[:MAX_EXTRA_LEN]

    with _lock:
        data = _load_offers(uid)
        offer = _find_offer(data, offer_id)
        if not offer:
            raise HooksError("Offer not found")
        # Snapshot for prompt (avoid holding lock during network I/O)
        offer_snap = json.loads(json.dumps(offer))

    page_text = ""
    if mode == "new_angles" and offer_snap.get("url"):
        page_text = fetch_offer_page_text(offer_snap["url"])

    try:
        pin_active_offer(uid, offer_id)
    except HooksError:
        pass
    bank = list_caption_bank(uid, offer_id)
    assigned = pick_random_words(count_i) if random_words else []
    system, user = _build_prompt(
        offer_snap,
        count_i,
        mode,
        extra,
        page_text,
        past_captions=bank,
        random_words=assigned,
    )
    use_search = mode == "new_angles"

    def _write(prompt: str) -> tuple[str, str, str, dict[str, int]]:
        try:
            return _writer_chat(system, prompt, use_web_search=use_search)
        except HooksError:
            raise
        except Exception as exc:
            _log.exception("Hook generate failed")
            raise HooksError(f"Hook generate request failed: {exc}") from exc

    raw, provider, model, usage = _write(user)
    try:
        hooks = _parse_hooks_json(
            raw, count_i, reco=_reco_product_phrase(offer_snap)
        )
    except HooksError as exc:
        if "Jury rejected" not in str(exc):
            raise
        raw, provider, model, usage = _write(
            user
            + "\n\nJURY REJECTED the last draft: "
            + str(exc)
            + "\nRewrite them as comments. New moment every line. "
            "Do not define paid fyp. Do not say it was off and stop there."
        )
        hooks = _parse_hooks_json(
            raw, count_i, reco=_reco_product_phrase(offer_snap)
        )

    try:
        from . import usage as usage_mod

        usage_mod.record_llm(
            uid,
            thread_id=thread_id,
            provider=provider,
            model=model,
            usage=usage,
            kind="hooks",
        )
    except Exception:
        _log.debug("Hook usage record failed", exc_info=True)

    with _lock:
        run = _append_history(uid, offer_id, mode, count_i, extra, hooks)

    return {
        "id": run["id"],
        "offerId": offer_id,
        "mode": mode,
        "count": count_i,
        "extraInstructions": extra,
        "createdAt": run["createdAt"],
        "hooks": hooks,
        "model": model,
        "provider": provider,
        "randomWords": assigned,
    }


# ---------------------------------------------------------------------------
# Background generate jobs (survive tab close; UI polls / resumes)
# ---------------------------------------------------------------------------

_gen_jobs: dict[str, dict[str, Any]] = {}
_gen_jobs_lock = threading.Lock()


def start_generate_job(
    uid: str,
    offer_id: str,
    count: int,
    mode: str,
    extra_instructions: str | None = None,
    random_words: bool = False,
) -> dict:
    """Kick off generation in a daemon thread; return a pollable job handle."""
    # Validate early so the client gets a 4xx instead of a failed job.
    try:
        count_i = int(count)
    except (TypeError, ValueError) as exc:
        raise HooksError("count must be 2, 5, 10, or 20") from exc
    if count_i not in ALLOWED_COUNTS:
        raise HooksError("count must be 2, 5, 10, or 20")
    if mode not in ALLOWED_MODES:
        raise HooksError("mode must be variations, mix, or new_angles")
    with _lock:
        data = _load_offers(uid)
        if not _find_offer(data, offer_id):
            raise HooksError("Offer not found")

    job_id = uuid.uuid4().hex[:12]
    job: dict[str, Any] = {
        "id": job_id,
        "owner_uid": uid,
        "status": "running",
        "offerId": offer_id,
        "mode": mode,
        "count": count_i,
        "extraInstructions": (extra_instructions or "").strip()[:MAX_EXTRA_LEN],
        "randomWords": bool(random_words),
        "createdAt": time.time(),
        "finishedAt": None,
        "error": None,
        "result": None,
    }
    with _gen_jobs_lock:
        _gen_jobs[job_id] = job

    def _run() -> None:
        try:
            result = generate_hooks(
                uid,
                offer_id,
                count_i,
                mode,
                extra_instructions,
                random_words=random_words,
            )
            with _gen_jobs_lock:
                job["status"] = "done"
                job["result"] = result
                job["finishedAt"] = time.time()
        except HooksError as exc:
            with _gen_jobs_lock:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["finishedAt"] = time.time()
        except Exception as exc:
            _log.exception("Hook generate job failed")
            with _gen_jobs_lock:
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"
                job["finishedAt"] = time.time()

    threading.Thread(target=_run, daemon=True, name=f"hooks-gen-{job_id}").start()
    return {
        "jobId": job_id,
        "status": "running",
        "offerId": offer_id,
        "mode": mode,
        "count": count_i,
    }


def get_generate_job(job_id: str, uid: str | None = None) -> dict | None:
    with _gen_jobs_lock:
        job = _gen_jobs.get(job_id)
        if not job:
            return None
        if uid is not None and job.get("owner_uid") != uid:
            return None
        out = {
            "jobId": job["id"],
            "status": job["status"],
            "offerId": job.get("offerId"),
            "mode": job.get("mode"),
            "count": job.get("count"),
            "error": job.get("error"),
        }
        if job.get("status") == "done" and job.get("result"):
            out.update(job["result"])
        return out


def has_running_generate_job() -> bool:
    """True while a background hook-generation thread is still in flight."""
    with _gen_jobs_lock:
        return any(str(j.get("status") or "") == "running" for j in _gen_jobs.values())


def list_active_generate_jobs(uid: str) -> list[dict]:
    with _gen_jobs_lock:
        return [
            {
                "jobId": j["id"],
                "status": j["status"],
                "offerId": j.get("offerId"),
                "mode": j.get("mode"),
                "count": j.get("count"),
            }
            for j in _gen_jobs.values()
            if j.get("owner_uid") == uid and j.get("status") == "running"
        ]
