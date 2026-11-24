import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agent_relay  # noqa: E402
import config_manager  # noqa: E402
from orchestrator_dispatcher import DeepSeekWebProvider  # noqa: E402


class WebAssistGateTests(unittest.TestCase):
    def web_config(self, enabled=False):
        config = config_manager.default_config()
        config["web_assist"] = {"enabled": enabled}
        config["web_providers"] = [{
            "id": "deepseek-web",
            "base_url": "https://chat.deepseek.com/",
            "enabled": True,
        }]
        return config

    def test_run_provider_stops_before_adapter_or_browser(self):
        with mock.patch.object(
            config_manager, "apply_config_to_environment",
            return_value=self.web_config(False),
        ), mock.patch.object(agent_relay.ProviderRegistry, "create") as create:
            with self.assertRaisesRegex(RuntimeError, "WEB_ASSIST_BETA_DISABLED"):
                agent_relay.run_provider(
                    "test", "default", provider_name="deepseek",
                )
        create.assert_not_called()

    def test_cli_does_not_check_login_or_print_browser_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["CODEX_HOME"] = directory
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "agent_relay.py"),
                    "test",
                    "--provider",
                    "deepseek",
                ],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("WEB_ASSIST_BETA_DISABLED", result.stdout)
        self.assertNotIn("未检测到当前 Provider", result.stdout)
        self.assertNotIn("AgentRelay 在线专家模型中继", result.stdout)

    def test_legacy_dispatcher_availability_requires_beta(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "skills" / "agent-relay" / "agent_relay_login_state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}", encoding="utf-8")
            config_manager.save_config(self.web_config(False), home)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertFalse(DeepSeekWebProvider().check_available())
                config_manager.save_config(self.web_config(True), home)
                self.assertTrue(DeepSeekWebProvider().check_available())


if __name__ == "__main__":
    unittest.main()
