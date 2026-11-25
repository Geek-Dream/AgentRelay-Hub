import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BROWSE = (
    PROJECT_ROOT
    / "skills"
    / "catalog"
    / "agentrelay"
    / "browser-automatic"
    / "scripts"
    / "browse.py"
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BrowserInteractionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = load_module(
            "browser_interaction_policy_test",
            PROJECT_ROOT / "scripts" / "browser_interaction_policy.py",
        )
        cls.browse = load_module("browse_policy_test", BROWSE)

    def test_browser_command_requires_an_explicit_mode(self):
        request = self.policy.interaction_request(
            "exec_command",
            {"cmd": "python browse.py screenshot https://example.com"},
        )
        self.assertIsNotNone(request)
        self.assertEqual(request["kind"], "browser_automation")
        self.assertEqual(request["mode"], "")

    def test_background_and_foreground_are_preserved_explicitly(self):
        background = self.policy.interaction_request(
            "exec_command",
            {
                "cmd": (
                    "python browse.py --interaction-mode background "
                    "screenshot https://example.com"
                )
            },
        )
        foreground = self.policy.interaction_request(
            "exec_command",
            {
                "cmd": (
                    "python browse.py --interaction-mode foreground "
                    "screenshot https://example.com"
                )
            },
        )
        self.assertEqual(background["mode"], "background")
        self.assertEqual(foreground["mode"], "foreground")

    def test_direct_pointer_is_foreground_only_and_never_defaults(self):
        request = self.policy.interaction_request(
            "computer",
            {"action": "click", "x": 20, "y": 30},
        )
        self.assertEqual(request["kind"], "direct_pointer")
        self.assertEqual(request["mode"], "")
        self.assertFalse(request["background_allowed"])
        self.assertEqual(request["interaction_count"], 1)

    def test_workflow_fingerprint_changes_with_step_file_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            steps = workspace / "steps.json"
            steps.write_text(
                json.dumps([
                    {"action": "goto", "url": "https://example.com"},
                    {"action": "click", "selector": "#one"},
                ]),
                encoding="utf-8",
            )
            command = (
                "python browse.py --interaction-mode background "
                "run steps.json"
            )
            first = self.policy.interaction_request(
                "exec_command",
                {"cmd": command, "cwd": str(workspace)},
            )
            steps.write_text(
                json.dumps([
                    {"action": "goto", "url": "https://example.com"},
                    {"action": "click", "selector": "#two"},
                ]),
                encoding="utf-8",
            )
            second = self.policy.interaction_request(
                "exec_command",
                {"cmd": command, "cwd": str(workspace)},
            )
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["interaction_count"], 1)
        self.assertEqual(second["interaction_count"], 1)

    def test_multiple_page_interactions_are_counted_for_split_workflows(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            steps = workspace / "steps.json"
            steps.write_text(
                json.dumps([
                    {"action": "click", "selector": "#one"},
                    {"action": "click", "selector": "#two"},
                ]),
                encoding="utf-8",
            )
            request = self.policy.interaction_request(
                "exec_command",
                {
                    "cmd": "python browse.py run steps.json",
                    "cwd": str(workspace),
                },
            )
        self.assertEqual(request["interaction_count"], 2)

    def test_plain_shell_and_api_calls_do_not_request_interaction(self):
        self.assertIsNone(self.policy.interaction_request(
            "exec_command",
            {"cmd": "pytest -q tests/test_browser_interaction_policy.py"},
        ))
        self.assertIsNone(self.policy.interaction_request(
            "exec_command",
            {"cmd": "rg browse.py skills/catalog"},
        ))
        self.assertIsNone(self.policy.interaction_request(
            "exec_command",
            {
                "cmd": (
                    "*** Begin Patch\n"
                    "*** Update File: skills/browser-automatic/scripts/browse.py\n"
                    "*** End Patch"
                )
            },
        ))
        self.assertIsNone(self.policy.interaction_request(
            "api_call",
            {"url": "https://api.example.com/v1/chat", "method": "POST"},
        ))

    def test_browse_runner_rejects_multi_interaction_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            steps = Path(temporary) / "steps.json"
            steps.write_text(
                json.dumps([
                    {"action": "click", "selector": "#one"},
                    {"action": "click", "selector": "#two"},
                ]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "at most one page interaction"):
                self.browse._load_steps(steps)

    def test_browse_launch_defaults_background_and_only_foreground_is_headed(self):
        playwright = mock.MagicMock()
        self.browse._launch(playwright, "background")
        playwright.chromium.launch.assert_called_with(headless=True)
        self.browse._launch(playwright, "foreground")
        playwright.chromium.launch.assert_called_with(headless=False)


if __name__ == "__main__":
    unittest.main()
