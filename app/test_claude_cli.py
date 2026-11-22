"""Opus text goes through the Claude CLI subscription, not the Anthropic API."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.agent import providers
from app.claude_cli import ClaudeCliError, child_env, complete
from app.hooks import HooksError, _writer_chat


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ClaudeCliTests(unittest.TestCase):
    def test_child_env_strips_api_key(self) -> None:
        env = child_env({"ANTHROPIC_API_KEY": "sk-ant", "CLAUDE_CODE_OAUTH_TOKEN": "tok", "PATH": "x"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")

    def test_complete_passes_prompt_and_strips_key(self) -> None:
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["input"] = kwargs.get("input")
            seen["env"] = kwargs.get("env") or {}
            return _Proc(json.dumps({"result": "paid fyp was off"}))

        text = complete(
            "be short",
            "write one line",
            env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "sk-ant", "PATH": "x"},
            run=fake_run,
            executable="claude",
        )
        self.assertEqual(text, "paid fyp was off")
        self.assertEqual(seen["input"], "write one line")
        self.assertIn("--model", seen["cmd"])
        self.assertIn("opus", seen["cmd"])
        self.assertIn("--system-prompt", seen["cmd"])
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])
        self.assertEqual(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "tok")

    def test_missing_token(self) -> None:
        with self.assertRaises(ClaudeCliError):
            complete("s", "u", env={"PATH": "x"}, run=lambda *a, **k: _Proc(""), executable="claude")

    def test_writer_uses_cli_not_openai(self) -> None:
        with (
            patch("app.claude_cli.complete", return_value="hook line") as cli,
            patch("app.hooks._openai_chat", side_effect=AssertionError("openai api")),
        ):
            text, provider, model, usage = _writer_chat("sys", "user", use_web_search=True)
        self.assertEqual(text, "hook line")
        self.assertEqual(provider, "claude-cli")
        self.assertEqual(model, "opus")
        self.assertEqual(usage["input_tokens"], 0)
        cli.assert_called_once()

    def test_writer_does_not_fall_back(self) -> None:
        with (
            patch("app.claude_cli.complete", side_effect=ClaudeCliError("no token")),
            patch("app.hooks._openai_chat", side_effect=AssertionError("openai api")),
        ):
            with self.assertRaises(HooksError):
                _writer_chat("sys", "user", use_web_search=False)

    def test_opus_text_uses_cli(self) -> None:
        with patch("app.claude_cli.complete", return_value="line") as cli:
            out = providers._anthropic(
                "claude-opus-5",
                [{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}],
                False,
            )
        self.assertEqual(out["content"], "line")
        self.assertEqual(out["tool_calls"], [])
        self.assertEqual(out["usage"]["output_tokens"], 0)
        cli.assert_called_once()
        system, user = cli.call_args.args[:2]
        self.assertIn("rules", system)
        self.assertEqual(user, "hi")

    def test_opus_tools_do_not_call_api(self) -> None:
        with patch("app.claude_cli.complete", side_effect=AssertionError("cli")):
            with self.assertRaises(ValueError) as ctx:
                providers._anthropic(
                    "claude-opus-5",
                    [{"role": "user", "content": "hi"}],
                    True,
                )
        self.assertIn("non-Opus", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
