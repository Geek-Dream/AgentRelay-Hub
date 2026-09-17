import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from config_manager import (  # noqa: E402
    normalize_conversation,
    resolve_conversation_mode,
    save_config,
)


class ConversationModeTests(unittest.TestCase):
    """按用户定稿：混合=极速+专家合并；识图按模式独立；默认极速。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self._old_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)

    def tearDown(self):
        if self._old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old_codex_home

    def write(self, *providers):
        save_config({"web_providers": list(providers)}, self.home)

    def test_legacy_hybrid_flag_migrates_to_single_hybrid_mode(self):
        conversation = normalize_conversation(
            "deepseek-web", {"supports_hybrid": True, "supports_images": True})
        self.assertEqual(conversation["modes"], ["hybrid"])
        self.assertEqual(conversation["images"], {"hybrid": True})
        # 旧的读取方仍能用派生字段
        self.assertTrue(conversation["supports_hybrid"])
        self.assertTrue(conversation["supports_images"])
        self.assertFalse(conversation["supports_flash"])

    def test_images_are_tracked_per_mode(self):
        conversation = normalize_conversation(
            "kimi", {"modes": ["flash", "expert"],
                     "images": {"flash": True, "expert": False}})
        self.assertEqual(conversation["modes"], ["flash", "expert"])
        self.assertEqual(conversation["images"], {"flash": True, "expert": False})

    def test_flash_and_expert_default_to_flash(self):
        self.write({"id": "kimi", "url": "https://www.kimi.com/",
                    "conversation": {"modes": ["flash", "expert"],
                                     "images": {"flash": True, "expert": False}}})
        mode, note = resolve_conversation_mode(provider_id="kimi", home=self.home)
        self.assertEqual(mode, "flash")
        self.assertEqual(note, "")

    def test_second_attempt_without_images_escalates_to_expert(self):
        self.write({"id": "kimi", "url": "https://www.kimi.com/",
                    "conversation": {"modes": ["flash", "expert"],
                                     "images": {"flash": True, "expert": False}}})
        mode, note = resolve_conversation_mode(provider_id="kimi", attempt=2, home=self.home)
        self.assertEqual(mode, "expert")
        self.assertIn("专家", note)

    def test_second_attempt_with_images_stays_on_flash(self):
        self.write({"id": "kimi", "url": "https://www.kimi.com/",
                    "conversation": {"modes": ["flash", "expert"],
                                     "images": {"flash": True, "expert": False}}})
        mode, _ = resolve_conversation_mode(
            provider_id="kimi", attempt=2, has_images=True, home=self.home)
        self.assertEqual(mode, "flash")

    def test_expert_without_image_support_falls_back(self):
        self.write({"id": "kimi", "url": "https://www.kimi.com/",
                    "conversation": {"modes": ["flash", "expert"],
                                     "images": {"flash": True, "expert": False}}})
        mode, note = resolve_conversation_mode(
            provider_id="kimi", requested="expert", has_images=True, home=self.home)
        self.assertEqual(mode, "flash")
        self.assertIn("不支持图片", note)

    def test_hybrid_only_provider_without_images_reports_it(self):
        self.write({"id": "gpt-web", "url": "https://chatgpt.com/",
                    "conversation": {"modes": ["hybrid"], "images": {"hybrid": False}}})
        mode, note = resolve_conversation_mode(
            provider_id="gpt-web", has_images=True, home=self.home)
        self.assertEqual(mode, "hybrid")
        self.assertIn("不能转发图片", note)

    def test_explicit_user_request_wins(self):
        self.write({"id": "kimi", "url": "https://www.kimi.com/",
                    "conversation": {"modes": ["flash", "expert"],
                                     "images": {"flash": True, "expert": False}}})
        quick, _ = resolve_conversation_mode(provider_id="kimi", requested="1", home=self.home)
        expert, _ = resolve_conversation_mode(provider_id="kimi", requested="2", home=self.home)
        self.assertEqual((quick, expert), ("flash", "expert"))

    def test_requested_mode_not_enabled_falls_back_with_note(self):
        self.write({"id": "gpt-web", "url": "https://chatgpt.com/",
                    "conversation": {"modes": ["hybrid"], "images": {"hybrid": False}}})
        mode, note = resolve_conversation_mode(
            provider_id="gpt-web", requested="expert", home=self.home)
        self.assertEqual(mode, "hybrid")
        self.assertIn("没有启用", note)

    def test_provider_id_accepts_both_forms(self):
        self.write({"id": "deepseek-web", "url": "https://chat.deepseek.com/",
                    "conversation": {"modes": ["hybrid"], "images": {"hybrid": True}}})
        for provider_id in ("deepseek", "deepseek-web"):
            mode, _ = resolve_conversation_mode(provider_id=provider_id, home=self.home)
            self.assertEqual(mode, "hybrid")

    def test_unknown_provider_falls_back_to_hybrid(self):
        mode, _ = resolve_conversation_mode(provider_id="unknown-web", home=self.home)
        self.assertEqual(mode, "hybrid")


if __name__ == "__main__":
    unittest.main()
