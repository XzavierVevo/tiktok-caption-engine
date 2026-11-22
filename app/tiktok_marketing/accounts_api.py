"""Create one auction ad account at a time under a Business Center."""

from __future__ import annotations

from typing import Any

from .client import tiktok_request


def list_qualifications(token: str, bc_id: str) -> list[dict[str, Any]]:
    data = tiktok_request(
        "GET",
        "/bc/advertiser/qualification/get/",
        token,
        params={"bc_id": str(bc_id)},
    )
    rows = data.get("list") or data.get("qualifications") or []
    return [row for row in rows if isinstance(row, dict)]


def create_ad_account(token: str, bc_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """POST /bc/advertiser/create/. One account per call. Type AUCTION."""
    qualification_id = str(spec.get("qualificationId") or "").strip()
    body: dict[str, Any] = {
        "bc_id": str(bc_id),
        "advertiser_info": {
            "name": str(spec.get("name") or "Ad account")[:100],
            "currency": str(spec.get("currency") or "USD"),
            "timezone": str(spec.get("timezone") or "America/Los_Angeles"),
            "type": "AUCTION",
        },
        "customer_info": {
            "company": str(spec.get("company") or "Company")[:255],
            "industry": int(spec.get("industry") or 290101),
            "registered_area": str(spec.get("registeredArea") or "US"),
        },
    }
    bc_type = str(spec.get("bcType") or "DIRECT").upper()
    if bc_type != "DIRECT" and qualification_id:
        body["qualification_info"] = {"qualification_id": qualification_id}
    return tiktok_request("POST", "/bc/advertiser/create/", token, json_body=body)
