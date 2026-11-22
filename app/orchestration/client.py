"""Versioned client for the installation's sole scheduling authority."""
from __future__ import annotations

import os
from typing import Any

import requests


class SchedulerUnavailable(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get("ORCHESTRATION_ENABLED", "0").strip() == "1"


class Client:
    def __init__(self, base_url: str | None = None, key: str | None = None):
        self.base = (base_url or os.environ.get("ORCHESTRATION_BASE_URL") or
                     os.environ.get("CLOUDFLARE_STATE_BASE_URL") or
                     os.environ.get("PUBLIC_UI_ORIGIN") or "").rstrip("/")
        self.key = key if key is not None else os.environ.get("INTERNAL_API_KEY", "")
        if not self.base.startswith("https://") or not self.key:
            raise SchedulerUnavailable("Orchestration requires an HTTPS coordinator and service key")

    def call(self, operation: str, **payload: Any) -> dict:
        try:
            response = requests.post(
                f"{self.base}/_internal/orchestration/{operation}", json=payload,
                headers={"x-internal-api-key": self.key}, timeout=(3, 12),
                allow_redirects=False,
            )
            if response.status_code == 409:
                raise LeaseLost("Scheduler lease is no longer owned")
            if response.status_code != 200:
                raise SchedulerUnavailable(f"Scheduler {operation} returned HTTP {response.status_code}")
            result = response.json()
            if not isinstance(result, dict):
                raise SchedulerUnavailable("Invalid coordinator response")
            return result
        except (requests.RequestException, ValueError) as exc:
            raise SchedulerUnavailable(f"Scheduler {operation} unavailable") from exc

    def submit(self, tenant: str, request_key: str, tasks: list[dict]) -> str:
        return str(self.call("submit", tenant=tenant, requestKey=request_key, tasks=tasks)["id"])

    def snapshot(self, tenant: str, workflow: str | None = None) -> dict:
        return self.call("snapshot", tenant=tenant, workflow=workflow)

    def control(self, tenant: str, workflow: str, action: str) -> dict:
        return self.call("control", tenant=tenant, workflow=workflow, action=action)
