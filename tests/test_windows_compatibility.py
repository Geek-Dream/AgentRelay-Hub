import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import app_close_policy
from scripts import agent_targets


class WindowsCompatibilityTests(unittest.TestCase):
    def test_utf8_entry_point_does_not_crash_under_gbk(self):
        env = dict(os.environ, PYTHONIOENCODING="cp936")
        env.pop("PYTHONUTF8", None)
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts/agent_image.py"), "/missing.png"],
            cwd=PROJECT_ROOT, env=env, text=True, encoding="utf-8", errors="replace",
            capture_output=True, check=False,
        )
        self.assertNotIn("UnicodeEncodeError", result.stderr)
        self.assertIn("⚠️", result.stdout + result.stderr)

    def test_app_close_permission_is_narrow_and_revocable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            command = 'taskkill /IM editor.exe /F'
            self.assertFalse(app_close_policy.allows(command, home))
            app_close_policy.remember(command, home)
            self.assertTrue(app_close_policy.allows(command, home))
            self.assertFalse(app_close_policy.allows('taskkill /IM other.exe /F', home))
            self.assertTrue(app_close_policy.forget("editor.exe", home))
            self.assertFalse(app_close_policy.allows(command, home))
            for unsafe in ("taskkill /PID 42 /F", "taskkill /IM *.exe /F", "Stop-Process -Id 42"):
                with self.assertRaises(ValueError):
                    app_close_policy.remember(unsafe, home)

    def test_command_registration_and_uninstall_preserve_other_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = {
                "id": "claude", "skill_dir": root / "skills/agent-relay",
                "command_dir": root / "commands", "config_path": root / "settings.json",
            }
            (target["skill_dir"] / "scripts").mkdir(parents=True)
            (target["skill_dir"] / "SKILL.md").write_text("relay", encoding="utf-8")
            (root / "settings.json").write_text(json.dumps({"hooks": {"Stop": [
                {"hooks": [{"command": "user-command"}, {"command": "python agentrelay_hook_shim.py"}]},
                {"other": True},
            ]}}), encoding="utf-8")
            agent_targets.install_slash_commands(target, target["skill_dir"])
            self.assertTrue((root / "commands/agent-approval.md").is_file())
            agent_targets.unregister_agent_relay("claude", target)
            self.assertFalse((root / "commands/agent-approval.md").exists())
            config = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(config["hooks"]["Stop"][0]["hooks"][0]["command"], "user-command")
            self.assertEqual(config["hooks"]["Stop"][1], {"other": True})
            self.assertFalse(target["skill_dir"].exists())
    def test_install_selection_uninstalls_deselected_target(self):
        import install
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"install_targets": ["claude"], "install_skills": []}
            removed = []
            with patch.object(install, "_load_local_config", return_value=(config, lambda *_: None)), \
                    patch.object(agent_targets, "relay_installed", return_value=True), \
                    patch.object(agent_targets, "unregister_agent_relay", side_effect=lambda target, meta: removed.append(target)), \
                    patch.object(install, "apply_agent_targets", return_value=({}, {})) as apply:
                install.apply_target_selection(["codex"], [])
            self.assertEqual(removed, ["claude"])
            apply.assert_called_once_with(["codex"], [])
    def test_web_choice_never_installs_or_merges_hooks(self):
        import install
        from unittest.mock import patch, MagicMock
        tty = MagicMock()
        tty.isatty.return_value = True
        with patch.object(install.sys, "stdin", tty), \
                patch.object(install, "choose_install_targets", return_value=(["claude"], "web")), \
                patch.object(install, "validate_publish_tree"), \
                patch.object(install, "check_base_environment"), \
                patch.object(install, "ensure_virtualenv"), \
                patch.object(install, "install_dependencies"), \
                patch.object(install, "install_and_verify_chromium"), \
                patch.object(install, "install_files") as install_files, \
                patch.object(install, "merge_hooks_json") as merge_hooks, \
                patch.object(install, "apply_target_selection") as apply, \
                patch.object(install, "run_web_configurator"), \
                patch.object(install, "print_next_steps"):
            self.assertEqual(install.main([]), 0)
        install_files.assert_not_called()
        merge_hooks.assert_not_called()
        apply.assert_not_called()

    def test_empty_interactive_choice_uninstalls_without_reinstalling(self):
        import install
        from unittest.mock import patch, MagicMock
        tty = MagicMock()
        tty.isatty.return_value = True
        with patch.object(install.sys, "stdin", tty), \
                patch.object(install.sys, "argv", ["install.py"]), \
                patch.object(install, "choose_install_targets", return_value=([], "install")), \
                patch.object(install, "choose_install_skills", return_value=[]), \
                patch.object(install, "validate_publish_tree"), \
                patch.object(install, "check_base_environment"), \
                patch.object(install, "ensure_virtualenv"), \
                patch.object(install, "install_dependencies"), \
                patch.object(install, "install_and_verify_chromium"), \
                patch.object(install, "install_files") as install_files, \
                patch.object(install, "merge_hooks_json") as merge_hooks, \
                patch.object(install, "apply_target_selection", return_value=({}, {})) as apply, \
                patch.object(install, "_load_local_config", return_value=({}, lambda *_: None)), \
                patch.object(install, "run_web_configurator"), \
                patch.object(install, "print_next_steps"):
            self.assertEqual(install.main(None), 0)
        install_files.assert_not_called()
        merge_hooks.assert_not_called()
        apply.assert_called_once_with([], [])


if __name__ == "__main__":
    unittest.main()
