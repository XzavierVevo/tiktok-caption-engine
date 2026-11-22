"""Scheduler rows should identify the real work shown in the activity bar."""

import unittest
from unittest.mock import patch

from app.routers.operations import _describe_workflows


class WorkflowDescriptionTests(unittest.TestCase):
    def test_deactivate_grant_names_profile_and_reason(self) -> None:
        snapshot = {
            "workflows": [{"id": "wf-1", "state": "active", "request_key": "provision-step:step-1"}],
            "tasks": [{"id": "step-1", "workflow": "wf-1", "state": "running"}],
        }
        with (
            patch("app.provision.inventory.get_step", return_value={
                "id": "step-1", "kind": "deactivate_ad_accounts",
                "profileId": "p-249", "status": "claimed",
                "orchestration": {"workflow": "wf-1"},
            }),
            patch("app.provision.inventory.get_profile", return_value={"profileNo": 249}),
        ):
            described = _describe_workflows("uid", snapshot)["workflows"][0]
        self.assertEqual(described["title"], "Deactivate ad accounts")
        self.assertEqual(described["profileLabel"], "249")
        self.assertIn("disapproval", described["purpose"])
        self.assertEqual(described["stepStatus"], "claimed")
        self.assertTrue(described["admitted"])

    def test_orphaned_admission_is_not_shown_as_running(self) -> None:
        snapshot = {
            "workflows": [{"id": "wf-old", "state": "active"}],
            "tasks": [{"id": "step-old", "workflow": "wf-old", "state": "ready"}],
        }
        with (
            patch("app.provision.inventory.get_step", return_value={
                "id": "step-old", "kind": "deactivate_ad_accounts",
                "profileId": "p-249", "status": "pending",
            }),
            patch("app.provision.inventory.get_profile", return_value={"profileNo": 249}),
        ):
            described = _describe_workflows("uid", snapshot)["workflows"][0]
        self.assertFalse(described["admitted"])


if __name__ == "__main__":
    unittest.main()
