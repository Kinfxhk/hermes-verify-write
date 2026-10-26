"""Wiring test: hooks registered by register(), and a failure reaching the next turn.

No Hermes import — a stub ctx stands in for PluginContext, and the two handlers are
called exactly the way the host calls them (keyword payloads, signature-inspected).
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_verify_write", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_verify_write"] = module
    spec.loader.exec_module(module)
    return module


class StubContext:
    def __init__(self):
        self.hooks = {}
        self.commands = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description=""):
        self.commands[name] = handler


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.plugin.vw.reset()
        self.ctx = StubContext()
        self.plugin.register(self.ctx)

    def test_both_hooks_registered(self):
        self.assertIn("post_tool_call", self.ctx.hooks)
        self.assertIn("pre_llm_call", self.ctx.hooks)

    def test_failed_write_reaches_the_next_turn(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "f.txt")
        Path(path).write_text("stale", encoding="utf-8")

        self.ctx.hooks["post_tool_call"](
            tool_name="write_file",
            args={"path": path, "content": "fresh"},
            result="ok",
            status="ok",
            session_id="s-1",
            task_id="t-1",
        )
        out = self.ctx.hooks["pre_llm_call"](session_id="s-1", task_id="t-1")
        self.assertIsInstance(out, dict)
        self.assertIn("FAILED", out["context"])
        self.assertIn("sha mismatch", out["context"])

    def test_clean_write_says_nothing(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "f.txt")
        Path(path).write_text("fresh", encoding="utf-8")
        self.ctx.hooks["post_tool_call"](
            tool_name="write_file",
            args={"path": path, "content": "fresh"},
            result="ok",
            status="ok",
            session_id="s-2",
        )
        self.assertIsNone(self.ctx.hooks["pre_llm_call"](session_id="s-2"))

    def test_tool_error_is_not_double_reported(self):
        self.ctx.hooks["post_tool_call"](
            tool_name="write_file",
            args={"path": "/nope/f.txt", "content": "x"},
            result="error: permission denied",
            status="error",
            session_id="s-3",
        )
        self.assertIsNone(self.ctx.hooks["pre_llm_call"](session_id="s-3"))

    def test_slash_command_prints_the_ledger(self):
        self.ctx.hooks["post_tool_call"](
            tool_name="write_file",
            args={"path": "/definitely/missing.txt", "content": "x"},
            result="ok",
            status="ok",
            session_id="s-4",
        )
        text = self.ctx.commands["verify-write"]("s-4")
        self.assertIn("FAILED", text)

    def test_hook_never_raises_on_garbage(self):
        # a hostile payload must not break the turn
        self.ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": None}, result=None)
        self.ctx.hooks["pre_llm_call"](session_id="")


if __name__ == "__main__":
    unittest.main()
