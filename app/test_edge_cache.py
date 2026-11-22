"""Unit tests for edge_cache activity signalling."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class EdgeCacheActivityTests(unittest.TestCase):
    def test_build_activity_idle(self) -> None:
        from app import edge_cache

        with (
            patch.object(edge_cache, "_pending_step_count", return_value=0),
            patch.object(edge_cache, "_runtime_active_count", return_value=0),
        ):
            out = edge_cache.build_activity("uid1")
        self.assertFalse(out["hasWork"])
        self.assertEqual(out["pendingSteps"], 0)
        self.assertEqual(out["runtimeJobs"], 0)

    def test_build_activity_busy_steps(self) -> None:
        from app import edge_cache

        with (
            patch.object(edge_cache, "_pending_step_count", return_value=2),
            patch.object(edge_cache, "_runtime_active_count", return_value=0),
        ):
            out = edge_cache.build_activity("uid1")
        self.assertTrue(out["hasWork"])
        self.assertEqual(out["pendingSteps"], 2)

    def test_publish_skips_identical_signature(self) -> None:
        from app import edge_cache

        edge_cache._last_activity_sig.clear()
        put = MagicMock(return_value=True)
        with (
            patch.object(edge_cache, "_pending_step_count", return_value=0),
            patch.object(edge_cache, "_runtime_active_count", return_value=0),
            patch.object(edge_cache, "put_edge_json", put),
        ):
            edge_cache.publish_activity("uid1")
            edge_cache.publish_activity("uid1")
            self.assertEqual(put.call_count, 1)
            edge_cache.publish_activity("uid1", force=True)
            self.assertEqual(put.call_count, 2)


if __name__ == "__main__":
    unittest.main()
