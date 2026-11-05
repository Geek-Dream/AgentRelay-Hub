import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import approval_policy as policy


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_default_standard_allows_clear_small_change(self):
        confirm, _ = policy.should_confirm("修改右上角导出按钮颜色为绿色", "standard", 1, 1)
        self.assertFalse(confirm)

    def test_strict_requires_confirmation_for_every_write(self):
        for prompt in ("新增输入框", "修改颜色", "删除旧样式"):
            self.assertTrue(policy.should_confirm(prompt, "strict", 1, 1)[0])
        self.assertFalse(policy.should_confirm("为什么这个按钮不显示？", "strict")[0])

    def test_relaxed_only_requires_large_or_shared_scope(self):
        self.assertFalse(policy.should_confirm("修改多个代码文件的排版", "relaxed", 3, 2)[0])
        self.assertTrue(policy.should_confirm("完整重构整个项目", "relaxed")[0])
        self.assertTrue(policy.should_confirm("修改公共组件的滚动条", "relaxed")[0])

    def test_standard_requires_multiple_files_domains_or_ambiguity(self):
        self.assertTrue(policy.should_confirm("修改这两个文件的样式", "standard", 2, 1)[0])
        self.assertTrue(policy.should_confirm("同时修改前端和后端", "standard", 1, 2)[0])
        self.assertTrue(policy.should_confirm("修改页面并参考当前项目风格自由发挥", "standard", 1, 1)[0])

    def test_pending_contract_cannot_be_bypassed_by_relaxed_mode(self):
        contract = {"approved": False, "target_scope": "A 模块滚动条"}
        self.assertTrue(policy.should_confirm("修改滚动条", "relaxed", contract=contract)[0])

    def test_policy_persists_and_cli_works_without_project_files(self):
        self.assertEqual(policy.load_policy(self.home)["mode"], "standard")
        policy.save_policy(self.home, "strict")
        self.assertEqual(policy.load_policy(self.home)["mode"], "strict")
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"),
                                 "set", "relaxed", "--codex-home", str(self.home)],
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["mode"], "relaxed")

    def test_numeric_menu_describes_and_persists_each_mode(self):
        for number, mode in policy.MENU_MODES.items():
            self.assertIn(number, policy.render_menu(self.home))
            self.assertIn(policy.LABELS[mode], policy.render_menu(self.home))
            result = policy.apply_selection(self.home, number)
            self.assertEqual(result, {"version": "1", "mode": mode})
            self.assertEqual(policy.load_policy(self.home), result)
        for number in ("0", "4", "abc", ""):
            with self.assertRaises(ValueError):
                policy.apply_selection(self.home, number)

    def test_menu_cli_defaults_to_menu_and_accepts_a_number(self):
        shown = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"),
                                "--codex-home", str(self.home)],
                               text=True, capture_output=True, check=True)
        self.assertIn("[1]", shown.stdout)
        self.assertIn("默认", shown.stdout)
        self.assertIn("危险", shown.stdout)
        selected = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"),
                                   "2", "--codex-home", str(self.home)],
                                  text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(selected.stdout)["mode"], "standard")
        invalid = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"),
                                  "4", "--codex-home", str(self.home)],
                                 text=True, capture_output=True, check=False)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("1、2、3", invalid.stderr)

    def test_visible_card_is_only_marked_once_per_request(self):
        gate = {}
        self.assertTrue(policy.mark_card_shown(gate, "hash-1"))
        self.assertFalse(policy.mark_card_shown(gate, "hash-1"))
        self.assertTrue(policy.mark_card_shown(gate, "hash-2"))


if __name__ == "__main__":
    unittest.main()
