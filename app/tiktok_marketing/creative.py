"""Learn More CTA portfolio and Display Card for sales Spark ads."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from .client import TikTokApiError, tiktok_request

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = ROOT / "data" / "display_cards" / "tap_here.png"
CARD_PATH = ROOT / "data" / "display_cards" / "tap_here_750x421.jpg"
LEARN_MORE = "LEARN_MORE"


def _is_learn_more(label: str) -> bool:
    return "".join(ch for ch in (label or "").lower() if ch.isalnum()) == "learnmore"


def learn_more_cta_id(token: str, advertiser_id: str) -> str:
    """Dynamic CTA portfolio that only contains Learn More."""
    data = tiktok_request(
        "GET",
        "/creative/cta/recommend/",
        token,
        params={
            "advertiser_id": str(advertiser_id),
            "new_version": True,
            "asset_type": "CTA_AUTO_OPTIMIZED",
            "objective_type": "WEB_CONVERSIONS",
            "promotion_type": "WEBSITE",
        },
    )
    content = []
    for asset in data.get("recommend_assets") or []:
        if not isinstance(asset, dict):
            continue
        label = str(asset.get("asset_content") or "")
        ids = [str(item) for item in (asset.get("asset_ids") or []) if str(item)]
        if ids and _is_learn_more(label):
            content.append({"asset_content": "Learn more", "asset_ids": ids})
            break
    if not content:
        raise TikTokApiError("Learn more CTA is not available on this ad account")
    created = tiktok_request(
        "POST",
        "/creative/portfolio/create/",
        token,
        json_body={
            "advertiser_id": str(advertiser_id),
            "creative_portfolio_type": "CTA",
            "portfolio_content": content,
        },
    )
    return str(created.get("creative_portfolio_id") or "")


def _write_display_card() -> Path:
    """Resize the TAP HERE card to TikTok's 750x421 Display Card size."""
    CARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    source = SOURCE_PATH if SOURCE_PATH.is_file() else CARD_PATH
    if not source.is_file():
        raise TikTokApiError("TAP HERE display card image is missing")
    if (
        CARD_PATH.is_file()
        and CARD_PATH.stat().st_mtime >= source.stat().st_mtime
        and CARD_PATH.stat().st_size > 1000
    ):
        return CARD_PATH
    from PIL import Image

    raw = Image.open(source).convert("RGB")
    width, height = 750, 421
    # Contain (letterbox) so TAP HERE is never cropped by the 750x421 card.
    scale = min(width / raw.width, height / raw.height)
    fitted = raw.resize(
        (max(1, int(raw.width * scale)), max(1, int(raw.height * scale))),
        getattr(Image, "Resampling", Image).LANCZOS,
    )
    canvas = Image.new("RGB", (width, height), (17, 17, 17))
    canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    canvas.save(CARD_PATH, "JPEG", quality=95)
    return CARD_PATH


def _upload_card_image(token: str, advertiser_id: str) -> str:
    path = _write_display_card()
    raw = path.read_bytes()
    signature = hashlib.md5(raw).hexdigest()
    last_error: TikTokApiError | None = None
    for suffix in ("", f"_{int(time.time())}"):
        file_name = f"tap_here_{signature[:12]}{suffix}.jpg"
        try:
            data = tiktok_request(
                "POST",
                "/file/image/ad/upload/",
                token,
                params={"advertiser_id": str(advertiser_id)},
                form={
                    "advertiser_id": str(advertiser_id),
                    "upload_type": "UPLOAD_BY_FILE",
                    "file_name": file_name,
                    "image_signature": signature,
                },
                files={"image_file": (file_name, raw, "image/jpeg")},
            )
        except TikTokApiError as exc:
            last_error = exc
            if "duplicate" not in str(exc).lower():
                raise
            continue
        return str(data.get("image_id") or data.get("id") or "")
    if last_error:
        raise last_error
    return ""


def display_card_id(token: str, advertiser_id: str) -> str:
    """Display Card interactive add-on. 750x421 image, nothing else."""
    image_id = _upload_card_image(token, advertiser_id)
    if not image_id:
        raise TikTokApiError("Display Card image did not upload")
    created = tiktok_request(
        "POST",
        "/creative/portfolio/create/",
        token,
        json_body={
            "advertiser_id": str(advertiser_id),
            "creative_portfolio_type": "CARD",
            "portfolio_content": [{"card_type": "IMAGE", "image_id": image_id}],
        },
    )
    return str(created.get("creative_portfolio_id") or "")
