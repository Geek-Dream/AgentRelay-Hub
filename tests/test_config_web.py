import json
import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from config_web import build_config_page  # noqa: E402


class FusionConfigWebTests(unittest.TestCase):
    def page_config(self):
        return {
            "fusion": {
                "enabled": True,
                "provider_id": "",
                "tool_mode": "readonly",
                "timeout": 30,
                "max_steps": 8,
            },
            "fusion_state": {
                "enabled": True,
                "provider_id": "",
                "tool_mode": "readonly",
                "delegable": False,
                "reason": "FUSION_PROVIDER_NOT_CONFIGURED",
            },
            "commander": {
                "enabled": False,
                "terminal_path": "codex",
                "max_agents": 5,
            },
            "web_providers": [{
                "id": "deepseek-web",
                "name": "DeepSeek",
                "base_url": "https://chat.deepseek.com/",
                "enabled": True,
            }],
            "local_providers": [{
                "id": "qwen-local",
                "name": "Qwen",
                "endpoint": "http://127.0.0.1:11434/v1",
                "model": "qwen",
                "enabled": True,
            }],
            "api_providers": [],
            "api_router": {"port": 15731},
        }

    def test_fusion_tab_has_switch_provider_and_tool_mode_controls(self):
        page = build_config_page(self.page_config())
        self.assertIn('data-tab="fusion"', page)
        self.assertIn('<div id="fusion" class="view">', page)
        self.assertIn('id="fusion-enabled"', page)
        self.assertIn('id="fusion-provider"', page)
        self.assertIn('id="fusion-mode"', page)
        self.assertIn("/agent-fusion", page)

    def test_unconfigured_provider_state_is_visible_and_explained(self):
        page = build_config_page(self.page_config())
        self.assertIn("FUSION_PROVIDER_NOT_CONFIGURED", page)
        self.assertIn("已开启，但尚未选择模型", page)
        self.assertIn("当前不可调用", page)
        self.assertIn('id="fusion-overview"', page)

    def test_state_is_embedded_without_breaking_script_tags(self):
        config = self.page_config()
        config["web_providers"][0]["name"] = "</script><script>alert(1)</script>"
        page = build_config_page(config)
        config_json = re.search(
            r"const state=(\{.*?\});const \$=id=>document\.getElementById\(id\);",
            page,
        )
        self.assertIsNotNone(config_json)
        state = json.loads(config_json.group(1).replace("<\\/", "</"))
        self.assertEqual(
            state["fusion_state"]["reason"],
            "FUSION_PROVIDER_NOT_CONFIGURED",
        )
        self.assertNotIn("</script><script>alert(1)</script>", page)


if __name__ == "__main__":
    unittest.main()
