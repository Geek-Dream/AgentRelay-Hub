"""Adaptive Voice 的模式切换、回读与跨入口 Hook 接线测试。"""

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = PROJECT_ROOT / "hooks" / "agent_relay_tracker.py"
VOICE_SCRIPT = PROJECT_ROOT / "scripts" / "voice_mode.py"


class VoiceModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.env = {**os.environ, "CODEX_HOME": str(self.home)}
        module_spec = importlib.util.spec_from_file_location("voice_mode", VOICE_SCRIPT)
        self.module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = self.module
        module_spec.loader.exec_module(self.module)

    def test_default_is_normal_and_invalid_modes_are_rejected(self):
        self.assertEqual(self.module.current_mode(home=self.home), "normal")
        self.assertEqual(self.module.instruction("normal"), "")
        with self.assertRaises(ValueError):
            self.module.parse_command("/adaptive-voice unknown")

    def test_slash_command_show_set_and_clear(self):
        shown = self.module.parse_command("/adaptive-voice")
        self.assertEqual(shown["action"], "show")
        changed = self.module.parse_command("/adaptive-voice eli5")
        self.assertEqual(changed["action"], "set")
        result = self.module.apply_command(changed, home=self.home)
        self.assertTrue(result["persisted"])
        self.assertEqual(self.module.current_mode(home=self.home), "eli5")
        cleared = self.module.apply_command(
            self.module.parse_command("/adaptive-voice clear"), home=self.home,
        )
        self.assertEqual(cleared["mode"], "normal")

    def test_natural_language_mode_request_is_conservative(self):
        for prompt in ("用大白话解释并举例", "切成5岁能懂的模式", "专业点，用术语给 Agent 看", "恢复正常输出"):
            request = self.module.parse_command(prompt)
            self.assertIsNotNone(request, prompt)
            self.assertEqual(request["action"], "set", prompt)
        for prompt in (
            "为什么不能用大白话？", "把代码改成 Python", "写一个专业术语解释文档",
            "正常输出是什么意思？", "用专业术语写一份接口文档", "给用户的大白话文案",
            "请不要用专业术语", "不要用大白话回答", "不要恢复正常输出",
        ):
            self.assertIsNone(self.module.parse_command(prompt), prompt)

    def test_unreadable_shared_config_is_never_replaced_by_voice_mode(self):
        path = self.module.config_manager.config_path(self.home)
        path.parent.mkdir(parents=True)
        original = b"recoverable-but-currently-undecryptable"
        path.write_bytes(original)
        with patch.object(
            self.module.config_manager, "_fernet", side_effect=ValueError("bad key"),
        ):
            with self.assertRaises(ValueError):
                self.module.set_mode("eli5", home=self.home)
        self.assertEqual(path.read_bytes(), original)

    def test_adaptive_voice_installs_from_explicit_source(self):
        from scripts import agent_targets

        source = self.home / "source" / "adaptive-voice"
        (source / "references").mkdir(parents=True)
        (source / "SKILL.md").write_text("# Adaptive Voice", encoding="utf-8")
        (source / "references" / "modes.md").write_text("# Modes", encoding="utf-8")
        (source / "profiles").mkdir()
        (source / "profiles" / "default.md").write_text("# Blank", encoding="utf-8")
        (source / ".git").mkdir()
        (source / ".git" / "private").write_text("not a skill file", encoding="utf-8")
        target = {"skill_dir": self.home / "skills" / "adaptive-voice"}
        result = agent_targets.install_adaptive_voice_to(target, source)
        self.assertEqual(result["files"], 4)
        self.assertEqual((target["skill_dir"] / "SKILL.md").read_text(), "# Adaptive Voice")
        self.assertTrue((target["skill_dir"] / "references" / "modes.md").is_file())
        self.assertFalse((target["skill_dir"] / ".git").exists())

        (target["skill_dir"] / "profiles").mkdir(exist_ok=True)
        (target["skill_dir"] / "profiles" / "default.md").write_text("saved preference", encoding="utf-8")
        agent_targets.install_adaptive_voice_to(target, source)
        self.assertEqual(
            (target["skill_dir"] / "profiles" / "default.md").read_text(encoding="utf-8"),
            "saved preference",
        )

    def test_standalone_skill_has_no_agentrelay_dependency(self):
        source = self.home / "adaptive-voice"
        (source / "scripts").mkdir(parents=True)
        script = PROJECT_ROOT / "scripts" / "voice_mode.py"
        (source / "scripts" / "voice_mode.py").write_bytes(script.read_bytes())
        standalone_home = self.home / "standalone-home"
        env = {**os.environ, "ADAPTIVE_VOICE_HOME": str(standalone_home)}
        env.pop("CODEX_HOME", None)
        result = subprocess.run(
            [sys.executable, str(source / "scripts" / "voice_mode.py"), "set", "eli5", "--json"],
            cwd=source, env=env, text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "eli5")
        shown = subprocess.run(
            [sys.executable, str(source / "scripts" / "voice_mode.py"), "show", "--json"],
            cwd=source, env=env, text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(json.loads(shown.stdout)["mode"], "eli5")
        self.assertFalse((standalone_home / "config").exists())

    def test_selected_voice_install_uses_only_bundled_source(self):
        import install

        from scripts import skill_catalog

        target = {"label": "Codex", "skill_dir": str(self.home / "skills" / "agent-relay")}
        catalog = skill_catalog.catalog_with_installed_skills(PROJECT_ROOT, {})
        calls = []
        with patch.object(install, "load_install_skill_catalog", return_value=catalog), \
                patch("scripts.agent_targets.agent_targets", return_value={"codex": target}), \
                patch("scripts.agent_targets.install_skill_to"), \
                patch("scripts.agent_targets.install_adaptive_voice_to") as voice, \
                patch("scripts.agent_targets.install_catalog_skill_to", side_effect=lambda *_args, **_kwargs: calls.append(True) or self.home / "skill"), \
                patch("scripts.agent_targets.install_slash_commands"), \
                patch("scripts.agent_targets.verify_target", return_value=[]), \
                patch.object(install, "install_files"), \
                patch.object(install, "merge_hooks_json"):
            install._apply_agent_targets_loop(["codex"], [], catalog)
            voice.assert_not_called()
            self.assertEqual(calls, [])
            install._apply_agent_targets_loop(
                ["codex"], [], catalog, voice_source=self.home / "explicit-voice",
            )
            voice.assert_not_called()
            self.assertEqual(calls, [])
            install._apply_agent_targets_loop(["codex"], ["adaptive-voice"], catalog)
            voice.assert_not_called()
            self.assertEqual(calls, [True])
            install._apply_agent_targets_loop(
                ["codex"], ["adaptive-voice"], catalog,
                voice_source=self.home / "explicit-voice",
            )
            voice.assert_called_once_with(target, self.home / "explicit-voice")
            self.assertEqual(calls, [True])

    def test_bundled_voice_installs_standalone_and_shared_mode(self):
        from scripts import agent_targets, skill_catalog

        source = PROJECT_ROOT / "skills" / "catalog" / "agentrelay" / "adaptive-voice"
        target = {
            "skill_dir": self.home / "skills" / "agent-relay",
            "command_dir": self.home / "commands",
        }
        result = agent_targets.install_adaptive_voice_to(target, source)
        destination = self.home / "skills" / "adaptive-voice"
        self.assertGreater(result["installed"], 0)
        self.assertTrue((destination / "commands" / "adaptive-voice.md").is_file())
        self.assertTrue((self.home / "commands" / "adaptive-voice.md").is_file())
        standalone = subprocess.run(
            [sys.executable, str(destination / "scripts" / "voice_mode.py"), "set", "expert", "--json"],
            env={**os.environ, "ADAPTIVE_VOICE_HOME": str(self.home / "local")},
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(standalone.returncode, 0, standalone.stderr)
        self.assertEqual(json.loads(standalone.stdout)["mode"], "expert")
        saved = self.module.set_mode("eli5", home=self.home)
        self.assertEqual(saved, {"mode": "eli5"})
        shared = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "voice_mode.py"), "show", "--json"],
            env=self.env, text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(json.loads(shared.stdout)["mode"], "eli5")
        merged = skill_catalog.catalog_with_installed_skills(PROJECT_ROOT, {})
        self.assertIn("adaptive-voice", merged["defaults"])
        self.assertNotIn(
            "adaptive-voice", skill_catalog.selection_from_cli(merged, no_skills=True),
        )

    def test_install_wizard_configures_selected_voice(self):
        import install

        for choice, expected in (("2", "eli5"), ("3", "expert"), ("1", "normal")):
            config = {"voice": {"mode": "normal"}}
            with patch.object(install, "_ask", return_value=choice):
                install.configure_voice(config)
            self.assertEqual(config["voice"]["mode"], expected)
        with patch.object(install, "_ask", return_value="9"):
            with self.assertRaises(install.InstallError):
                install.configure_voice({})

    def test_config_round_trip(self):
        sys.modules.pop("scripts.voice_mode", None)
        manager = importlib.import_module("scripts.config_manager")
        config = manager.default_config()
        self.assertEqual(config["voice"]["mode"], "normal")
        self.module.set_mode("adaptive", home=self.home)
        self.assertEqual(manager.load_config(self.home)["voice"]["mode"], "adaptive")
        with self.assertRaises(ValueError):
            self.module.normalize_mode("invalid", strict=True)
        with self.assertRaises(ValueError):
            self.module.set_mode(" invalid ", home=self.home)
        self.assertEqual(manager.load_config(self.home)["voice"]["mode"], "adaptive")
        config["voice"] = {"mode": "invalid"}
        manager.save_config(config, self.home)
        self.assertEqual(manager.load_config(self.home)["voice"]["mode"], "normal")

    def test_hook_consumes_command_and_injects_current_mode(self):
        tracker_spec = importlib.util.spec_from_file_location("voice_tracker", HOOK)
        tracker = importlib.util.module_from_spec(tracker_spec)
        sys.modules[tracker_spec.name] = tracker
        tracker_spec.loader.exec_module(tracker)
        tracker.CODEX_DIR = self.home
        state = {"session_id": "voice-test"}
        result = tracker.voice_settings_request(state, "/adaptive-voice expert")
        self.assertIsNotNone(result)
        self.assertEqual(self.module.current_mode(home=self.home), "expert")
        self.assertIn("已经保存", result)
        self.assertIn("[Adaptive Voice · expert]", tracker.voice_context(state))
        self.assertNotIn("正常", tracker.voice_context(state))

    def test_hook_prefers_checkout_over_stale_installed_module(self):
        installed = self.home / "skills" / "agent-relay" / "scripts"
        installed.mkdir(parents=True)
        (installed / "voice_mode.py").write_text("SOURCE = 'stale'\n", encoding="utf-8")
        tracker_spec = importlib.util.spec_from_file_location("voice_source_tracker", HOOK)
        tracker = importlib.util.module_from_spec(tracker_spec)
        tracker_spec.loader.exec_module(tracker)
        tracker.CODEX_DIR = self.home
        with patch("importlib.util.find_spec", return_value=None):
            for name in ("voice_mode", "scripts", "scripts.voice_mode"):
                sys.modules.pop(name, None)
            module = tracker._shared_script_module("voice_mode")
        self.assertEqual(Path(module.__file__), VOICE_SCRIPT)

    def test_real_hook_persists_then_reads_mode(self):
        for prompt, expected in (
            ("/adaptive-voice eli5", "eli5"),
            ("请用大白话并举例说明", "eli5"),
        ):
            result = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps({
                    "hook_event_name": "UserPromptSubmit", "session_id": "voice-real",
                    "prompt": prompt, "cwd": str(PROJECT_ROOT),
                }, ensure_ascii=False), text=True, capture_output=True, env=self.env,
                timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.module.current_mode(home=self.home), expected)
        self.assertIn("[Adaptive Voice · eli5]", self.module.instruction("eli5"))

    def test_invalid_slash_command_does_not_fall_through_to_task(self):
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({
                "hook_event_name": "UserPromptSubmit", "session_id": "voice-invalid",
                "prompt": "/adaptive-voice unknown", "cwd": str(PROJECT_ROOT),
            }), text=True, capture_output=True, env=self.env, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("模式未修改", result.stdout)
        self.assertEqual(self.module.current_mode(home=self.home), "normal")

    def test_real_hook_uses_runtime_sibling_when_config_home_is_split(self):
        runtime = self.home / "runtime"
        tracker_dir = runtime / "hooks"
        scripts_dir = runtime / "skills" / "agent-relay" / "scripts"
        tracker_dir.mkdir(parents=True)
        scripts_dir.mkdir(parents=True)
        (tracker_dir / "agent_relay_tracker.py").write_bytes(HOOK.read_bytes())
        for name in ("voice_mode.py", "config_manager.py", "stdio_encoding.py"):
            (scripts_dir / name).write_bytes((PROJECT_ROOT / "scripts" / name).read_bytes())
        result = subprocess.run(
            [sys.executable, str(tracker_dir / "agent_relay_tracker.py")],
            input=json.dumps({
                "hook_event_name": "UserPromptSubmit", "session_id": "voice-split",
                "prompt": "/adaptive-voice expert", "cwd": str(PROJECT_ROOT),
            }), text=True, capture_output=True, env=self.env, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.module.current_mode(home=self.home), "expert")

    def test_web_page_exposes_shared_switcher(self):
        page = importlib.import_module("scripts.config_web").build_config_page({
            "voice": {"mode": "expert"},
            "fusion": {"enabled": True, "tool_mode": "readonly", "provider_id": ""},
            "fusion_state": {}, "web_assist": {"enabled": False},
            "commander": {}, "web_providers": [], "local_providers": [],
            "api_providers": [], "notifications": {"enabled": True},
        })
        self.assertIn('id="voice-mode"', page)
        self.assertIn("输出风格", page)
        self.assertIn("body:JSON.stringify({voice:{mode:", page)
        self.assertIn("/adaptive-voice", page)
        self.assertIn('value="adaptive"', page)
    def test_real_web_api_saves_and_reads_shared_mode(self):
        import install

        opened = threading.Event()
        location = []
        failures = []

        def run_server():
            try:
                install.run_web_configurator()
            except Exception as exc:  # surfaced by assertion below
                failures.append(exc)

        thread = threading.Thread(target=run_server, daemon=True)
        with patch.object(install, "CODEX_HOME", self.home), \
                patch.object(install.webbrowser, "open", side_effect=lambda url: (location.append(url), opened.set())), \
                patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            thread.start()
            self.assertTrue(opened.wait(10), "config server did not start")
            try:
                request = Request(
                    location[0] + "/api/settings",
                    data=json.dumps({"voice": {"mode": "eli5"}}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    result = json.load(response)
                self.assertEqual(result["config"]["voice"], {"mode": "eli5"})
                self.assertEqual(self.module.current_mode(home=self.home), "eli5")
                with urlopen(location[0] + "/api/config", timeout=10) as response:
                    read_back = json.load(response)
                self.assertEqual(read_back["config"]["voice"]["mode"], "eli5")
            finally:
                close = Request(location[0] + "/api/close", data=b"{}", method="POST")
                urlopen(close, timeout=10).close()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
