"""Campaign chat composes existing BC tools instead of requiring Spark codes."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import loop as agent_loop
from app.agent import providers
from app.agent import tools as agent_tools

ROOT = Path(__file__).resolve().parents[2]
INSTRUCTIONS = ROOT / "worker" / "src" / "mcp" / "instructions.ts"


def _ctx() -> agent_tools.ToolContext:
    return agent_tools.ToolContext(
        list_sources=lambda *_a, **_k: [],
        delete_source=lambda *_a, **_k: {},
        register_downloaded=lambda *_a, **_k: {},
        set_end_caption=lambda *_a, **_k: {},
        list_outputs=lambda *_a, **_k: {},
        delete_outputs=lambda *_a, **_k: {},
        build_batch_videos=lambda *_a, **_k: [],
        start_batch_from_pairs=lambda *_a, **_k: None,
    )


def _thread(text: str) -> dict:
    return {"messages": [{"role": "user", "content": text}]}


class CampaignChatToolGateTests(unittest.TestCase):
    def test_launch_phrase_selects_sales_campaign(self) -> None:
        names = agent_tools.chat_tool_names(_thread("launch 264 at 0.21"))
        self.assertIn("start_sales_campaign", names)
        self.assertIn("continue_bc", names)
        self.assertIn("mark_bc_payment", names)

    def test_profile_number_selects_business_center_tools(self) -> None:
        names = agent_tools.chat_tool_names(_thread("264"))
        self.assertIn("start_sales_campaign", names)

    def test_caption_request_does_not_select_sales_campaign(self) -> None:
        names = agent_tools.chat_tool_names(_thread("write 10 captions for these clips"))
        self.assertNotIn("start_sales_campaign", names)

    def test_render_and_launch_also_selects_publish_tools(self) -> None:
        names = agent_tools.chat_tool_names(_thread("render and launch the campaign"))
        self.assertIn("start_sales_campaign", names)
        self.assertIn("start_tiktok_upload", names)

    def test_bc_turns_get_more_tool_rounds(self) -> None:
        plain = agent_tools.chat_tool_names(_thread("write 10 captions"))
        launch = agent_tools.chat_tool_names(_thread("launch 264 at 0.21"))
        self.assertEqual(agent_loop.tool_round_limit(plain), agent_loop.MAX_TOOL_ROUNDS)
        self.assertEqual(agent_loop.tool_round_limit(launch), agent_loop.BC_TOOL_ROUNDS)
        self.assertGreater(agent_loop.BC_TOOL_ROUNDS, agent_loop.MAX_TOOL_ROUNDS)


class CampaignChatPromptTests(unittest.TestCase):
    def test_prompts_do_not_require_spark_codes_for_sales(self) -> None:
        instructions = INSTRUCTIONS.read_text(encoding="utf-8")
        live = providers.system_prompt_for_tools(
            agent_tools.CHAT_TOOL_GROUPS["business_center"]
        )
        blobs = (live, instructions)
        for blob in blobs:
            self.assertIn("start_sales_campaign", blob)
            self.assertIn("continue_bc", blob)
            self.assertIn("mark_bc_payment", blob)
            self.assertIn("Do not call fetch_spark", blob)
            self.assertIn("Do not wait for Scheduled", blob)
            self.assertIn("Say the money", blob)
            self.assertIn("[[uncompliant:income]]", blob)
            self.assertNotIn("never say money", blob)
            self.assertNotIn("3rd-grade reading", blob)
            self.assertNotIn("Lead-gen is not built", blob)
            self.assertNotIn("adsReady only when white-hat is Scheduled", blob)
            self.assertNotIn(
                "After white-hat Scheduled, Sales Instant Page ads enqueue automatically",
                blob,
            )


class CampaignChatWrapperTests(unittest.TestCase):
    def test_continue_bc_calls_continue_profile(self) -> None:
        job = object()
        with (
            patch("app.provision_jobs.continue_profile", return_value=job) as cont,
            patch("app.provision_jobs.job_state", return_value={"id": "job-1", "status": "queued"}),
        ):
            out = agent_tools.execute(
                "uid",
                "continue_bc",
                {"profileId": "p264"},
                _ctx(),
            )
        cont.assert_called_once_with("uid", "p264")
        self.assertTrue(out["ok"])
        self.assertEqual(out["job"]["id"], "job-1")

    def test_mark_bc_payment_calls_existing_helper(self) -> None:
        with patch(
            "app.routers.provision.provision_mark_payment_added",
            return_value={"ok": True, "entry": {"amountUsd": 12}},
        ) as marked:
            out = agent_tools.execute(
                "uid",
                "mark_bc_payment",
                {"profileId": "p264", "amountUsd": 12},
                _ctx(),
            )
        marked.assert_called_once()
        self.assertEqual(marked.call_args.args[0], "p264")
        self.assertEqual(marked.call_args.args[1].amountUsd, 12)
        self.assertEqual(marked.call_args.kwargs["uid"], "uid")
        self.assertTrue(out["ok"])

    def test_confirm_summaries(self) -> None:
        self.assertIn(
            "p264",
            agent_tools.confirm_summary("continue_bc", {"profileId": "p264"}),
        )
        payment = agent_tools.confirm_summary(
            "mark_bc_payment", {"profileId": "p264", "amountUsd": 10}
        )
        self.assertIn("p264", payment)
        self.assertIn("10", payment)


if __name__ == "__main__":
    unittest.main()
