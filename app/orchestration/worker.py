"""Bounded executor. Each grant owns one thread; waits return to the coordinator.

Handlers must check context ownership before external actions, and declare
ambiguous results instead of silently replaying a mutation.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .client import Client, LeaseLost, SchedulerUnavailable

_log = logging.getLogger(__name__)


@dataclass
class Context:
    client: Client
    tenant: str
    worker: str
    task: dict
    lost: threading.Event = field(default_factory=threading.Event)
    deadline: float = field(default_factory=lambda: time.monotonic() + 60)

    def check(self) -> None:
        if self.lost.is_set() or time.monotonic() >= self.deadline:
            raise LeaseLost("Task ownership expired or was cancelled")

    def checkpoint(self, value: dict) -> None:
        self.renew(value)

    def renew(self, value: dict | None = None) -> None:
        self.check()
        sent = time.monotonic()
        extra = {"checkpoint": value} if value is not None else {}
        response = self.client.call("heartbeat", tenant=self.tenant, worker=self.worker,
                                    token=self.task["token"], **extra)
        if not response.get("ok"):
            self.lost.set()
            raise LeaseLost("Task cancelled")
        self.deadline = sent + 60


Handler = Callable[[Context, dict], dict]


class Registry:
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self.handlers:
            raise ValueError(f"Handler already registered: {kind}")
        self.handlers[kind] = handler


class Worker:
    def __init__(self, client: Client, tenant: str, registry: Registry, *, slots: int = 3,
                 host: str = "worker"):
        self.client, self.tenant, self.registry = client, tenant, registry
        self.slots = max(1, min(16, slots))
        self.id = f"{host}:{uuid.uuid4().hex}"
        self.stop = threading.Event()

    def execute(self, task: dict) -> None:
        ctx = Context(self.client, self.tenant, self.id, task)
        finished = threading.Event()

        def renew() -> None:
            while not finished.wait(20):
                try:
                    ctx.renew()
                except Exception:
                    ctx.lost.set()
                    return

        heartbeat = threading.Thread(target=renew, daemon=True, name="orchestration-lease")
        heartbeat.start()
        try:
            ctx.check()
            outcome = self.registry.handlers[task["kind"]](ctx, task["input"])
        except LeaseLost:
            # Let the coordinator quarantine uncertain mutations. Never free a
            # device from a worker that can no longer prove ownership.
            return
        except Exception as exc:
            _log.exception("Handler failed kind=%s", task["kind"])
            outcome = {"type": "ambiguous", "reason": f"handler_exception:{type(exc).__name__}"}
        finally:
            finished.set()
            heartbeat.join(timeout=1)
        # Completion is idempotent by grant token. Retrying a lost reply does
        # not call the provider again. Failed reports leave durable recovery work.
        for attempt in range(3):
            try:
                self.client.call("complete", tenant=self.tenant, worker=self.id,
                                 token=task["token"], outcome=outcome)
                return
            except LeaseLost:
                return
            except SchedulerUnavailable:
                if attempt == 2:
                    _log.exception("Result needs reconciliation workflow=%s task=%s", task["workflow"], task["id"])
                else:
                    self.stop.wait(1)

    def run(self) -> None:
        with ThreadPoolExecutor(max_workers=self.slots, thread_name_prefix="operation") as pool:
            futures = set()
            claim_id = uuid.uuid4().hex
            while not self.stop.is_set():
                futures = {f for f in futures if not f.done()}
                if len(futures) >= self.slots:
                    self.stop.wait(.2)
                    continue
                try:
                    task = self.client.call("claim", tenant=self.tenant, worker=self.id,
                                            kinds=list(self.registry.handlers), requestId=claim_id).get("task")
                except SchedulerUnavailable:
                    self.stop.wait(5)
                    continue
                if task:
                    # A replayed claim may be old. Renew before invoking a handler.
                    try:
                        owned = self.client.call("heartbeat", tenant=self.tenant, worker=self.id, token=task["token"])
                        if not owned.get("ok"):
                            claim_id = uuid.uuid4().hex
                            continue
                    except LeaseLost:
                        claim_id = uuid.uuid4().hex
                        continue
                    except SchedulerUnavailable:
                        self.stop.wait(5)
                        continue
                    futures.add(pool.submit(self.execute, task))
                else:
                    self.stop.wait(2)
                claim_id = uuid.uuid4().hex
