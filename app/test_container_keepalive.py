"""Idle container keepalive only reports work that is still running."""

from __future__ import annotations

import unittest

from app.container_keepalive import _LIVE


class _Job:
    def __init__(self, status: str) -> None:
        self.status = status


class _Runtime:
    def __init__(self, jobs: list[_Job]) -> None:
        self._jobs = jobs

    def values(self) -> list[_Job]:
        return list(self._jobs)


def _any_live(runtimes: list[_Runtime]) -> bool:
    for runtime in runtimes:
        for job in runtime.values():
            if str(getattr(job, "status", "") or "") in _LIVE:
                return True
    return False


class KeepaliveTests(unittest.TestCase):
    def test_finished_jobs_do_not_count(self) -> None:
        self.assertFalse(_any_live([
            _Runtime([_Job("done"), _Job("failed"), _Job("cancelled")]),
        ]))

    def test_running_or_queued_counts(self) -> None:
        self.assertTrue(_any_live([
            _Runtime([_Job("done")]),
            _Runtime([_Job("queued")]),
        ]))
        self.assertTrue(_any_live([_Runtime([_Job("running")])]))


if __name__ == "__main__":
    unittest.main()
