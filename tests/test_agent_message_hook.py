import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from hooks import agent_relay_hook as hook  # noqa: E402
import agent_message_bus as bus_module  # noqa: E402


class MessageHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-message-hook-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "messages.sqlite3"
        self.bus = bus_module.MessageBus(self.db)
        self.bus.register("pi:sender", session_id="sender")
        self.bus.register("codex:session-a", session_id="session-a")
        self.bus.send(
            "pi:sender",
            "codex:session-a",
            "peer finding",
            push=False,
        )
        self.env = {
            "AGENTRELAY_MESSAGES": "on",
            "AGENTRELAY_MESSAGE_DB": str(self.db),
            "AGENTRELAY_TARGET": "codex",
        }

    def test_safe_hook_event_registers_and_renders_peer_context(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-a",
            "cwd": str(PROJECT_ROOT),
        }
        with mock.patch.dict(os.environ, self.env, clear=False):
            context = hook.agent_message_context(payload, "PostToolUse")
        self.assertIn("peer finding", context)
        self.assertIn("not_user_authorization", context)
        self.assertEqual(self.bus.inbox("codex:session-a"), [])

    def test_message_context_is_opt_in_and_never_runs_on_pre_tool_deny_event(self):
        payload = {"session_id": "session-a", "cwd": str(PROJECT_ROOT)}
        with mock.patch.dict(os.environ, {"AGENTRELAY_MESSAGES": "off"}, clear=False):
            self.assertEqual(hook.agent_message_context(payload, "PostToolUse"), "")
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertEqual(hook.agent_message_context(payload, "PreToolUse"), "")
        self.assertEqual(len(self.bus.inbox("codex:session-a", peek=True)), 1)

    def test_existing_tracker_context_is_preserved(self):
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "existing tracker context",
            }
        }
        merged = hook.merge_agent_message_context(
            json.dumps(output),
            "PostToolUse",
            "peer context",
        )
        context = json.loads(merged)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("existing tracker context", context)
        self.assertIn("peer context", context)

    def test_when_idle_message_is_released_at_the_next_prompt_boundary(self):
        message = self.bus.send(
            "pi:sender",
            "codex:session-a",
            "idle-only finding",
            delivery="when_idle",
            push=False,
        )
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-a",
            "cwd": str(PROJECT_ROOT),
        }
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertNotIn(
                "idle-only finding",
                hook.agent_message_context(payload, "PostToolUse"),
            )
            prompt = {**payload, "hook_event_name": "UserPromptSubmit"}
            context = hook.agent_message_context(prompt, "UserPromptSubmit")
        self.assertIn("idle-only finding", context)
        self.assertEqual(self.bus.get(message["message_id"])["status"], "delivered")

    def test_empty_tracker_output_still_produces_valid_hook_json(self):
        merged = hook.merge_agent_message_context("", "UserPromptSubmit", "peer context")
        output = json.loads(merged)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIn("peer context", output["additionalContext"])


if __name__ == "__main__":
    unittest.main()
