"""End-to-end behaviour for the automation notice/consent wiring.

These use the real policy module and the real consent module; only the OS
notification call is stubbed, so the classification table below is what the
Hook would actually decide on the user's machine.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import automation_consent, browser_interaction_policy  # noqa: E402
from scripts import desktop_notify  # noqa: E402


def classify(command):
    return browser_interaction_policy.interaction_request(
        "exec_command", {"cmd": command},
    )


class AutomationClassificationTests(unittest.TestCase):
    """The table the user actually cares about, run against the real policy."""

    def test_genuine_automation_is_announced(self):
        rows = {
            "python app.py &": "background_process",
            "nohup python server.py > out.log 2>&1 &": "background_process",
            "setsid python worker.py": "background_process",
            "open -a Slack": "foreground_window",
            "open -a Slack --background": "background_window",
            "python browse.py screenshot https://example.com": "browser_automation",
            "cliclick c:100,200": "direct_pointer",
            "xdotool click 1": "direct_pointer",
        }
        for command, expected in rows.items():
            with self.subTest(command=command):
                request = classify(command)
                self.assertIsNotNone(request, command)
                self.assertEqual(request["kind"], expected, command)
                self.assertTrue(request["notice"], command)

    def test_ordinary_work_is_never_interrupted(self):
        commands = [
            "pytest -q",
            "git status",
            "python script.py",
            "echo hi 2>&1",
            "pytest > result.txt",
            "echo hi >> log.txt",
            "ps aux | grep python",
            "cd /tmp && ls",
            "test -f a || echo missing",
            "rg browse.py skills/catalog",
            "rg cliclick scripts/",
            "cat browser_interaction_policy.py",
            "grep -n open scripts/app_close_policy.py",
            "printf 'xdotool click 1\\n'",
            "jq '.open' config.json",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(classify(command), command)

    def test_keywords_in_heredoc_or_comment_do_not_gate(self):
        heredoc = "python - <<'PY'\nprint('cliclick c:1,2')\nPY"
        self.assertIsNone(classify(heredoc))
        commented = "# open -a Slack\nls -la"
        self.assertIsNone(classify(commented))

    def test_shell_dash_c_still_gates(self):
        request = classify("sh -c 'open -a Slack'")
        self.assertIsNotNone(request)
        self.assertEqual(request["kind"], "foreground_window")

    def test_shell_wrapper_only_gates_what_the_inner_shell_would_run(self):
        gated = {
            "sh -c 'open -a Slack'": "foreground_window",
            "bash -c 'python app.py &'": "background_process",
            "bash -lc 'open -g -a Slack'": "background_window",
            "bash -ec 'python app.py &'": "background_process",
            "bash --login -c 'open -a Slack'": "foreground_window",
        }
        for command, expected in gated.items():
            with self.subTest(command=command):
                request = classify(command)
                self.assertIsNotNone(request, command)
                self.assertEqual(request["kind"], expected, command)
        harmless = [
            "sh -c 'git status'",
            "bash -c 'ls -la'",
            "bash -lc 'pytest -q'",
            "sh -c 'echo hi 2>&1'",
            "python -c 'print(1)'",
        ]
        for command in harmless:
            with self.subTest(command=command):
                self.assertIsNone(classify(command), command)

    def test_foreground_and_background_are_distinguished_for_notice(self):
        foreground = classify("open -a Slack")
        background = classify("open -a Slack --background")
        self.assertTrue(foreground["affects_user"])
        self.assertFalse(background["affects_user"])
        self.assertIn("前台", automation_consent.announcement(foreground))
        self.assertIn("后台", automation_consent.announcement(background))


class AutomationConsentFlowTests(unittest.TestCase):
    """The granularity rules the user described, applied end to end."""

    def setUp(self):
        self.state = {"agent_id": "codex"}

    def test_first_launch_asks_then_the_same_session_reuses_it(self):
        request = classify("python app.py &")
        target = request["target"]
        self.assertEqual(target, "")
        # A background process has no program name to remember, so it must
        # never be silently reused; only named programs qualify.
        self.assertTrue(automation_consent.should_ask(
            self.state, "codex", request["kind"], target,
            "2026-11-25T10:00:00", "s1",
        ))

    def test_named_program_declared_once_does_not_ask_twice(self):
        automation_consent.record_declaration(
            self.state, "codex", "background_window", "slack",
            "2026-11-25T10:00:00", "s1",
        )
        self.assertFalse(automation_consent.should_ask(
            self.state, "codex", "background_window", "slack",
            "2026-11-25T10:20:00", "s1",
        ))

    def test_switching_feature_still_notifies_even_when_scope_is_persistent(self):
        automation_consent.record_declaration(
            self.state, "codex", "background_window", "slack",
            "2026-11-25T10:00:00", "s1", persistent=True,
        )
        self.assertFalse(automation_consent.should_ask(
            self.state, "codex", "background_window", "slack",
            "2026-11-29T09:00:00", "s2",
        ))
        self.assertTrue(automation_consent.must_announce(
            self.state, "codex", "background_window", "slack",
            "2026-11-29T09:00:00", "s2",
        ))

    def test_disabling_notifications_silences_the_popup_not_the_gate(self):
        config = {"notifications": {"enabled": False}}
        self.assertFalse(automation_consent.notifications_enabled(config))
        with mock.patch.object(desktop_notify.subprocess, "run") as run:
            sent = desktop_notify.notify(
                "AgentRelay", "即将打开 Slack", enabled=False,
            )
        self.assertFalse(sent)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
