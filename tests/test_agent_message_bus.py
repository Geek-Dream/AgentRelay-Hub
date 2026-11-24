import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_message_bus as bus_module  # noqa: E402


class MessageBusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-message-bus-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.bus = bus_module.MessageBus(
            Path(self.temp.name) / "messages.sqlite3",
            now=lambda: self.now,
        )
        self.register("codex:one")
        self.register("pi:two")

    def register(self, agent, **kwargs):
        host, session = agent.split(":", 1)
        values = {
            "session_id": session,
            "workspace": str(PROJECT_ROOT),
            "name": kwargs.pop("name", session),
        }
        values.update(kwargs)
        return self.bus.register(agent, **values)

    def send(self, **kwargs):
        values = {
            "sender": "codex:one",
            "recipient": "pi:two",
            "body": "Please review the schema",
            "review_action": "new",
            "review_reason": "Independent schema review is needed",
            "push": False,
        }
        values.update(kwargs)
        if values.get("reply_to"):
            values.pop("review_action", None)
            values.pop("review_reason", None)
        return self.bus.send(**values)

    def test_cross_host_message_and_reply_preserve_task_correlation(self):
        original = self.send(kind="task")
        self.assertEqual(original["status"], "queued")
        self.assertEqual(original["task_id"], original["message_id"])

        incoming = self.bus.inbox("pi:two")
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["status"], "delivered")
        envelope = json.loads(incoming[0]["envelope"])
        self.assertEqual(envelope["from"], "codex:one")
        self.assertEqual(envelope["body"], "Please review the schema")
        self.assertTrue(envelope["handling"]["not_user_authorization"])
        self.assertTrue(envelope["handling"]["keep_existing_permissions"])

        self.now += 1
        reply = self.send(
            sender="pi:two",
            recipient="",
            reply_to=original["message_id"],
            body="Review complete",
            kind="reply",
        )
        self.assertEqual(reply["task_id"], original["task_id"])
        self.assertEqual(reply["reply_to"], original["message_id"])
        self.assertEqual(self.bus.get(original["message_id"])["status"], "replied")
        self.assertEqual(self.bus.inbox("codex:one")[0]["body"], "Review complete")
        self.assertEqual(
            self.bus.task_history("codex:one")[0]["status"],
            "replied",
        )

    def test_non_reply_send_requires_an_explicit_necessity_review(self):
        with self.assertRaisesRegex(bus_module.MessageBusError, "review_action"):
            self.bus.send("codex:one", "pi:two", "Unreviewed", push=False)
        with self.assertRaisesRegex(bus_module.MessageBusError, "review_reason"):
            self.bus.send(
                "codex:one",
                "pi:two",
                "Unreviewed",
                review_action="new",
                push=False,
            )
        self.assertEqual(self.bus.inbox("pi:two", peek=True), [])

    def test_history_supports_skip_and_follow_up_without_losing_correlation(self):
        original = self.send(task_id="task-config", body="Review message config")
        history = self.bus.task_history("codex:one", recipient="pi:two")
        self.assertEqual(history[0]["task_id"], "task-config")
        self.assertEqual(history[0]["status"], "active")
        self.assertEqual(history[0]["objective"], "Review message config")

        self.now += 1
        skipped = self.send(
            body="Check why message config cannot connect",
            related_task_id="task-config",
            review_action="skip",
            review_reason="The same config review is already active",
        )
        self.assertEqual(skipped["status"], "skipped")
        self.assertTrue(skipped["skipped"])
        self.assertEqual(len(self.bus.inbox("pi:two", peek=True)), 1)

        self.now += 1
        follow_up = self.send(
            body="Additional schema logs are available",
            related_task_id="task-config",
            review_action="follow_up",
            review_reason="New logs supplement the active review",
        )
        self.assertEqual(follow_up["task_id"], original["task_id"])
        self.assertEqual(len(self.bus.inbox("pi:two", peek=True)), 2)
        history = self.bus.task_history("codex:one")
        self.assertEqual(history[0]["message_count"], 2)
        self.assertEqual(history[0]["reviews"][0]["action"], "follow_up")

    def test_same_new_task_is_reserved_atomically_across_connections(self):
        buses = [
            bus_module.MessageBus(self.bus.path, now=lambda: self.now)
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)

        def reserve(bus):
            barrier.wait(timeout=5)
            return bus.send(
                "codex:one",
                "pi:two",
                "Review the same task",
                task_id="shared-task",
                review_action="new",
                review_reason="One owner is needed for this review",
                push=False,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, buses))
        self.assertEqual(sum(not item.get("duplicate", False) for item in results), 1)
        self.assertEqual(sum(bool(item.get("duplicate")) for item in results), 1)
        self.assertEqual(len(self.bus.inbox("pi:two", peek=True)), 1)

    def test_idempotency_key_returns_original_without_duplicating(self):
        first = self.send(idempotency_key="request-42")
        second = self.send(body="changed body", idempotency_key="request-42")
        self.assertEqual(second["message_id"], first["message_id"])
        self.assertEqual(second["body"], first["body"])
        self.assertEqual(len(self.bus.inbox("pi:two")), 1)

    def test_expired_message_is_never_delivered(self):
        message = self.send(ttl=5)
        self.now += 6
        self.assertEqual(self.bus.inbox("pi:two"), [])
        self.assertEqual(self.bus.get(message["message_id"])["status"], "expired")

    def test_when_idle_waits_for_busy_recipient(self):
        self.bus.heartbeat("pi:two", state="busy")
        message = self.send(delivery="when_idle")
        self.assertEqual(self.bus.inbox("pi:two"), [])
        self.assertEqual(self.bus.get(message["message_id"])["status"], "queued")

        self.bus.heartbeat("pi:two", state="idle")
        self.assertEqual(len(self.bus.inbox("pi:two")), 1)

    def test_safe_boundary_message_can_arrive_while_busy(self):
        self.bus.heartbeat("pi:two", state="busy")
        self.send(delivery="safe_boundary")
        self.assertEqual(len(self.bus.inbox("pi:two")), 1)

    def test_peek_does_not_acknowledge_and_wrong_agent_cannot_ack(self):
        message = self.send()
        self.assertEqual(self.bus.inbox("pi:two", peek=True)[0]["status"], "queued")
        with self.assertRaises(bus_module.MessageBusError):
            self.bus.ack("codex:one", message["message_id"], "read")
        self.bus.inbox("pi:two")
        acknowledged = self.bus.ack("pi:two", message["message_id"], "read")
        self.assertEqual(acknowledged["status"], "read")
        self.assertEqual(self.bus.inbox("pi:two"), [])

    def test_reply_chain_has_a_hard_hop_limit(self):
        parent = self.send(max_hops=1)
        reply = self.send(
            sender="pi:two",
            recipient="codex:one",
            reply_to=parent["message_id"],
            kind="reply",
        )
        with self.assertRaisesRegex(bus_module.MessageBusError, "hop"):
            self.send(
                sender="codex:one",
                recipient="pi:two",
                reply_to=reply["message_id"],
                kind="reply",
            )

    def test_reply_cannot_outlive_parent_task(self):
        parent = self.send(ttl=20)
        self.now += 5
        reply = self.send(
            sender="pi:two",
            recipient="codex:one",
            reply_to=parent["message_id"],
            ttl=3600,
        )
        self.assertLessEqual(reply["expires_at"], parent["expires_at"])

    def test_unique_name_resolution_rejects_ambiguity(self):
        self.register("opencode:three", name="worker")
        self.register("claude:four", name="worker")
        with self.assertRaisesRegex(bus_module.MessageBusError, "ambiguous"):
            self.send(recipient="worker")

    def test_private_endpoint_tokens_never_appear_in_listing(self):
        self.register(
            "claude:private",
            adapter="claude_socket",
            endpoint={"socket_path": "/tmp/test.sock", "token": "secret-token"},
        )
        listing = json.dumps(self.bus.list_agents(include_offline=True))
        self.assertNotIn("secret-token", listing)

    def test_database_is_private_and_uses_wal(self):
        mode = self.bus.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        with sqlite3.connect(self.bus.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_cli_round_trip(self):
        env = dict(os.environ)
        env["AGENTRELAY_MESSAGE_DB"] = str(self.bus.path)
        command = [
            sys.executable,
            str(SCRIPTS / "agent_message_bus.py"),
            "list",
            "--json",
        ]
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        addresses = {item["address"] for item in json.loads(result.stdout)["agents"]}
        self.assertIn("codex:one", addresses)
        self.assertIn("pi:two", addresses)

    def test_cli_history_and_review_actions(self):
        env = dict(os.environ)
        env["AGENTRELAY_MESSAGE_DB"] = str(self.bus.path)

        def run(*args):
            return subprocess.run(
                [sys.executable, str(SCRIPTS / "agent_message_bus.py"), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        sent = run(
            "send", "--from", "codex:one", "--to", "pi:two",
            "--body", "Review the CLI contract", "--task-id", "cli-task",
            "--review-action", "new", "--review-reason", "Independent CLI review",
            "--no-push",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        history = run("history", "--agent", "codex:one", "--to", "pi:two")
        self.assertEqual(history.returncode, 0, history.stderr)
        tasks = json.loads(history.stdout)["tasks"]
        self.assertEqual(tasks[0]["task_id"], "cli-task")
        self.assertEqual(tasks[0]["reviews"][0]["action"], "new")

        skipped = run(
            "send", "--from", "codex:one", "--to", "pi:two",
            "--body", "Check the CLI contract again", "--related-task-id", "cli-task",
            "--review-action", "skip", "--review-reason", "Already being reviewed",
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertEqual(json.loads(skipped.stdout)["status"], "skipped")
        self.assertEqual(len(self.bus.inbox("pi:two", peek=True)), 1)

    def test_existing_message_history_is_backfilled_for_follow_up_review(self):
        original = self.send(task_id="legacy-task", body="Existing task")
        with sqlite3.connect(self.bus.path) as connection:
            connection.execute("DELETE FROM collaboration_tasks")
        reopened = bus_module.MessageBus(self.bus.path, now=lambda: self.now)
        history = reopened.task_history("codex:one")
        self.assertEqual(history[0]["task_id"], original["task_id"])
        self.assertEqual(history[0]["objective"], "Existing task")
        follow_up = reopened.send(
            "codex:one",
            "pi:two",
            "New information for the existing task",
            related_task_id="legacy-task",
            review_action="follow_up",
            review_reason="New information is available",
            push=False,
        )
        self.assertEqual(follow_up["task_id"], original["task_id"])

    def test_cli_can_derive_a_safe_host_session_address(self):
        env = dict(os.environ)
        env["AGENTRELAY_MESSAGE_DB"] = str(self.bus.path)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "agent_message_bus.py"),
                "register",
                "--host", "pi",
                "--session-id", "session/with spaces",
                "--name", "pi-worker",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        registered = json.loads(result.stdout)
        self.assertTrue(registered["address"].startswith("pi:session-with-spaces-"))
        self.assertNotIn("pi:sender", registered["address"])


if __name__ == "__main__":
    unittest.main()
