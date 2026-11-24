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
        self.send(
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

    def send(self, *args, **kwargs):
        if not kwargs.get("reply_to"):
            kwargs.setdefault("review_action", "new")
            kwargs.setdefault("review_reason", "Independent peer input is needed")
        return self.bus.send(*args, **kwargs)

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

    def test_message_context_defaults_on_and_never_runs_on_pre_tool_deny_event(self):
        payload = {"session_id": "default-session", "cwd": str(PROJECT_ROOT)}
        default_env = dict(self.env)
        default_env.pop("AGENTRELAY_MESSAGES", None)
        with mock.patch.dict(os.environ, default_env, clear=False):
            os.environ.pop("AGENTRELAY_MESSAGES", None)
            self.assertEqual(
                hook.agent_message_context(payload, "SessionStart"),
                "",
            )
        self.assertIn(
            "codex:default-session",
            {item["address"] for item in self.bus.list_agents()},
        )
        with mock.patch.dict(os.environ, {"AGENTRELAY_MESSAGES": "off"}, clear=False):
            self.assertEqual(hook.agent_message_context(payload, "PostToolUse"), "")
        with mock.patch.dict(os.environ, self.env, clear=False):
            self.assertEqual(hook.agent_message_context(payload, "PreToolUse"), "")
        self.assertEqual(len(self.bus.inbox("codex:session-a", peek=True)), 1)

    def test_claude_native_send_message_cannot_bypass_task_review(self):
        payload = {
            "session_id": "claude-a",
            "tool_name": "SendMessage",
            "tool_input": {"to": "another-claude", "message": "duplicate task"},
        }
        env = {
            "AGENTRELAY_MESSAGES": "on",
            "AGENTRELAY_MESSAGE_DB": str(self.db),
            "AGENTRELAY_TARGET": "claude",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            reason = hook.agent_message_context(payload, "PreToolUse")
            self.assertIn("task review", reason)
            self.assertIn("history", reason)
            self.assertEqual(
                hook.agent_message_context(
                    {**payload, "tool_name": "Read"},
                    "PreToolUse",
                ),
                "",
            )
        merged = json.loads(hook.merge_agent_message_context(
            "",
            "PreToolUse",
            reason,
        ))
        output = merged["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("task review", output["permissionDecisionReason"])

    def test_claude_discovers_codex_and_pi_and_receives_correlated_replies(self):
        claude_payload = {"session_id": "claude-a", "cwd": str(PROJECT_ROOT)}
        claude_env = {
            "AGENTRELAY_MESSAGE_DB": str(self.db),
            "AGENTRELAY_TARGET": "claude",
        }
        with mock.patch.dict(os.environ, claude_env, clear=False):
            os.environ.pop("AGENTRELAY_MESSAGES", None)
            os.environ.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
            hook.agent_message_context(claude_payload, "SessionStart")

        peers = {item["address"] for item in self.bus.list_agents()}
        self.assertTrue({"claude:claude-a", "codex:session-a", "pi:sender"} <= peers)

        codex_request = self.send(
            "claude:claude-a",
            "codex:session-a",
            "Codex independent review",
            kind="task",
        )
        codex_env = {
            "AGENTRELAY_MESSAGES": "on",
            "AGENTRELAY_MESSAGE_DB": str(self.db),
            "AGENTRELAY_TARGET": "codex",
        }
        with mock.patch.dict(os.environ, codex_env, clear=False):
            codex_context = hook.agent_message_context(
                {"session_id": "session-a", "cwd": str(PROJECT_ROOT)},
                "PostToolUse",
            )
        self.assertIn("Codex independent review", codex_context)
        self.send(
            "codex:session-a",
            reply_to=codex_request["message_id"],
            body="Codex review reply",
        )

        pi_request = self.send(
            "claude:claude-a",
            "pi:sender",
            "Pi independent search",
            kind="task",
        )
        pi_incoming = self.bus.inbox("pi:sender")
        self.assertEqual(pi_incoming[0]["body"], "Pi independent search")
        self.send(
            "pi:sender",
            reply_to=pi_request["message_id"],
            body="Pi search reply",
        )

        with mock.patch.dict(os.environ, claude_env, clear=False):
            os.environ.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
            replies = hook.agent_message_context(
                claude_payload,
                "UserPromptSubmit",
            )
        self.assertIn("Codex review reply", replies)
        self.assertIn("Pi search reply", replies)
        self.assertEqual(
            self.bus.get(codex_request["message_id"])["status"],
            "replied",
        )
        self.assertEqual(
            self.bus.get(pi_request["message_id"])["status"],
            "replied",
        )

    def test_skill_description_exposes_cross_agent_trigger(self):
        skill = (PROJECT_ROOT / "skills" / "agent-relay" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        frontmatter = skill.split("---", 2)[1]
        self.assertIn("跨 Agent 会话协作", frontmatter)
        self.assertIn("独立只读调查", frontmatter)
        self.assertIn("已注册", frontmatter)

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
        message = self.send(
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
