import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


targets = load_module("agent_targets", PROJECT_ROOT / "scripts" / "agent_targets.py")
shim = load_module(
    "agentrelay_hook_shim",
    PROJECT_ROOT / "scripts" / "agentrelay_hook_shim.py",
)
tracker = load_module(
    "agent_relay_tracker",
    PROJECT_ROOT / "hooks" / "agent_relay_tracker.py",
)

try:
    import yaml  # noqa: F401

    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class AgentTargetRegistryTests(unittest.TestCase):
    def test_registry_covers_requested_agents(self):
        registry = targets.agent_targets()
        for expected in ("codex", "claude", "pi", "opencode", "hermes", "kimi"):
            self.assertIn(expected, registry)
        self.assertEqual(registry["opencode"]["level"], "skill-only")
        self.assertTrue(any(a["id"] == "deepseek-cli" for a in targets.UNSUPPORTED_AGENTS))

    def test_detect_agents_returns_bool_map(self):
        detected = targets.detect_agents(targets.agent_targets())
        self.assertEqual(
            set(detected),
            {"codex", "claude", "pi", "opencode", "hermes", "kimi"},
        )
        self.assertTrue(all(isinstance(value, bool) for value in detected.values()))


class KimiRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentrelay-kimi-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / "config.toml"

    def test_merge_appends_hooks_and_preserves_existing(self):
        self.config.write_text(
            'default_model = "kimi-code/k3"\n\n'
            "[[hooks]]\n"
            'event = "PreToolUse"\n'
            'matcher = "Bash"\n'
            "command = \"node ~/.kimi-code/hooks/block.mjs\"\n"
            "timeout = 5\n",
            encoding="utf-8",
        )
        venv = Path("/venv/bin/python")
        shim = Path("/skill/scripts/agentrelay_hook_shim.py")
        self.assertTrue(targets.register_kimi(self.config, venv, shim))
        text = self.config.read_text(encoding="utf-8")
        self.assertIn('default_model = "kimi-code/k3"', text)
        self.assertIn("block.mjs", text)
        for event in targets.KIMI_EVENTS:
            self.assertIn(f'event = "{event}"', text)
        self.assertIn("agentrelay_hook_shim.py", text)
        import tomllib
        data = tomllib.loads(text)
        self.assertEqual(len(data["hooks"]), 1 + len(targets.KIMI_EVENTS))

    def test_merge_is_idempotent(self):
        venv = Path("/venv/bin/python")
        shim = Path("/skill/scripts/agentrelay_hook_shim.py")
        self.assertTrue(targets.register_kimi(self.config, venv, shim))
        first = self.config.read_text(encoding="utf-8")
        self.assertFalse(targets.register_kimi(self.config, venv, shim))
        self.assertEqual(self.config.read_text(encoding="utf-8"), first)

    def test_merge_rejects_invalid_toml(self):
        self.config.write_text("[[hooks]\nevent = 'broken'\n", encoding="utf-8")
        if sys.version_info < (3, 11):
            self.skipTest("tomllib 需要 Python 3.11+")
        with self.assertRaises(targets.TargetError):
            targets.register_kimi(
                self.config, Path("/venv/bin/python"),
                Path("/skill/scripts/agentrelay_hook_shim.py"),
            )

    def test_registered_in_config(self):
        venv = Path("/venv/bin/python")
        shim = Path("/skill/scripts/agentrelay_hook_shim.py")
        meta = {"config_path": self.config, "level": "full"}
        ok, _ = targets.registered_in_config("kimi", meta, venv, shim)
        self.assertFalse(ok)
        targets.register_kimi(self.config, venv, shim)
        ok, detail = targets.registered_in_config("kimi", meta, venv, shim)
        self.assertTrue(ok, detail)


class ClaudeRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentrelay-claude-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.settings = self.tmp / "settings.json"

    def test_merge_creates_hooks_and_keeps_existing(self):
        self.settings.write_text(
            json.dumps({
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Bash",
                         "hooks": [{"type": "command", "command": "/other/hook.sh"}]}
                    ]
                },
                "model": "opus",
            }),
            encoding="utf-8",
        )
        changed = targets.register_claude(self.settings, "venv-python hook.py")
        self.assertTrue(changed)
        config = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "opus")
        commands = [
            hook["command"]
            for entry in config["hooks"]["PostToolUse"]
            for hook in entry["hooks"]
        ]
        self.assertIn("/other/hook.sh", commands)
        self.assertIn("venv-python hook.py", commands)
        for event in targets.CLAUDE_EVENTS:
            self.assertIn(event, config["hooks"])
        backups = list(self.tmp.glob("settings.json.before-agentrelay-install.*"))
        self.assertEqual(len(backups), 1)

    def test_merge_is_idempotent_and_updates_command(self):
        targets.register_claude(self.settings, "old-command agent_relay_hook.py")
        self.assertFalse(targets.register_claude(self.settings, "old-command agent_relay_hook.py"))
        config = json.loads(self.settings.read_text(encoding="utf-8"))
        count = sum(
            1
            for entry in config["hooks"]["Stop"]
            for hook in entry["hooks"]
            if "agent_relay_hook" in hook["command"]
        )
        self.assertEqual(count, 1)
        self.assertTrue(targets.register_claude(self.settings, "new-command agent_relay_hook.py"))
        config = json.loads(self.settings.read_text(encoding="utf-8"))
        hook = targets.find_agent_relay_hook(config["hooks"]["Stop"])
        self.assertEqual(hook["command"], "new-command agent_relay_hook.py")

    def test_merge_rejects_invalid_json(self):
        self.settings.write_text("{not json", encoding="utf-8")
        with self.assertRaises(targets.TargetError):
            targets.register_claude(self.settings, "cmd")


@unittest.skipUnless(HAS_YAML, "PyYAML 未安装")
class HermesRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentrelay-hermes-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / "config.yaml"

    def _entries(self):
        return {
            event: [{"command": f"python shim.py --target hermes --event {canonical}",
                     "timeout": 15}]
            for event, canonical in targets.HERMES_EVENTS.items()
        }

    def test_merge_creates_hooks_and_is_idempotent(self):
        self.config.write_text("model: hermes-3\n", encoding="utf-8")
        self.assertTrue(targets.register_hermes_with_yaml(self.config, self._entries()))
        data = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "hermes-3")
        for event in targets.HERMES_EVENTS:
            self.assertEqual(len(data["hooks"][event]), 1)
        self.assertFalse(targets.register_hermes_with_yaml(self.config, self._entries()))
        data = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(len(data["hooks"]["pre_llm_call"]), 1)

    def test_merge_preserves_existing_hooks(self):
        self.config.write_text(
            "hooks:\n  pre_llm_call:\n    - command: \"~/other.sh\"\n      timeout: 60\n",
            encoding="utf-8",
        )
        targets.register_hermes_with_yaml(self.config, self._entries())
        data = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        commands = [item["command"] for item in data["hooks"]["pre_llm_call"]]
        self.assertIn("~/other.sh", commands)
        self.assertTrue(any("agentrelay_hook_shim" not in c for c in commands))
        self.assertTrue(any("--target hermes" in c for c in commands))


class ShimProtocolTests(unittest.TestCase):
    def test_inject_event_adds_missing_event_name(self):
        payload = json.dumps({"session_id": "s1", "user_message": "hi"})
        result = json.loads(shim.inject_event(payload, "UserPromptSubmit"))
        self.assertEqual(result["hook_event_name"], "UserPromptSubmit")
        self.assertEqual(result["session_id"], "s1")

    def test_inject_event_keeps_existing_event_name(self):
        payload = json.dumps({"hook_event_name": "PostToolUse"})
        result = json.loads(shim.inject_event(payload, "UserPromptSubmit"))
        self.assertEqual(result["hook_event_name"], "PostToolUse")

    def test_inject_event_passthrough_on_invalid_json(self):
        self.assertEqual(shim.inject_event("not-json", "Stop"), "not-json")

    def test_normalize_kimi_failure_bridges_error_to_tool_output(self):
        payload = json.dumps({
            "hook_event_name": "PostToolUseFailure",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "error": "Command failed with exit code 1",
        })
        result = json.loads(shim.normalize_payload(payload, "kimi", "PostToolUseFailure"))
        self.assertEqual(result["tool_output"], "Command failed with exit code 1")
        self.assertEqual(result["error"], "Command failed with exit code 1")

    def test_normalize_kimi_keeps_existing_tool_output(self):
        payload = json.dumps({
            "hook_event_name": "PostToolUseFailure",
            "error": "x",
            "tool_output": "already here",
        })
        result = json.loads(shim.normalize_payload(payload, "kimi", "PostToolUseFailure"))
        self.assertEqual(result["tool_output"], "already here")

    def test_render_kimi_stop_block_uses_stderr_and_exit_2(self):
        stdout = json.dumps({"decision": "block", "reason": "候选提醒"})
        out, err, code = shim.render_for_target(stdout, "kimi")
        self.assertEqual((out, err, code), ("", "候选提醒", 2))

    def test_render_kimi_non_block_output_dropped(self):
        stdout = json.dumps({
            "hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": "提醒"}
        })
        self.assertEqual(shim.render_for_target(stdout, "kimi"), ("", "", 0))

    def test_translate_hermes_additional_context(self):
        stdout = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "候选提醒",
            }
        })
        self.assertEqual(
            json.loads(shim.translate_output(stdout, "hermes")),
            {"context": "候选提醒"},
        )

    def test_translate_hermes_passthrough_stop_block(self):
        stdout = json.dumps({"decision": "block", "reason": "继续处理"})
        self.assertEqual(
            json.loads(shim.translate_output(stdout, "hermes")),
            {"decision": "block", "reason": "继续处理"},
        )

    def test_translate_non_hermes_unchanged(self):
        stdout = '{"hookSpecificOutput":{"additionalContext":"x"}}'
        self.assertEqual(shim.translate_output(stdout, "claude"), stdout)
        self.assertEqual(shim.translate_output(stdout, ""), stdout)


class PiExtensionTests(unittest.TestCase):
    def test_extension_written_and_idempotent(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-pi-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        extension = tmp / "extensions" / "agent-relay.ts"
        self.assertTrue(targets.write_pi_extension(
            extension, Path("/venv/bin/python"), Path("/skill/scripts/agentrelay_hook_shim.py"),
        ))
        content = extension.read_text(encoding="utf-8")
        self.assertNotIn("/venv/bin/python", content)
        self.assertNotIn("/skill/scripts/agentrelay_hook_shim.py", content)
        self.assertNotIn(str(tmp), content)
        self.assertIn('process.platform === "win32"', content)
        self.assertIn('join(VENV, "Scripts", "python.exe")', content)
        self.assertIn("AGENTRELAY_PYTHON", content)
        self.assertIn("AGENTRELAY_SHIM", content)
        self.assertIn("AGENTRELAY_FUSION", content)
        self.assertIn("before_agent_start", content)
        self.assertIn('registerCommand("agent-fusion"', content)
        self.assertIn("state?.delegable", content)
        self.assertIn("agent-fusion", content)
        self.assertIn("return item.label + \" - \" + item.description;", content)
        self.assertIn("查看当前状态", content)
        self.assertIn("检查开关、Commander 最高优先级互斥", content)
        self.assertIn("开启 Fusion", content)
        self.assertIn("保存开启偏好", content)
        self.assertIn("async function fusionCommand", content)
        self.assertFalse(targets.write_pi_extension(
            extension, Path("/venv/bin/python"), Path("/skill/scripts/agentrelay_hook_shim.py"),
        ))


class RelayPythonTests(unittest.TestCase):
    def test_windows_virtualenv_python_is_detected(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-win-python-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        dedicated = tmp / "agentrelay-env" / "Scripts" / "python.exe"
        dedicated.parent.mkdir(parents=True)
        dedicated.write_bytes(b"")
        with patch.object(tracker, "CODEX_DIR", tmp), patch.dict(
            os.environ, {"AGENTRELAY_PYTHON": ""}
        ):
            self.assertEqual(tracker.find_relay_python(), str(dedicated))

    def test_python_environment_override_wins(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-python-override-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        dedicated = tmp / "custom-python"
        dedicated.write_bytes(b"")
        with patch.object(tracker, "CODEX_DIR", tmp / "unused"), patch.dict(
            os.environ, {"AGENTRELAY_PYTHON": str(dedicated)}
        ):
            self.assertEqual(tracker.find_relay_python(), str(dedicated))


class CodexVerifyTests(unittest.TestCase):
    def test_verify_codex_target_in_temp_home(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-verify-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        hook_dir = tmp / "hooks"
        hook_dir.mkdir(parents=True)
        for name in ("agent_relay_hook.py", "agent_relay_tracker.py"):
            shutil.copy2(PROJECT_ROOT / "hooks" / name, hook_dir / name)
        skill_dir = tmp / "skills" / "agent-relay"
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# AgentRelay", encoding="utf-8")
        (skill_dir / "scripts" / "agent_relay.py").write_text("", encoding="utf-8")
        fusion_dir = tmp / "skills" / "agent-fusion"
        (fusion_dir / "scripts").mkdir(parents=True)
        (fusion_dir / "SKILL.md").write_text("# AgentFusion", encoding="utf-8")
        (fusion_dir / "scripts" / "agent_fusion.py").write_text("", encoding="utf-8")
        (fusion_dir / "scripts" / "fusion_mode.py").write_text("", encoding="utf-8")
        (fusion_dir / "scripts" / "fusion_runner.py").write_text("", encoding="utf-8")
        hooks_json = tmp / "hooks.json"
        events = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
        hooks_json.write_text(json.dumps({
            "hooks": {
                event: [{"hooks": [{"type": "command",
                                    "command": "python hooks/agent_relay_hook.py"}]}]
                for event in events
            }
        }), encoding="utf-8")
        meta = {
            "id": "codex", "label": "Codex CLI", "level": "full",
            "skill_dir": skill_dir, "hook_dir": hook_dir, "config_path": hooks_json,
        }
        with patch.dict(os.environ, {"CODEX_HOME": str(tmp)}):
            checks = targets.verify_target(
                "codex", meta,
                Path(sys.executable),
                hook_dir / "agent_relay_hook.py",
                hook_dir / "agentrelay_hook_shim.py",
                hook_dir / "agent_relay_tracker.py",
                tmp,
            )
        results = {check["name"]: check for check in checks}
        for name in ("Skill 文件", "Fusion Skill", "Hook 配置", "Hook 干跑", "Tracker 状态"):
            self.assertIn(name, results)
            self.assertTrue(results[name]["ok"], f"{name}: {results[name]['detail']}")

    def test_verify_reports_missing_registration(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-verify-missing-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        meta = {
            "id": "claude", "label": "Claude Code", "level": "full",
            "skill_dir": tmp / "skills" / "agent-relay",
            "hook_dir": tmp / "hooks",
            "config_path": tmp / "settings.json",
        }
        ok, detail = targets.registered_in_config(
            "claude", meta, Path(sys.executable), tmp / "shim.py"
        )
        self.assertFalse(ok)
        self.assertIn("不存在", detail)


class KimiEndToEndTests(unittest.TestCase):
    """复现用户场景：同一方案反复失败 → retry 达标 → Stop 事件注入候选提醒。"""

    def test_repeated_failures_block_stop_with_relay_reminder(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-kimi-e2e-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        hook_dir = tmp / "hooks"
        hook_dir.mkdir(parents=True)
        for name in ("agent_relay_hook.py", "agent_relay_tracker.py"):
            shutil.copy2(PROJECT_ROOT / "hooks" / name, hook_dir / name)
        shim = PROJECT_ROOT / "scripts" / "agentrelay_hook_shim.py"
        env = {**os.environ, "CODEX_HOME": str(tmp)}

        def run(event: str, payload: dict) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(shim), "--target", "kimi", "--event", event],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
                env=env,
            )

        base = {"session_id": "kimi-e2e", "cwd": str(tmp)}
        prompt = run("UserPromptSubmit", {
            **base, "hook_event_name": "UserPromptSubmit",
            "prompt": "帮我写一个动画页面",
        })
        self.assertEqual(prompt.returncode, 0, prompt.stderr)

        failure = {
            **base, "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "echo '<html>'"},
            "tool_call_id": "call-1",
            "error": "Command failed with exit code 1: zsh:1: no matches found",
        }
        for _ in range(4):
            result = run("PostToolUseFailure", failure)
            self.assertEqual(result.returncode, 0, result.stderr)

        stop = run("Stop", {**base, "hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual(stop.returncode, 2, f"stdout={stop.stdout} stderr={stop.stderr}")
        self.assertIn("AgentRelay", stop.stderr)
        self.assertIn("候选", stop.stderr)


if __name__ == "__main__":
    unittest.main()
