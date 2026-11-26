import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import automation_consent  # noqa: E402
from scripts import config_manager  # noqa: E402


class AutomationConsentTests(unittest.TestCase):
    def test_system_notifications_are_opt_in_not_the_approval_channel(self):
        self.assertFalse(automation_consent.notifications_enabled({}))
        self.assertTrue(automation_consent.notifications_enabled(
            {"notifications": {"enabled": True}},
        ))

    def test_explicit_disable_is_respected(self):
        config = {"notifications": {"enabled": False}}
        self.assertFalse(automation_consent.notifications_enabled(config))

    def test_config_defaults_disable_optional_system_popups(self):
        config = config_manager.default_config()
        self.assertFalse(automation_consent.notifications_enabled(config))
        cipher = mock.Mock()
        cipher.encrypt.side_effect = lambda payload: payload
        config.pop("notifications")
        with mock.patch.object(config_manager, "_fernet", return_value=cipher):
            with mock.patch.object(config_manager, "_secure_write") as write:
                config_manager.save_config(config)
        saved = write.call_args.args[1].decode("utf-8")
        self.assertIn('"enabled": false', saved)

    def test_announcement_always_states_what_will_happen(self):
        request = {
            "kind": "foreground_window",
            "target": "Slack",
            "notice": "即将打开「Slack」，窗口会跳到前台并可能打断你当前操作。",
        }
        text = automation_consent.announcement(request)
        self.assertIn("Slack", text)
        self.assertIn("前台", text)

    def test_announcement_is_built_even_without_a_target(self):
        request = {
            "kind": "background_process",
            "target": "",
            "notice": "即将在后台常驻运行一个进程，它会一直占用资源直到被关闭。",
        }
        text = automation_consent.announcement(request)
        self.assertIn("后台", text)
        self.assertTrue(text)

    def test_recent_declaration_skips_reasking_within_one_session(self):
        state = {}
        first = automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", session="session-1",
        )
        self.assertTrue(first)
        automation_consent.record_declaration(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", "session-1",
        )
        again = automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:30:00", session="session-1",
        )
        self.assertFalse(again)

    def test_a_declaration_without_a_session_does_not_leak_into_another(self):
        state = {}
        automation_consent.record_declaration(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", "session-1",
        )
        asked = automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:30:00",
        )
        self.assertTrue(asked)

    def test_a_new_session_asks_again(self):
        state = {}
        automation_consent.record_declaration(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", "session-1",
        )
        asked = automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T11:00:00", session="session-2",
        )
        self.assertTrue(asked)

    def test_a_different_target_asks_again(self):
        state = {}
        automation_consent.record_declaration(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", "session-1",
        )
        asked = automation_consent.should_ask(
            state, "codex", "foreground_window", "Chrome",
            "2026-11-25T10:30:00", session="session-1",
        )
        self.assertTrue(asked)

    def test_a_different_kind_asks_again(self):
        state = {}
        automation_consent.record_declaration(
            state, "codex", "background_window", "Slack",
            "2026-11-25T10:00:00", "session-1",
        )
        asked = automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:30:00", session="session-1",
        )
        self.assertTrue(asked)

    def test_declaration_expires_after_the_reuse_window(self):
        state = {}
        automation_consent.record_declaration(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", "session-1",
        )
        asked = automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T12:30:00", session="session-1",
        )
        self.assertTrue(asked)

    def test_revocation_forces_asking_again(self):
        state = {}
        automation_consent.record_declaration(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", "session-1",
        )
        automation_consent.revoke(state, "codex", "foreground_window", "Slack")
        asked = automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:05:00", session="session-1",
        )
        self.assertTrue(asked)

    def test_persistent_scope_still_announces_on_new_feature(self):
        state = {}
        automation_consent.record_declaration(
            state, "codex", "background_window", "slack",
            "2026-11-25T10:00:00", "session-1", persistent=True,
        )
        # Persistent means "do not ask again" for that program, but a new
        # session/feature must still notify the user before acting.
        self.assertFalse(automation_consent.should_ask(
            state, "codex", "background_window", "slack",
            "2026-11-27T10:00:00", session="session-9",
        ))
        self.assertTrue(automation_consent.must_announce(
            state, "codex", "background_window", "slack",
            "2026-11-27T10:00:00", session="session-9",
        ))

    def test_announcement_is_not_repeated_within_the_same_session(self):
        state = {}
        automation_consent.mark_announced(
            state, "codex", "background_window", "slack",
            "2026-11-25T10:00:00", session="session-1",
        )
        self.assertFalse(automation_consent.must_announce(
            state, "codex", "background_window", "slack",
            "2026-11-25T10:00:05", session="session-1",
        ))

    def test_announcing_an_operation_never_counts_as_consent(self):
        state = {}
        automation_consent.mark_announced(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:00", session="session-1",
        )
        # Telling the user is not the same as the user agreeing.
        self.assertTrue(automation_consent.should_ask(
            state, "codex", "foreground_window", "Slack",
            "2026-11-25T10:00:05", session="session-1",
        ))
    def test_mode_retry_normalizes_only_browser_display_options(self):
        from scripts import browser_interaction_policy as policy

        original = {"command": 'browse.py scrape https://example.com'}
        changed = {
            "command": 'browse.py --interaction-mode foreground scrape '
                       '"foo --interaction-mode foreground bar"',
        }
        self.assertNotEqual(
            policy.interaction_request("Bash", original)["retry_request_id"],
            policy.interaction_request("Bash", changed)["retry_request_id"],
        )
        retry = {
            "command": "browse.py --interaction-mode foreground scrape https://example.com",
        }
        self.assertEqual(
            policy.interaction_request("Bash", original)["retry_request_id"],
            policy.interaction_request("Bash", retry)["retry_request_id"],
        )

    def test_mode_retry_supports_headless_tool_options(self):
        from scripts import browser_interaction_policy as policy

        original = {"action": "navigate", "url": "https://example.com"}
        retry = {**original, "headless": True}
        self.assertEqual(
            policy.interaction_request("playwright_browser", original)["retry_request_id"],
            policy.interaction_request("playwright_browser", retry)["retry_request_id"],
        )

    def test_notification_message_names_the_real_encrypted_config(self):
        import tempfile
        from scripts import approval_policy

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.object(
                approval_policy, "_notification_enabled", return_value=False,
            ), mock.patch.dict(sys.modules, {"config_manager": config_manager}):
                result = approval_policy.command_request(
                    "/agent-approval notify off", home,
                )
            self.assertIn(str(config_manager.config_path(home)), result["message"])
            self.assertNotIn("notifications.json", result["message"])


if __name__ == "__main__":
    unittest.main()
