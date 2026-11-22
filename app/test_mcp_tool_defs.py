"""Fail if worker/src/mcp/tool-defs.json drifts from app.agent.tools.TOOL_DEFS."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.agent import tools as t

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "worker" / "src" / "mcp" / "tool-defs.json"

WORKER_ONLY = {
    "create_thread",
    "list_threads",
    "set_active_thread",
    "confirm_tool",
    "get_spark_job",
}


class McpToolDefsDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        self.by_name = {row["name"]: row for row in self.rows}

    def test_every_python_tool_is_declared(self) -> None:
        missing = [d["name"] for d in t.TOOL_DEFS if d["name"] not in self.by_name]
        self.assertEqual(missing, [], f"run python scripts/generate_mcp_tool_defs.py; missing {missing}")

    def test_json_has_no_unknown_tools(self) -> None:
        python = {d["name"] for d in t.TOOL_DEFS}
        extra = [n for n in self.by_name if n not in python and n not in WORKER_ONLY]
        self.assertEqual(extra, [])

    def test_confirm_flags_match(self) -> None:
        for d in t.TOOL_DEFS:
            row = self.by_name.get(d["name"])
            if not row:
                continue
            self.assertEqual(
                bool(row.get("confirm")),
                bool(d.get("confirm")),
                d["name"],
            )

    def test_required_params_and_sales_fields(self) -> None:
        for d in t.TOOL_DEFS:
            row = self.by_name[d["name"]]
            src = d["parameters"]
            got = row["inputSchema"]
            self.assertEqual(
                sorted(src.get("required") or []),
                sorted(got.get("required") or []),
                d["name"],
            )
            src_props = set((src.get("properties") or {}).keys())
            got_props = set((got.get("properties") or {}).keys())
            missing = src_props - got_props
            self.assertFalse(missing, f"{d['name']} missing properties {missing}")


if __name__ == "__main__":
    unittest.main()
