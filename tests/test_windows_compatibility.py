import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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
    def test_checklist_redraws_in_place_and_codex_defaults_only_to_installed(self):
        import install
        import io
        from unittest.mock import patch
        output = io.StringIO()
        with patch("sys.stdout", output):
            height = install.redraw_checklist(["[ ] 1. Codex", "[x] 2. Claude"], 0, interactive=True)
            install.redraw_checklist(["[x] 1. Codex", "[x] 2. Claude"], height, interactive=True)
        text = output.getvalue()
        self.assertEqual(text.count("\033[2K"), 4)
        self.assertIn("\033[2A", text)
        self.assertIn("\033[2K[x] 1. Codex", text)

        registry = {
            "codex": {"level": "full", "label": "Codex"},
            "claude": {"level": "full", "label": "Claude"},
        }
        with patch.object(agent_targets, "agent_targets", return_value=registry), \
                patch.object(agent_targets, "detect_agents", return_value={"codex": True, "claude": True}), \
                patch.object(agent_targets, "relay_installed", side_effect=lambda target, meta: target == "claude"), \
                patch("builtins.input", return_value=""):
            selected, action = install.choose_install_targets()
        self.assertEqual(selected, ["claude"])
        self.assertEqual(action, "install")

    def test_commander_child_command_uses_each_host_background_mode(self):
        from scripts.research_commander import commander_child_command
        self.assertEqual(commander_child_command("claude", "task")[:2], ["claude", "-p"])
        self.assertEqual(commander_child_command("pi", "task")[:2], ["pi", "-p"])
        self.assertEqual(commander_child_command("opencode", "task")[:2], ["opencode", "run"])
        self.assertEqual(commander_child_command("codex", "task")[:2], ["codex", "exec"])

    def test_commander_prefers_api_and_rejects_web_provider(self):
        from scripts.commander_provider_pool import CommanderProviderCoordinator, ProviderProfile, WEB
        with tempfile.TemporaryDirectory() as directory:
            pool = CommanderProviderCoordinator(directory, [
                ProviderProfile("local-llm", "local"),
                ProviderProfile("my-api", "api"),
                ProviderProfile("deepseek-web", "web"),
            ])
            lease, error = pool.acquire("task", "role", ["local-llm", "my-api", "deepseek-web"])
            self.assertIsNotNone(lease)
            self.assertEqual(lease.provider_id, "my-api")
            pool.release(lease)
            lease, error = pool.acquire("task-2", "role", ["deepseek-web"])
            self.assertIsNone(lease)
            self.assertEqual(error, "COMMANDER_WEB_PROVIDER_NOT_SUPPORTED")

    def test_commander_api_call_uses_controlled_config_without_child_key(self):
        from unittest.mock import patch
        from scripts import commander_provider_call as bridge
        provider = {"id": "my-api", "endpoint": "http://localhost/v1", "model": "test-model", "api_key": "secret"}
        response = {"choices": [{"message": {"content": "answer"}}]}
        from scripts import config_manager
        with patch.object(config_manager, "load_config", return_value={"api_providers": [provider]}), \
                patch.object(bridge.OpenAICompatibleAdapter, "send", return_value=response) as send:
            self.assertEqual(bridge._request_provider("my-api", "question"), "answer")
            send.assert_called_once_with({"messages": [{"role": "user", "content": "question"}]})
    def test_commander_config_requires_api_or_local_not_web(self):
        from scripts import config_manager
        base = config_manager.default_config()
        for providers, expected in (
            ({"web_providers": [{"id": "web-only", "enabled": True}]}, False),
            ({"local_providers": [{"id": "local", "endpoint": "http://localhost/v1"}]}, True),
            ({"api_providers": [{"id": "api", "endpoint": "http://localhost/v1", "api_key": "key"}]}, True),
        ):
            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                (home / ".codex").mkdir()
                config = {**base, "commander": dict(base["commander"]), **providers}
                saved = config_manager.load_config(home)
                saved.update(config)
                # save_config is the production path that normalizes commander availability.
                config_manager.save_config(saved, home)
                self.assertEqual(config_manager.load_config(home)["commander"]["enabled"], expected)
    def test_commander_non_chat_api_uses_protocol_router(self):
        from scripts import commander_provider_call as bridge
        from scripts import config_manager
        provider = {"id": "my-api", "endpoint": "https://upstream.example", "model": "test", "api_key": "secret", "format": "responses"}
        response = {"choices": [{"message": {"content": "answer"}}]}
        with patch.object(config_manager, "load_config", return_value={"api_providers": [provider]}), \
                patch("scripts.api_format_router.ensure_router", return_value="http://127.0.0.1:15731/v1") as router, \
                patch.object(bridge.OpenAICompatibleAdapter, "__init__", return_value=None) as init, \
                patch.object(bridge.OpenAICompatibleAdapter, "send", return_value=response):
            self.assertEqual(bridge._request_provider("my-api", "question"), "answer")
            self.assertEqual(init.call_args.args[0], "http://127.0.0.1:15731/v1")
            self.assertEqual(init.call_args.kwargs["model"], "my-api")
            self.assertEqual(init.call_args.kwargs["api_key"], "")

    def test_local_provider_skips_disabled_first_entry(self):
        from scripts import commander_provider_call as bridge
        from scripts import config_manager
        config = {"local_providers": [
            {"id": "disabled", "endpoint": "http://disabled", "enabled": False},
            {"id": "enabled", "endpoint": "http://enabled", "enabled": True},
        ], "api_providers": []}
        with patch.object(config_manager, "load_config", return_value=config):
            profiles = bridge._profiles()
        self.assertEqual(len(profiles), 1)
        self.assertTrue(profiles[0].enabled)

    def test_api_provider_respects_concurrency_slots(self):
        from scripts.commander_provider_pool import CommanderProviderCoordinator, ProviderProfile
        with tempfile.TemporaryDirectory() as directory:
            pool = CommanderProviderCoordinator(directory, [ProviderProfile("api", "api", max_concurrency=2)])
            first, _ = pool.acquire("one", "role", ["api"])
            second, _ = pool.acquire("two", "role", ["api"])
            third, error = pool.acquire("three", "role", ["api"])
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertIsNone(third)
            pool.release(first)
            pool.release(second)

    def test_child_response_rejects_empty_and_error_results(self):
        from scripts.research_commander import _child_response_text
        self.assertEqual(_child_response_text(["claude"], '{"result":"ok"}'), "ok")
        self.assertEqual(_child_response_text(["opencode", "run"], '{"type":"text","text":"ok"}'), "ok")
        with self.assertRaises(RuntimeError):
            _child_response_text(["claude"], '{"is_error":true,"result":"denied"}')
        self.assertEqual(_child_response_text(["claude"], ""), "")


if __name__ == "__main__":
    unittest.main()
