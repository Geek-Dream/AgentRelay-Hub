import importlib.util
import inspect
import json
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

import agent_fusion  # noqa: E402
import api_format_router  # noqa: E402
import config_manager  # noqa: E402
import fusion_mode  # noqa: E402
import fusion_runner  # noqa: E402
import research_commander  # noqa: E402
from orchestrator_core import Orchestrator  # noqa: E402


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FusionTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="agentrelay-fusion-test-"))
        self.lock_dir = self.home / "locks"
        self._old_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)

    def tearDown(self):
        if self._old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old_codex_home
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def configure_ready_fusion(self):
        config_manager.save_config({
            "fusion": {
                "enabled": True,
                "provider_id": "web:deepseek-web",
                "timeout": 12,
            },
            "web_providers": [{
                "id": "deepseek-web",
                "name": "DeepSeek",
                "base_url": "https://chat.deepseek.com/",
                "enabled": True,
                "conversation": {"modes": ["hybrid"]},
            }],
        }, self.home)


class FusionConfigTests(FusionTestBase):
    def test_package_import_matches_orchestrator_runtime(self):
        subprocess.run(
            [sys.executable, "-c", "import scripts.fusion_mode; import scripts.agent_fusion"],
            cwd=PROJECT_ROOT,
            check=True,
        )

    def test_old_config_gets_default_fusion_block(self):
        config_manager.save_config({
            "web_providers": [{
                "id": "deepseek-web",
                "base_url": "https://chat.deepseek.com/",
            }],
        }, self.home)
        loaded = config_manager.load_config(self.home)
        self.assertEqual(loaded["fusion"], {
            "enabled": True,
            "provider_id": "",
            "timeout": 30,
            "tool_mode": "readonly",
            "max_steps": 8,
        })

    def test_empty_provider_is_immediately_not_delegable(self):
        state = fusion_mode.fusion_state(home=self.home, lock_dir=self.lock_dir)
        self.assertTrue(state["enabled"])
        self.assertFalse(state["delegable"])
        self.assertEqual(state["reason"], "FUSION_PROVIDER_NOT_CONFIGURED")

    def test_empty_provider_never_reaches_provider_call(self):
        with mock.patch.object(fusion_runner, "run_sidekick") as run_sidekick:
            result = agent_fusion.delegate(
                "复核这个判断", home=self.home, lock_dir=self.lock_dir,
                environ={},
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "FUSION_PROVIDER_NOT_CONFIGURED")
        run_sidekick.assert_not_called()

    def test_web_provider_is_readonly_only(self):
        self.configure_ready_fusion()
        config = config_manager.load_config(self.home)
        config["fusion"]["tool_mode"] = "coding"
        config_manager.save_config(config, self.home)

        state = fusion_mode.fusion_state(
            home=self.home,
            lock_dir=self.lock_dir,
            environ={},
        )
        self.assertFalse(state["delegable"])
        self.assertFalse(state["tool_capable"])
        self.assertEqual(state["reason"], "FUSION_TOOL_MODE_UNSUPPORTED")

    def test_commands_reject_coding_with_a_web_provider(self):
        self.configure_ready_fusion()
        mode_result, mode_code = agent_fusion._mode_command(
            "coding",
            self.home,
            confirmed=True,
        )
        self.assertEqual(mode_code, 2)
        self.assertEqual(
            mode_result["error"],
            "FUSION_TOOL_MODE_UNSUPPORTED",
        )

        config = config_manager.load_config(self.home)
        config["fusion"]["provider_id"] = ""
        config["fusion"]["tool_mode"] = "coding"
        config_manager.save_config(config, self.home)
        provider_result, provider_code = agent_fusion._provider_command(
            "web:deepseek-web",
            self.home,
        )
        self.assertEqual(provider_code, 2)
        self.assertEqual(
            provider_result["error"],
            "FUSION_TOOL_MODE_UNSUPPORTED",
        )

    def test_timeout_is_clamped(self):
        config_manager.save_config({
            "fusion": {"enabled": True, "provider_id": "", "timeout": 999},
        }, self.home)
        self.assertEqual(config_manager.load_config(self.home)["fusion"]["timeout"], 120)

    def test_provider_selection_and_clear(self):
        config_manager.save_config({
            "web_providers": [{
                "id": "deepseek-web",
                "base_url": "https://chat.deepseek.com/",
            }],
        }, self.home)
        result, code = agent_fusion._provider_command("qianwen", self.home)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "FUSION_PROVIDER_NOT_FOUND")

        result, code = agent_fusion._provider_command("web:deepseek-web", self.home)
        self.assertEqual(code, 0)
        self.assertEqual(result["provider"]["ref"], "web:deepseek-web")

        result, code = agent_fusion._provider_command("none", self.home)
        self.assertEqual(code, 0)
        self.assertEqual(result["state"]["provider_id"], "")


class FusionProviderResolutionTests(unittest.TestCase):
    def test_local_and_api_providers_require_a_model(self):
        for kind in ("local", "api"):
            provider = {
                "kind": kind,
                "id": "incomplete",
                "ref": f"{kind}:incomplete",
                "entry": {"endpoint": "https://example.test/v1"},
            }
            self.assertFalse(fusion_mode.provider_is_ready(provider))
            provider["entry"]["model"] = "model-name"
            self.assertTrue(fusion_mode.provider_is_ready(provider))

    def test_exact_typed_provider_ids(self):
        config = {
            "web_providers": [{"id": "shared-web", "base_url": "https://example.test/"}],
            "local_providers": [{"id": "shared", "endpoint": "http://127.0.0.1:1/v1"}],
            "api_providers": [{"id": "shared", "endpoint": "https://api.test/v1"}],
        }
        local, local_error = fusion_mode.resolve_fusion_provider(config, "local:shared")
        api, api_error = fusion_mode.resolve_fusion_provider(config, "api:shared")
        self.assertEqual((local["ref"], local_error), ("local:shared", ""))
        self.assertEqual((api["ref"], api_error), ("api:shared", ""))

    def test_bare_id_across_kinds_is_ambiguous(self):
        config = {
            "local_providers": [{"id": "shared", "endpoint": "http://127.0.0.1:1/v1"}],
            "api_providers": [{"id": "shared", "endpoint": "https://api.test/v1"}],
        }
        provider, error = fusion_mode.resolve_fusion_provider(config, "shared")
        self.assertIsNone(provider)
        self.assertEqual(error, "FUSION_PROVIDER_AMBIGUOUS")

    def test_web_short_id_is_supported(self):
        config = {
            "web_providers": [{
                "id": "qianwen-web",
                "base_url": "https://www.qianwen.com/",
            }],
        }
        provider, error = fusion_mode.resolve_fusion_provider(config, "qianwen")
        self.assertEqual(provider["ref"], "web:qianwen-web")
        self.assertEqual(error, "")


class FusionCommanderMutualExclusionTests(FusionTestBase):
    def test_commander_lock_temporarily_blocks_fusion(self):
        self.configure_ready_fusion()
        self.assertTrue(fusion_mode.fusion_state(
            home=self.home, lock_dir=self.lock_dir, environ={}
        )["delegable"])

        with fusion_mode.fusion_commander_lock(
            "commander-task", home=self.home, lock_dir=self.lock_dir
        ):
            active = fusion_mode.fusion_state(
                home=self.home, lock_dir=self.lock_dir, environ={}
            )
            self.assertFalse(active["delegable"])
            self.assertEqual(active["reason"], "COMMANDER_ACTIVE")
            self.assertTrue(active["commander_active"])

        restored = fusion_mode.fusion_state(
            home=self.home, lock_dir=self.lock_dir, environ={}
        )
        self.assertTrue(restored["delegable"])
        self.assertTrue(config_manager.load_config(self.home)["fusion"]["enabled"])

    def test_enable_during_commander_does_not_bypass_lock(self):
        self.configure_ready_fusion()
        with fusion_mode.fusion_commander_lock(
            "commander-task", home=self.home, lock_dir=self.lock_dir
        ):
            config = config_manager.load_config(self.home)
            config["fusion"]["enabled"] = True
            config_manager.save_config(config, self.home)
            state = fusion_mode.fusion_state(
                home=self.home, lock_dir=self.lock_dir, environ={}
            )
        self.assertFalse(state["delegable"])
        self.assertEqual(state["reason"], "COMMANDER_ACTIVE")

    def test_commander_priority_overrides_saved_disabled_state(self):
        self.configure_ready_fusion()
        config = config_manager.load_config(self.home)
        config["fusion"]["enabled"] = False
        config_manager.save_config(config, self.home)
        with fusion_mode.fusion_commander_lock(
            "commander-task", home=self.home, lock_dir=self.lock_dir
        ):
            state = fusion_mode.fusion_state(
                home=self.home, lock_dir=self.lock_dir, environ={}
            )
        self.assertFalse(state["enabled"])
        self.assertFalse(state["delegable"])
        self.assertEqual(state["reason"], "COMMANDER_ACTIVE")

    def test_commander_child_is_always_blocked(self):
        self.configure_ready_fusion()
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                config = config_manager.load_config(self.home)
                config["fusion"]["enabled"] = enabled
                config_manager.save_config(config, self.home)
                state = fusion_mode.fusion_state(
                    home=self.home,
                    lock_dir=self.lock_dir,
                    environ={"AGENTRELAY_COMMANDER_CHILD": "1"},
                )
                self.assertFalse(state["delegable"])
                self.assertEqual(state["reason"], "COMMANDER_CHILD")

    def test_commander_process_marks_child_environment(self):
        self.configure_ready_fusion()
        agent = research_commander.CommanderAgent(
            "agent-1", "backend-code", self.home, model_config={"task_id": "task-1"}
        )

        class FakeProcess:
            pid = 12345
            returncode = 0

            def communicate(self, timeout=None):
                return "ok", ""

        with mock.patch.object(
            research_commander.subprocess, "Popen", return_value=FakeProcess()
        ) as popen:
            result = research_commander.CommanderProcess().run(
                agent, [sys.executable, "-c", "print('ok')"], timeout=1
            )

        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(child_env["AGENTRELAY_COMMANDER_CHILD"], "1")
        state = fusion_mode.fusion_state(
            home=self.home, lock_dir=self.lock_dir, environ=child_env
        )
        self.assertFalse(state["delegable"])
        self.assertEqual(state["reason"], "COMMANDER_CHILD")

    def test_multiple_commander_tasks_can_run_in_parallel(self):
        self.configure_ready_fusion()
        with fusion_mode.fusion_commander_lock(
            "task-a", home=self.home, lock_dir=self.lock_dir
        ), fusion_mode.fusion_commander_lock(
            "task-b", home=self.home, lock_dir=self.lock_dir
        ):
            state = fusion_mode.fusion_state(
                home=self.home, lock_dir=self.lock_dir, environ={}
            )
        self.assertEqual(
            {item["task_id"] for item in state["active_commander_tasks"]},
            {"task-a", "task-b"},
        )

    def test_commander_result_receipts_are_scoped_to_preempted_sessions(self):
        self.configure_ready_fusion()
        run_a = fusion_mode.start_fusion_run(
            session_id="session-a",
            workspace=self.home,
            task="task a",
            tool_mode="readonly",
            home=self.home,
        )
        run_b = fusion_mode.start_fusion_run(
            session_id="session-b",
            workspace=self.home,
            task="task b",
            tool_mode="readonly",
            home=self.home,
        )
        try:
            with fusion_mode.fusion_commander_lock(
                "scoped-task",
                home=self.home,
                lock_dir=self.lock_dir,
            ):
                pass
        finally:
            fusion_mode.finish_fusion_run(run_a["run_id"], home=self.home)
            fusion_mode.finish_fusion_run(run_b["run_id"], home=self.home)

        receipt_a = fusion_mode.commander_results(
            home=self.home,
            session_id="session-a",
        )
        receipt_b = fusion_mode.commander_results(
            home=self.home,
            session_id="session-b",
        )
        unrelated = fusion_mode.commander_results(
            home=self.home,
            session_id="session-c",
        )
        self.assertEqual(len(receipt_a), 1)
        self.assertEqual(len(receipt_b), 1)
        self.assertEqual(receipt_a[0]["task_id"], "scoped-task")
        self.assertEqual(
            set(receipt_a[0]["affected_sessions"]),
            {"session-a", "session-b"},
        )
        self.assertEqual(unrelated, [])

    def test_stale_commander_lock_is_removed(self):
        self.lock_dir.mkdir(parents=True)
        stale = self.lock_dir / "commander-stale.json"
        stale.write_text(json.dumps({
            "task_id": "dead-task",
            "pid": 2147483647,
        }), encoding="utf-8")
        state = fusion_mode.fusion_state(
            home=self.home, lock_dir=self.lock_dir, environ={}
        )
        self.assertFalse(state["commander_active"])
        self.assertFalse(stale.exists())

    def test_offline_commander_plan_holds_fusion_lock(self):
        self.configure_ready_fusion()
        observed = {}

        def fake_run_commander(orchestrator, task_id, request, **_kwargs):
            state = fusion_mode.fusion_state(home=self.home)
            observed.update(state)
            return ()

        with mock.patch.object(Orchestrator, "run_commander", new=fake_run_commander):
            result = Orchestrator().run_commander_auto(
                "offline-task", "检查前端和后端", self.home, native=False
            )

        self.assertEqual(result["mode"], "offline-plan")
        self.assertFalse(observed["delegable"])
        self.assertEqual(observed["reason"], "COMMANDER_ACTIVE")


class FusionProviderCallTests(unittest.TestCase):
    def test_non_chat_api_uses_existing_format_router(self):
        response = {
            "id": "resp-1",
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "router answer"}],
            }],
        }
        entry = {
            "id": "responses-api",
            "endpoint": "https://api.example/v1",
            "format": "responses",
            "model": "fusion-model",
        }
        with mock.patch.object(
            api_format_router,
            "call_upstream",
            return_value=(200, "application/json", json.dumps(response).encode("utf-8")),
        ) as call_upstream:
            answer = agent_fusion._call_formatted_api(entry, "question", 9)
        self.assertEqual(answer, "router answer")
        payload = call_upstream.call_args.args[1]
        self.assertEqual(payload["model"], "fusion-model")
        self.assertIn("input", payload)
        self.assertNotIn("messages", payload)


class FusionInstallTests(unittest.TestCase):
    def test_pi_extension_registers_command_and_gates_context(self):
        targets = load_module("fusion_agent_targets", SCRIPTS_DIR / "agent_targets.py")
        tmp = Path(tempfile.mkdtemp(prefix="agentrelay-fusion-pi-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        extension = tmp / "extensions" / "agent-relay.ts"
        fusion = tmp / "skills" / "agent-fusion" / "scripts" / "agent_fusion.py"
        self.assertTrue(targets.write_pi_extension(
            extension,
            Path("/venv/bin/python"),
            tmp / "skills" / "agent-relay" / "scripts" / "agentrelay_hook_shim.py",
            fusion,
        ))
        content = extension.read_text(encoding="utf-8")
        self.assertIn('registerCommand("agent-fusion"', content)
        self.assertIn('fusion(["status", "--json"])', content)
        self.assertIn("state?.delegable", content)
        self.assertIn("return item.label + \" - \" + item.description;", content)
        self.assertIn("查看当前状态", content)
        self.assertIn("检查开关、Commander 最高优先级互斥", content)
        self.assertIn("选择 Provider", content)
        self.assertIn("从本机已配置", content)
        self.assertIn("await ctx.ui.select", content)
        self.assertNotIn(str(fusion), content)
        self.assertIn("AGENTRELAY_FUSION", content)
        self.assertIn('join(VENV, "Scripts", "python.exe")', content)

    def test_installer_manifest_includes_fusion_skill_and_scripts(self):
        installer = load_module("fusion_install_module", PROJECT_ROOT / "install.py")
        self.assertIn("agent_fusion.py", installer.SCRIPT_FILES)
        self.assertIn("fusion_mode.py", installer.SCRIPT_FILES)
        self.assertEqual(
            set(installer.FUSION_SCRIPT_FILES),
            {"agent_fusion.py", "fusion_mode.py", "fusion_runner.py"},
        )
        self.assertEqual(installer.FUSION_SKILL_FILES, ("SKILL.md",))

    def test_orchestrator_guards_both_commander_entry_points(self):
        auto_source = inspect.getsource(Orchestrator.run_commander_auto)
        process_source = inspect.getsource(Orchestrator.run_commander_processes)
        self.assertIn("with fusion_commander_lock(task_id):", auto_source)
        self.assertIn("with fusion_commander_lock(task_id):", process_source)


if __name__ == "__main__":
    unittest.main()
