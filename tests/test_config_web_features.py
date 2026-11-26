import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config_web import build_config_page  # noqa: E402


def sample_config():
    return {
        "fusion": {"enabled": True, "tool_mode": "readonly", "provider_id": ""},
        "fusion_state": {"delegable": False, "reason": "FUSION_PROVIDER_NOT_CONFIGURED"},
        "web_assist": {"enabled": False},
        "commander": {"enabled": False, "terminal_path": "codex", "max_agents": 5},
        "web_providers": [],
        "local_providers": [],
        "api_providers": [],
        "api_router": {"port": 15731},
        "notifications": {"enabled": True},
        "executables": [],
    }


class TerminalPathTests(unittest.TestCase):
    def test_terminal_path_is_a_detected_agent_picker(self):
        config = sample_config()
        config["executables"] = [
            {"agent": "codex", "label": "Codex CLI", "path": "/usr/local/bin/codex", "ok": True},
            {"agent": "claude", "label": "Claude Code", "path": "/usr/local/bin/claude", "ok": True},
            {"agent": "opencode", "label": "OpenCode", "path": "", "ok": False},
        ]
        page = build_config_page(config)
        self.assertIn('id="terminal-agent"', page)
        self.assertIn('id="terminal-path"', page)
        self.assertIn("浏览…", page)
        self.assertIn("/api/pick-executable", page)
        self.assertIn("renderTerminalAgents", page)
        self.assertIn('"executables"', page)
        self.assertIn("Codex CLI", page)
        self.assertIn("Claude Code", page)
        self.assertIn("OpenCode", page)

    def test_manual_path_entry_stays_available(self):
        page = build_config_page(sample_config())
        self.assertIn("手动填写路径", page)


class AutomationSwitchTests(unittest.TestCase):
    def test_notification_switch_exists_on_the_web_page(self):
        page = build_config_page(sample_config())
        self.assertIn('id="notifications-enabled"', page)
        self.assertIn("可选的系统桌面提醒", page)
        self.assertIn("关闭提醒不会关闭审批", page)
        self.assertIn("/agent-approval notify", page)

    def test_switch_defaults_to_disabled_when_unspecified(self):
        config = sample_config()
        config.pop("notifications")
        page = build_config_page(config)
        self.assertIn('id="notifications-enabled"', page)
        self.assertIn("const notificationsOn=(state.notifications||{}).enabled===true;", page)


class BusyStateTests(unittest.TestCase):
    def test_async_actions_use_the_shared_busy_helper(self):
        page = build_config_page(sample_config())
        for call in (
            "withBusy(",
            "testProvider",
            "scanLocal",
            "applyTargets",
            "saveProvider",
            "refreshMessagePeers",
        ):
            self.assertIn(call, page)
        self.assertIn("prefers-reduced-motion", page)


class ModelFetchTests(unittest.TestCase):
    def test_api_form_offers_a_real_model_fetch(self):
        page = build_config_page(sample_config())
        self.assertIn("获取模型", page)
        self.assertIn("/api/models", page)


class CollaborationTabTests(unittest.TestCase):
    def test_collaboration_tab_can_assign_a_task(self):
        page = build_config_page(sample_config())
        self.assertIn('data-tab="collab"', page)
        self.assertIn('<div id="collab" class="view">', page)
        self.assertIn("/api/messages/send", page)
        self.assertIn("指派任务", page)


if __name__ == "__main__":
    unittest.main()
