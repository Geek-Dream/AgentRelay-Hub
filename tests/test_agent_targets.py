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

    def test_kimi_skill_dir_uses_kimi_code_home(self):
        kimi_home = Path(tempfile.mkdtemp(prefix="agentrelay-kimi-home-"))
        self.addCleanup(shutil.rmtree, kimi_home, True)
        with patch.dict(os.environ, {"KIMI_CODE_HOME": str(kimi_home)}):
            registry = targets.agent_targets()
        self.assertEqual(
            Path(registry["kimi"]["skill_dir"]),
            kimi_home / "skills" / "agent-relay",
        )

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
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
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

    def test_registration_covers_pre_tool_gate(self):
        self.assertTrue(targets.register_claude(self.settings, "python agent_relay_hook.py"))
        config = json.loads(self.settings.read_text(encoding="utf-8"))
        for event in targets.CLAUDE_EVENTS:
            self.assertIsNotNone(targets.find_agent_relay_hook(config["hooks"][event]))
        self.assertIn("PreToolUse", config["hooks"])

    def test_registered_in_config_requires_explicit_claude_target(self):
        venv = Path("/venv/bin/python")
        shim = self.tmp / "agentrelay_hook_shim.py"
        meta = {"config_path": self.settings, "level": "full"}

        targets.register_claude(
            self.settings, "python /old/hooks/agent_relay_hook.py"
        )
        ok, detail = targets.registered_in_config(
            "claude", meta, venv, shim
        )
        self.assertFalse(ok)
        self.assertIn("--target claude", detail)

        command = targets.shim_command(venv, shim, "claude")
        self.assertTrue(targets.register_claude(self.settings, command))
        ok, detail = targets.registered_in_config(
            "claude", meta, venv, shim
        )
        self.assertTrue(ok, detail)


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

    def test_translate_hermes_preserves_visible_system_message(self):
        stdout = json.dumps({
            "systemMessage": "「AgentRelay · 需求确认卡」",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "等待用户明确批准",
            },
        })
        self.assertEqual(
            json.loads(shim.translate_output(stdout, "hermes")),
            {
                "context": "等待用户明确批准",
                "systemMessage": "「AgentRelay · 需求确认卡」",
            },
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
        self.assertIn('import { Type } from "typebox"', content)
        self.assertIn('pi.on("tool_call"', content)
        self.assertIn('pi.on("tool_result"', content)
        self.assertIn('pi.on("message_end"', content)
        self.assertIn('pi.on("agent_settled"', content)
        self.assertIn('pi.on("agent_end"', content)
        self.assertIn('hook_event_name: "PostToolUse"', content)
        self.assertIn('hook_event_name: "Stop"', content)
        self.assertIn("event?.isError ? 1 : 0", content)
        self.assertIn('ctx?.ui?.notify', content)
        self.assertIn('block: true, reason: result.denial', content)
        self.assertIn('out?.systemMessage', content)
        self.assertIn('display: false', content)
        self.assertIn('registerCommand("agent-fusion"', content)
        self.assertIn('registerCommand("agent-approval"', content)
        self.assertIn('registerCommand("设置"', content)
        self.assertIn("async function approvalCommand", content)
        self.assertIn("async function approvalMenu", content)
        self.assertIn("APPROVAL_MENU_MODES", content)
        self.assertIn('name: FUSION_TOOL_NAME', content)
        self.assertIn('FUSION_TOOL_NAME = "agent_fusion_delegate"', content)
        self.assertIn("parameters: fusionDelegateSchema", content)
        self.assertIn('executionMode: "sequential"', content)
        self.assertIn("syncFusionTool(pi, fusionStatus())", content)
        self.assertIn("sessionManager.getSessionId()", content)
        self.assertIn('pi.on("session_start"', content)
        self.assertIn("result.systemPrompt", content)
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

    def test_registration_requires_explicit_pi_target(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-pi-registration-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        extension = tmp / "extensions" / "agent-relay.ts"
        targets.write_pi_extension(
            extension,
            Path("/venv/bin/python"),
            Path("/skill/scripts/agentrelay_hook_shim.py"),
        )
        meta = {"config_path": extension, "level": "full"}
        ok, detail = targets.registered_in_config("pi", meta, None, None)
        self.assertTrue(ok, detail)

    @unittest.skipUnless(shutil.which("node"), "Node.js 未安装")
    def test_generated_extension_bridges_pi_write_results_to_change_history(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-pi-history-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        workspace = tmp / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        codex_home = tmp / ".codex"
        subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts/approval_policy.py"),
                "set",
                "relaxed",
                "--codex-home",
                str(codex_home),
            ],
            check=True,
            capture_output=True,
        )

        extension = tmp / "extensions" / "agent-relay.ts"
        targets.write_pi_extension(
            extension,
            Path("/venv/bin/python"),
            PROJECT_ROOT / "scripts/agentrelay_hook_shim.py",
        )
        typebox = tmp / "node_modules" / "typebox"
        typebox.mkdir(parents=True)
        (typebox / "package.json").write_text(
            json.dumps({"name": "typebox", "type": "module", "main": "index.js"}),
            encoding="utf-8",
        )
        (typebox / "index.js").write_text(
            "export const Type = {\n"
            "  Object: (properties) => ({ type: 'object', properties }),\n"
            "  String: (options = {}) => ({ type: 'string', ...options }),\n"
            "  Optional: (schema) => schema,\n"
            "};\n",
            encoding="utf-8",
        )

        driver = tmp / "history-driver.mjs"
        driver.write_text(
            "import agentRelay from './extensions/agent-relay.ts';\n"
            "import { spawnSync } from 'node:child_process';\n"
            "const handlers = new Map();\n"
            "const notifications = [];\n"
            "const pi = {\n"
            "  registerTool() {},\n"
            "  registerCommand() {},\n"
            "  on(name, handler) { handlers.set(name, handler); },\n"
            "  getActiveTools() { return []; },\n"
            "  setActiveTools() {},\n"
            "};\n"
            "const ctx = {\n"
            f"  cwd: {json.dumps(str(workspace))},\n"
            "  sessionManager: { getSessionId() { return 'pi-history-session'; } },\n"
            "  hasUI: true,\n"
            "  isIdle() { return false; },\n"
            "  ui: { notify(message, type) { notifications.push({ message, type }); } },\n"
            "};\n"
            "await agentRelay(pi);\n"
            "await handlers.get('session_start')({}, ctx);\n"
            "const prompt = await handlers.get('before_agent_start')(\n"
            "  { prompt: '修复 app.py 里的一个拼写错误', systemPrompt: '' }, ctx);\n"
            "const selected = spawnSync(\n"
            "  process.env.AGENTRELAY_PYTHON,\n"
            "  [process.env.AGENTRELAY_TRACKER, 'select-work-profile',\n"
            "   '--session', 'pi-history-session', '--profile', 'light',\n"
            "   '--reason', 'Pi 黑盒测试选择轻量工作档'],\n"
            "  { encoding: 'utf-8', env: process.env },\n"
            ");\n"
            "if (selected.status !== 0) throw new Error(selected.stderr || selected.stdout);\n"
            "const toolCall = await handlers.get('tool_call')(\n"
            "  { toolName: 'edit', input: { path: 'app.py' }, toolCallId: 'pi-edit-1' }, ctx);\n"
            "const toolResult = await handlers.get('tool_result')(\n"
            "  { toolName: 'edit', input: { path: 'app.py' }, toolCallId: 'pi-edit-1',\n"
            "    content: [{ type: 'text', text: 'updated' }], isError: false }, ctx);\n"
            "const assistant = { role: 'assistant', content: [\n"
            "  { type: 'text', text: '已完成修改，拼写问题已修复。' },\n"
            "] };\n"
            "await handlers.get('message_end')({ message: assistant }, ctx);\n"
            "await handlers.get('agent_end')({ messages: [assistant], willContinue: false }, ctx);\n"
            "await handlers.get('agent_settled')({ aborted: false }, ctx);\n"
            "await handlers.get('agent_settled')({ aborted: false }, ctx);\n"
            "console.log(JSON.stringify({ prompt, toolCall, toolResult, notifications }));\n",
            encoding="utf-8",
        )

        env = {
            **os.environ,
            "HOME": str(tmp),
            "CODEX_HOME": str(codex_home),
            "AGENTRELAY_PYTHON": sys.executable,
            "AGENTRELAY_SHIM": str(PROJECT_ROOT / "scripts/agentrelay_hook_shim.py"),
            "AGENTRELAY_MAIN_HOOK": str(PROJECT_ROOT / "hooks/agent_relay_hook.py"),
            "AGENTRELAY_APPROVAL": str(PROJECT_ROOT / "scripts/approval_policy.py"),
            "AGENTRELAY_TRACKER": str(PROJECT_ROOT / "hooks/agent_relay_tracker.py"),
        }
        completed = subprocess.run(
            ["node", "--experimental-strip-types", str(driver)],
            cwd=tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        runtime = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertNotIn("toolCall", runtime)
        self.assertNotIn("toolResult", runtime)
        summaries = [
            item["message"]
            for item in runtime["notifications"]
            if "本次修改：" in item["message"]
        ]
        self.assertEqual(len(summaries), 1, runtime["notifications"])
        lines = summaries[0].splitlines()
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[1], "  📝 本次修改：")
        previous_index = lines.index("  🕘 上次修改：")
        self.assertTrue(
            all(
                not line or line.startswith("    ")
                for line in lines[2:previous_index]
            )
        )
        self.assertEqual(lines[previous_index + 1], "    暂无记录")
        self.assertNotIn("--------------------------------", summaries[0])
        self.assertNotIn("上上次修改", summaries[0])
        self.assertNotIn("这次改了哪些文件：", summaries[0])
        self.assertNotIn("app.py", summaries[0])
        self.assertIn("拼写问题已修复", summaries[0])
        history_dir = codex_home / "agent_relay_tracker" / "history" / "change-summaries"
        history_files = list(history_dir.glob("*.json"))
        self.assertEqual(len(history_files), 1)
        history = json.loads(history_files[0].read_text(encoding="utf-8"))
        self.assertEqual(len(history["entries"]), 1)
        self.assertNotIn("app.py", history["entries"][0]["summary"])
        self.assertIn("app.py", history["entries"][0]["changed_files"])

    @unittest.skipUnless(shutil.which("node"), "Node.js 未安装")
    def test_generated_extension_registers_and_executes_fusion_tool(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-pi-runtime-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        extension = tmp / "extensions" / "agent-relay.ts"
        targets.write_pi_extension(
            extension,
            Path("/venv/bin/python"),
            Path("/skill/scripts/agentrelay_hook_shim.py"),
        )

        # Pi supplies this virtual module at runtime. A minimal local shim keeps
        # this test independent from a globally installed Pi package.
        typebox = tmp / "node_modules" / "typebox"
        typebox.mkdir(parents=True)
        (typebox / "package.json").write_text(
            json.dumps({"name": "typebox", "type": "module", "main": "index.js"}),
            encoding="utf-8",
        )
        (typebox / "index.js").write_text(
            "export const Type = {\n"
            "  Object: (properties) => ({ type: 'object', properties }),\n"
            "  String: (options = {}) => ({ type: 'string', ...options }),\n"
            "  Optional: (schema) => schema,\n"
            "};\n",
            encoding="utf-8",
        )

        record = tmp / "fusion-args.jsonl"
        fake_fusion = tmp / "agent_fusion.py"
        fake_fusion.write_text(
            "import json, os, sys\n"
            "with open(os.environ['FUSION_RECORD'], 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "delegable = os.environ.get('FUSION_DELEGABLE', '1') == '1'\n"
            "state = {\n"
            "    'delegable': delegable,\n"
            "    'reason': 'READY' if delegable else 'FUSION_PROVIDER_NOT_CONFIGURED',\n"
            "    'tool_mode': 'readonly',\n"
            "    'max_steps': 8, 'provider': {'ref': 'api:test'},\n"
            "}\n"
            "if sys.argv[1:2] == ['status']:\n"
            "    result = {'status': 'success', 'state': state}\n"
            "else:\n"
            "    result = {'status': 'success', 'answer': 'fusion answer',\n"
            "              'session_id': 'session-123', 'steps': 2,\n"
            "              'tools_used': ['read_file'], 'tool_mode': 'readonly'}\n"
            "print('AGENT_FUSION_RESULT=' + json.dumps(result))\n",
            encoding="utf-8",
        )
        fake_shim = tmp / "agentrelay_hook_shim.py"
        fake_shim.write_text("import json\n"
                             "print(json.dumps({'systemMessage': '「AgentRelay · 写入确认卡」', "
                             "'hookSpecificOutput': {'permissionDecision': 'deny', "
                             "'permissionDecisionReason': 'scope denied'}}))\n", encoding="utf-8")

        driver = tmp / "runtime-driver.mjs"
        driver.write_text(
            "import agentRelay from './extensions/agent-relay.ts';\n"
            "const active = ['read', 'bash'];\n"
            "let tool;\n"
            "const handlers = new Map();\n"
            "const commands = new Map();\n"
            "const pi = {\n"
            "  registerTool(value) { tool = value; },\n"
            "  registerCommand(name, value) { commands.set(name, value); },\n"
            "  on(name, handler) { handlers.set(name, handler); },\n"
            "  getActiveTools() { return [...active]; },\n"
            "  setActiveTools(names) { active.splice(0, active.length, ...names); },\n"
            "};\n"
            "const notifications = [];\n"
            "const selections = [];\n"
            "const ctx = {\n"
            "  cwd: '/workspace/project',\n"
            "  sessionManager: { getSessionId() { return 'session-123'; } },\n"
            "  hasUI: true,\n"
            "  ui: {\n"
            "    notify(message, type) { notifications.push({message, type}); },\n"
            "    async select(prompt, choices) { selections.push({prompt, choices}); return choices[1]; },\n"
            "  },\n"
            "};\n"
            "await agentRelay(pi);\n"
            "await commands.get('agent-approval').handler('', ctx);\n"
            "await commands.get('设置').handler('1', ctx);\n"
            "await handlers.get('session_start')({}, ctx);\n"
            "const prompt = await handlers.get('before_agent_start')(\n"
            "  { prompt: 'inspect tests', systemPrompt: 'BASE PROMPT' }, ctx);\n"
            "if (!tool) throw new Error('tool was not registered');\n"
            "const availableActive = [...active];\n"
            "let update;\n"
            "const result = await tool.execute(\n"
            "  'call-1',\n"
            "  { task: 'inspect tests', context: 'Use rg', expect: 'cite files' },\n"
            "  undefined,\n"
            "  (value) => { update = value; },\n"
            "  ctx,\n"
            ");\n"
            "process.env.FUSION_DELEGABLE = '0';\n"
            "const unavailablePrompt = await handlers.get('before_agent_start')(\n"
            "  { prompt: 'inspect tests', systemPrompt: 'BASE PROMPT' }, ctx);\n"
            "const unavailableActive = [...active];\n"
            "const skipped = await tool.execute(\n"
            "  'call-2',\n"
            "  { task: 'inspect tests' },\n"
            "  undefined,\n"
            "  undefined,\n"
            "  ctx,\n"
            ");\n"
            "const toolGate = await handlers.get('tool_call')(\n"
            "  { toolName: 'write', input: { path: 'page.css' }, toolCallId: 'write-1' }, ctx);\n"
            "console.log(JSON.stringify({\n"
            "  toolName: tool.name,\n"
            "  executionMode: tool.executionMode,\n"
            "  availableActive,\n"
            "  unavailableActive,\n"
            "  prompt,\n"
            "  unavailablePrompt,\n"
            "  update,\n"
            "  result,\n"
            "  skipped,\n"
            "  toolGate,\n"
            "  notifications,\n"
            "  selections,\n"
            "}));\n",
            encoding="utf-8",
        )

        env = {
            **os.environ,
            "HOME": str(tmp),
            "CODEX_HOME": str(tmp / ".codex"),
            "AGENTRELAY_PYTHON": sys.executable,
            "AGENTRELAY_FUSION": str(fake_fusion),
            "AGENTRELAY_SHIM": str(fake_shim),
            "AGENTRELAY_APPROVAL": str(PROJECT_ROOT / "scripts" / "approval_policy.py"),
            "FUSION_RECORD": str(record),
        }
        completed = subprocess.run(
            ["node", "--experimental-strip-types", str(driver)],
            cwd=tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        runtime = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(runtime["toolName"], "agent_fusion_delegate")
        self.assertEqual(runtime["executionMode"], "sequential")
        self.assertIn("agent_fusion_delegate", runtime["availableActive"])
        self.assertNotIn("agent_fusion_delegate", runtime["unavailableActive"])
        self.assertTrue(runtime["prompt"]["systemPrompt"].startswith("BASE PROMPT"))
        self.assertIn(
            "Default to delegating and monitoring",
            runtime["prompt"]["systemPrompt"],
        )
        self.assertNotIn("systemPrompt", runtime.get("unavailablePrompt") or {})
        self.assertIn("Delegating to AgentRelay Fusion", runtime["update"]["content"][0]["text"])
        self.assertIn("fusion answer", runtime["result"]["content"][0]["text"])
        self.assertEqual(runtime["result"]["details"]["session_id"], "session-123")
        self.assertEqual(runtime["skipped"]["details"]["status"], "skipped")
        self.assertEqual(
            runtime["skipped"]["details"]["reason"],
            "FUSION_PROVIDER_NOT_CONFIGURED",
        )

        self.assertEqual(runtime["toolGate"], {"block": True, "reason": "scope denied"})
        self.assertTrue(any("写入确认卡" in item["message"] for item in runtime["notifications"]))
        self.assertEqual(len(runtime["selections"]), 1)
        self.assertIn("1. 严格", runtime["selections"][0]["choices"][0])
        self.assertTrue(any("2. 标准（默认）" in item["message"] for item in runtime["notifications"]))
        self.assertTrue(any("1. 严格" in item["message"] for item in runtime["notifications"]))
        saved_policy = json.loads((tmp / ".codex" / "agent_relay_tracker" / "approval-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_policy["mode"], "strict")

        calls = [
            json.loads(line)
            for line in record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        delegate_calls = [args for args in calls if args[:1] == ["delegate"]]
        self.assertEqual(len(delegate_calls), 1)
        self.assertEqual(
            delegate_calls[0],
            [
                "delegate",
                "inspect tests",
                "--workspace",
                "/workspace/project",
                "--session-id",
                "session-123",
                "--context",
                "Use rg\n\nExpected return:\ncite files",
            ],
        )


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
    def test_visible_card_protocol_rejects_plain_agentrelay_reminders(self):
        reminder = json.dumps({
            "systemMessage": "AgentRelay archive reminder",
        })
        card = json.dumps({
            "systemMessage": "「需求卡」\n- 任务：修改配置\n1. 确认审批",
        })

        self.assertFalse(targets.visible_card_in_stdout(reminder))
        self.assertTrue(targets.visible_card_in_stdout(card))

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
        for name in (
            "Skill 文件", "Fusion Skill", "Hook 配置", "Hook 干跑",
            "可见卡协议", "Tracker 状态",
        ):
            self.assertIn(name, results)
            self.assertTrue(results[name]["ok"], f"{name}: {results[name]['detail']}")

    def test_verify_claude_requires_a_visible_card_protocol(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-claude-verify-"))
        self.addCleanup(shutil.rmtree, tmp, True)
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
        settings = tmp / "settings.json"
        shim = PROJECT_ROOT / "scripts" / "agentrelay_hook_shim.py"
        command = targets.shim_command(Path(sys.executable), shim, "claude")
        targets.register_claude(settings, command)
        meta = {
            "id": "claude", "label": "Claude Code", "level": "full",
            "skill_dir": skill_dir, "hook_dir": tmp / "hooks",
            "config_path": settings,
        }

        checks = targets.verify_target(
            "claude", meta, Path(sys.executable),
            PROJECT_ROOT / "hooks" / "agent_relay_hook.py",
            shim,
            PROJECT_ROOT / "hooks" / "agent_relay_tracker.py",
            PROJECT_ROOT,
        )
        results = {check["name"]: check for check in checks}
        self.assertTrue(results["Hook 配置"]["ok"], results["Hook 配置"]["detail"])
        self.assertTrue(results["可见卡协议"]["ok"], results["可见卡协议"]["detail"])

    def test_verify_pi_requires_a_visible_card_protocol(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-pi-verify-"))
        self.addCleanup(shutil.rmtree, tmp, True)
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
        extension = tmp / "extensions" / "agent-relay.ts"
        shim = PROJECT_ROOT / "scripts" / "agentrelay_hook_shim.py"
        targets.write_pi_extension(extension, Path(sys.executable), shim)
        meta = {
            "id": "pi", "label": "Pi", "level": "full",
            "skill_dir": skill_dir, "hook_dir": tmp / "hooks",
            "config_path": extension,
        }

        checks = targets.verify_target(
            "pi", meta, Path(sys.executable),
            PROJECT_ROOT / "hooks" / "agent_relay_hook.py",
            shim,
            PROJECT_ROOT / "hooks" / "agent_relay_tracker.py",
            PROJECT_ROOT,
        )
        results = {check["name"]: check for check in checks}
        self.assertTrue(results["Hook 配置"]["ok"], results["Hook 配置"]["detail"])
        self.assertTrue(results["可见卡协议"]["ok"], results["可见卡协议"]["detail"])

    def test_verify_hermes_preserves_visible_card_protocol(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-hermes-verify-"))
        self.addCleanup(shutil.rmtree, tmp, True)
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
        config = tmp / "config.yaml"
        config.write_text(
            "hooks:\n" + "\n".join(
                f"  {event}:\n    - command: \"{targets.shim_command(Path(sys.executable), PROJECT_ROOT / 'scripts/agentrelay_hook_shim.py', 'hermes', canonical)}\""
                for event, canonical in targets.HERMES_EVENTS.items()
            ) + "\n",
            encoding="utf-8",
        )
        meta = {
            "id": "hermes", "label": "Hermes", "level": "full",
            "skill_dir": skill_dir, "hook_dir": tmp / "hooks", "config_path": config,
        }
        checks = targets.verify_target(
            "hermes", meta, Path(sys.executable),
            PROJECT_ROOT / "hooks" / "agent_relay_hook.py",
            PROJECT_ROOT / "scripts" / "agentrelay_hook_shim.py",
            PROJECT_ROOT / "hooks" / "agent_relay_tracker.py",
            PROJECT_ROOT,
        )
        results = {check["name"]: check for check in checks}
        self.assertTrue(results["可见卡协议"]["ok"], results["可见卡协议"]["detail"])

    def test_verify_pi_rejects_missing_prompt_notification(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-pi-negative-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        extension = tmp / "extensions" / "agent-relay.ts"
        targets.write_pi_extension(extension, Path(sys.executable), PROJECT_ROOT / "scripts/agentrelay_hook_shim.py")
        original = subprocess.run

        def missing_prompt_card(command, *args, **kwargs):
            result = original(command, *args, **kwargs)
            if command[0].endswith("node"):
                result.stdout = json.dumps({
                    "promptCard": False, "toolCard": True, "blocked": True,
                    "notifications": ["危险操作确认卡"],
                })
            return result

        with patch.object(targets.subprocess, "run", side_effect=missing_prompt_card):
            ok, detail = targets.verify_pi_visible_cards(
                extension, main_hook=PROJECT_ROOT / "hooks/agent_relay_hook.py",
                shim_path=PROJECT_ROOT / "scripts" / "agentrelay_hook_shim.py",
                workspace=PROJECT_ROOT, isolated_home=tmp,
                venv_python=Path(sys.executable), session_id="negative-pi",
            )
        self.assertFalse(ok)
        self.assertIn("需求卡", detail)

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

    def test_missing_delivery_audit_module_only_blocks_pending_audit(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-audit-fallback-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        session = "audit-fallback"
        with patch.object(tracker, "CODEX_DIR", tmp), patch.object(
            tracker, "_shared_script_module", side_effect=ImportError("missing")
        ):
            self.assertIsNone(
                tracker.delivery_audit_completion_block(session, "测试通过")
            )
            pending = tmp / "agent_relay_tracker" / "delivery-audit" / f"{session}.json"
            pending.parent.mkdir(parents=True)
            pending.write_text("{}", encoding="utf-8")
            reason = tracker.delivery_audit_completion_block(session, "测试通过")
        self.assertIn("审计模块不可用", reason)


if __name__ == "__main__":
    unittest.main()
