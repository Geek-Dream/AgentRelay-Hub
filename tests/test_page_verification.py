import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import page_verification


class PageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.page = self.workspace / "index.html"
        self.page.write_text("<h1>page</h1>", encoding="utf-8")
        self.session = "page-session"

    def edit(self, path=None):
        return page_verification.record_page_edit(
            self.home, self.session, str(self.workspace), "Edit",
            {"file_path": str(path or self.page), "old_string": "a", "new_string": "b"},
        )

    def pass_evidence(self):
        screenshot = self.home / "desktop.png"
        screenshot.write_bytes(b"real-browser-image")
        state = page_verification.read_state(self.home, self.session)
        state["verification"] = {"status": "passed", "browser": "chromium",
                                  "screenshots": [str(screenshot)], "files": state["files"],
                                  "url": "file:///tmp/page.html"}
        page_verification.write_state(self.home, self.session, state)

    def test_only_page_write_tools_are_recorded(self):
        self.assertEqual(self.edit(), [str(self.page)])
        self.assertEqual(page_verification.record_page_edit(
            self.home, self.session, str(self.workspace), "Read", {"file_path": str(self.page)}), [])
        other = self.workspace / "module.py"
        other.write_text("pass", encoding="utf-8")
        self.assertEqual(self.edit(other), [])

    def test_missing_stale_and_failed_evidence_are_blocked(self):
        self.edit()
        self.assertIn("没有成功的真实浏览器检查", page_verification.verify_evidence(self.home, self.session))
        state = page_verification.read_state(self.home, self.session)
        state["verification"] = {"status": "failed", "browser": "chromium"}
        page_verification.write_state(self.home, self.session, state)
        self.assertIsNotNone(page_verification.verify_evidence(self.home, self.session))
        self.pass_evidence()
        self.assertIsNone(page_verification.verify_evidence(self.home, self.session))
        self.page.write_text("changed after browser check", encoding="utf-8")
        self.assertIn("又有修改", page_verification.verify_evidence(self.home, self.session))

    def test_current_evidence_requires_real_screenshot(self):
        self.edit()
        self.pass_evidence()
        state = page_verification.read_state(self.home, self.session)
        Path(state["verification"]["screenshots"][0]).unlink()
        self.assertIn("截图不存在", page_verification.verify_evidence(self.home, self.session))

    def test_non_page_and_honest_unverified_messages_do_not_claim_completion(self):
        self.assertFalse(page_verification.completion_claim("后端已完成，测试通过"))
        self.assertFalse(page_verification.completion_claim("页面已修改；未验证"))
        self.assertTrue(page_verification.completion_claim("配置中心页面已完成，检查通过"))
        self.assertIsNone(page_verification.verify_evidence(self.home, self.session))

    def test_bash_page_change_uses_worktree_delta(self):
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=self.workspace, check=True)
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "initial"], cwd=self.workspace, check=True)
        page_verification.record_command_start(self.home, self.session, str(self.workspace),
                                               "Bash", {"command": "python formatter.py"})
        self.page.write_text("<h1>changed</h1>", encoding="utf-8")
        changed = page_verification.record_page_edit(self.home, self.session, str(self.workspace),
                                                    "Bash", {"command": "python formatter.py"}, {})
        self.assertEqual(changed, [str(self.page)])
        page_verification.record_command_start(self.home, self.session, str(self.workspace),
                                               "Bash", {"command": "npm test"})
        unchanged = page_verification.record_page_edit(self.home, self.session, str(self.workspace),
                                                      "Bash", {"command": "npm test"}, {})
        self.assertEqual(unchanged, [])

    def test_other_page_cannot_use_config_page_evidence(self):
        self.edit()
        self.pass_evidence()
        state = page_verification.read_state(self.home, self.session)
        state["verification"]["url"] = ""
        page_verification.write_state(self.home, self.session, state)
        self.assertIn("--url", page_verification.verify_evidence(self.home, self.session))

    def test_config_page_template_counts_as_page_source(self):
        source = self.workspace / "config_web.py"
        source.write_text("PAGE = '<html></html>'", encoding="utf-8")
        self.assertEqual(self.edit(source), [str(source)])


class HookPageGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.page = self.workspace / "page.html"
        self.page.write_text("<h1>page</h1>", encoding="utf-8")
        self.session = "hook-page"

    def run_hook(self, event, **values):
        payload = {"hook_event_name": event, "session_id": self.session,
                   "cwd": str(self.workspace), **values}
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "hooks" / "agent_relay_hook.py")],
                                input=json.dumps(payload), text=True, capture_output=True,
                                env={**__import__("os").environ, "CODEX_HOME": str(self.home),
                                     "AGENTRELAY_TARGET": "claude"}, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def test_hook_blocks_false_page_completion_then_accepts_current_evidence(self):
        self.run_hook("PostToolUse", tool_name="Edit", tool_use_id="edit-1",
                      tool_input={"file_path": str(self.page)}, tool_response={"success": True})
        denied = self.run_hook("Stop", last_assistant_message="页面已完成，验证通过。")
        self.assertEqual(denied["decision"], "block")
        self.assertIn("页面验证门禁", denied["reason"])
        PageEvidenceTests.pass_evidence(self)
        allowed = self.run_hook("Stop", last_assistant_message="页面已完成，验证通过。")
        self.assertNotEqual(allowed.get("reason", ""), denied["reason"])
        self.assertNotIn("页面验证门禁", allowed.get("reason", ""))

    def test_installed_hook_layout_resolves_page_verifier(self):
        import hooks.agent_relay_tracker as tracker
        skill = self.home / "skills" / "agent-relay" / "scripts"
        skill.mkdir(parents=True)
        (skill / "page_verification.py").write_text(
            (PROJECT_ROOT / "scripts" / "page_verification.py").read_text(encoding="utf-8"), encoding="utf-8")
        old_home = tracker.CODEX_DIR
        self.addCleanup(setattr, tracker, "CODEX_DIR", old_home)
        tracker.CODEX_DIR = self.home
        self.run_hook("PostToolUse", tool_name="Edit", tool_use_id="installed-edit",
                      tool_input={"file_path": str(self.page)}, tool_response={"success": True})
        self.assertTrue(page_verification.read_state(self.home, self.session)["files"])

    def test_non_page_task_is_not_blocked(self):
        source = self.workspace / "main.py"
        source.write_text("print('ok')", encoding="utf-8")
        self.run_hook("PostToolUse", tool_name="Edit", tool_use_id="edit-2",
                      tool_input={"file_path": str(source)}, tool_response={"success": True})
        result = self.run_hook("Stop", last_assistant_message="后端已完成，测试通过。")
        self.assertNotIn("页面验证门禁", result.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
