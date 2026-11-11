import importlib.util
import tempfile
import unittest
import json
import subprocess
import os
import shlex
from unittest import mock
from types import SimpleNamespace
from scripts.task_scheduler import TaskAnalyzer, TaskPlan, TaskScheduler, ModelRegistry as SchedulerRegistry, TaskDecisionEngine
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

from hooks.agent_relay_tracker import calculate_weight, get_trigger_conditions
from scripts.orchestrator_runtime import ModelRegistry, route_task
from scripts.orchestrator_store import CheckpointStore, KnowledgeRecord, MemoryStore
from scripts.orchestrator_dispatcher import Dispatcher
from scripts.orchestrator_store import DispatchRequest
from scripts.orchestrator_core import (Orchestrator, TaskType, classify_task,
                                        SelfWorker, SelfReviewer, VerificationRunner, ConfirmationStore, FallbackExecutor,
                                        ConfirmationCard, default_compileall_command)
from scripts.orchestrator_core import WorkflowEngine, TaskExecutionState, TaskStateMachine, TaskContext
from scripts import commander_provider_pool, fusion_mode, orchestrator_core
from scripts.agentrelay_console import (
    ANSI_BLUE_BOLD,
    ANSI_GREEN,
    ANSI_RED_BOLD,
    ANSI_RESET,
    CODEX_DIVIDER,
    render_confirmation_card,
    render_requirement_card,
    resolve_card_style,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def shlex_command(*parts):
    return shlex.join([str(part) for part in parts])


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMsvcrt(SimpleNamespace):
    def __init__(self):
        super().__init__(LK_NBLCK=1, LK_UNLCK=2)
        self.calls = []

    def locking(self, file_descriptor, mode, size):
        self.calls.append((mode, size))


class OrchestratorTests(unittest.TestCase):
    def test_task_analyzer_simple(self):
        plan = TaskAnalyzer().analyze("把页面XP改成20")
        self.assertEqual(plan.task_type, "SIMPLE_TASK"); self.assertEqual(plan.difficulty, 1)

    def test_multiple_ordinary_changes_use_one_confirmation_card(self):
        plan = TaskAnalyzer().analyze("把按钮颜色改成蓝色并把圆角改成 4px")
        self.assertEqual(plan.task_type, "CONFIRMATION_TASK")
        outcome = Orchestrator().execute_task("batch-ui", "把按钮颜色改成蓝色并把圆角改成 4px",
                                              allowed_files=("button.css",))
        self.assertNotIn("card", outcome)
        self.assertEqual(outcome["confirmation"].action, "execute")

    def test_custom_openai_compatible_api_provider_is_not_brand_bound(self):
        config = json.dumps([{"id": "kimi-api", "endpoint": "https://relay.example/v1",
                              "api_key_env": "KIMI_API_KEY", "model": "kimi-model"}])
        with mock.patch.dict(os.environ, {"AGENTRELAY_API_PROVIDERS_JSON": config,
                                          "KIMI_API_KEY": "test-key"}, clear=False):
            engine = WorkflowEngine.from_environment()
        self.assertIn("kimi-api", engine.provider_options)

    def test_decision_engine_requirement_card_rules(self):
        simple = TaskDecisionEngine().decide("修改颜色")
        self.assertFalse(simple.need_requirement_card)
        complex_plan = TaskDecisionEngine().decide("数据库迁移", modules=("api", "db"))
        self.assertTrue(complex_plan.need_requirement_card)

    def test_task_analyzer_complex(self):
        plan = TaskAnalyzer().analyze("将 MQ 重构为 Kafka", modules=("api", "worker"))
        self.assertEqual(plan.task_type, "PROJECT_COMMANDER_TASK"); self.assertTrue(plan.need_confirmation)

    def test_scheduler_local_and_fallback(self):
        scheduler = TaskScheduler(SchedulerRegistry({"local-model": {"type": "local", "available": True}}))
        self.assertEqual(scheduler.select(TaskPlan("SIMPLE_TASK", 1, "low", False, "local-model", "simple", "")), "local-model")
        self.assertIn(TaskScheduler().select(TaskPlan("SIMPLE_TASK", 1, "low", False, "local-model", "simple", "")), {"codex-self", "codex-subagent"})

    def test_block_weight_trigger(self):
        plan = TaskAnalyzer().analyze("修复失败", retry_count=3)
        self.assertEqual(plan.task_type, "BLOCKED_TASK")
        self.assertEqual(TaskScheduler().blocked_advice(plan), "codex-subagent")
    def test_weight_and_time_trigger(self):
        self.assertEqual(calculate_weight(299), 1)
        self.assertEqual(calculate_weight(300), 2)
        self.assertEqual(calculate_weight(600), 2)
        self.assertEqual(calculate_weight(900), 3)
        state = {"problem_id": "t", "round_effective_time_seconds": 900,
                 "retry_count": 0, "relay_triggered": False,
                 "relay_signal_emitted": False}
        self.assertTrue(get_trigger_conditions(state)["should_trigger"])

    def test_registry_routing(self):
        registry = ModelRegistry.from_json(Path(__file__).parents[1] / "scripts/model_registry.json")
        self.assertEqual(route_task(complexity=1, needs_edit=True, registry=registry).level, "direct")
        self.assertEqual(route_task(complexity=2, needs_web=True, registry=registry).model, "deepseek-web")
        self.assertEqual(route_task(complexity=5, registry=registry).level, "commander")

    def test_memory_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = MemoryStore(root / "memory.json")
            memory.add(KnowledgeRecord(problem="redis dependency", solution=["use lettuce"]))
            self.assertEqual(len(memory.search("redis")), 1)
            record = memory.search("redis")[0]
            self.assertFalse(record.verified)
            self.assertTrue(memory.mark_verified(record.record_id))
            self.assertTrue(memory.search("redis")[0].verified)
            checkpoint = CheckpointStore(root / "checkpoints", max_checkpoints=3)
            self.assertTrue(checkpoint.save("task", "change", [{"path": "a", "diff": "x"}]).exists())

    def test_dispatcher_guards_expert_and_provider(self):
        dispatcher = Dispatcher(providers={})
        request = DispatchRequest(task_id="t", level="expert", model="deepseek-web", prompt="分析问题")
        result = dispatcher.dispatch(request)
        self.assertEqual(result.status, "failed")
        blocked = Dispatcher().dispatch(DispatchRequest(task_id="t", level="expert", model="deepseek-web", prompt="x", read_only=False, allow_file_write=True))
        self.assertEqual(blocked.status, "rejected")

    def test_dispatcher_returns_provider_result(self):
        class FakeProvider:
            provider_id = "fake"
            def check_available(self): return True
            def dispatch(self, request):
                from scripts.orchestrator_store import DispatchResult
                return DispatchResult(status="success", task_id=request.task_id,
                                      request_id=request.request_id, mode=request.level,
                                      provider_id=self.provider_id, output="advice")
        result = Dispatcher({"fake": FakeProvider()}).dispatch(
            DispatchRequest(task_id="t", request_id="r", level="expert",
                            model="fake", prompt="analyze"))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.output, "advice")

    def test_default_deepseek_checks_login_state(self):
        from scripts.orchestrator_dispatcher import DeepSeekWebProvider
        import os
        old = os.environ.get("CODEX_HOME")
        with tempfile.TemporaryDirectory() as directory:
            os.environ["CODEX_HOME"] = directory
            self.assertFalse(DeepSeekWebProvider().check_available())
        if old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old

    def test_deepseek_passes_request_timeout(self):
        from scripts import orchestrator_dispatcher as module
        provider = module.DeepSeekWebProvider()
        captured = {}
        old_run = module.__dict__.get("run_provider")
        class FakeProvider(module.DeepSeekWebProvider):
            pass
        import sys, types
        fake = types.SimpleNamespace(run_provider=lambda *args, **kwargs: captured.update(kwargs) or {"answer": "ok"})
        sys.modules["scripts.agent_relay"] = fake
        try:
            result = provider.dispatch(DispatchRequest(task_id="t", level="expert", model="deepseek-web", prompt="x", timeout_seconds=42))
            self.assertEqual(result.status, "success")
            self.assertEqual(captured["timeout"], 42)
            self.assertEqual(captured["provider_name"], "deepseek")
        finally:
            sys.modules.pop("scripts.agent_relay", None)

    def test_deepseek_legacy_alias_is_accepted(self):
        dispatcher = Dispatcher({"deepseek": type("P", (), {"check_available": lambda self: False})()})
        result = dispatcher.dispatch(DispatchRequest(task_id="t", level="expert", model="deepseek", prompt="x"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "PROVIDER_UNAVAILABLE")

    def test_deepseek_web_uses_legacy_login_state(self):
        from scripts.agent_relay_runtime import provider_state_file
        root = Path("/tmp/orchestrator-test-codex")
        self.assertEqual(provider_state_file("deepseek-web", root), root / "skills/agent-relay/agent_relay_login_state.json")

    def test_no_external_model_simple_flow(self):
        orchestrator = Orchestrator()
        result, review, verification = orchestrator.run_simple("simple-1", "把 XP 改为 20", ["page.vue"])
        self.assertEqual(result.provider, "codex-subagent")
        self.assertEqual(result.status, "success")
        self.assertTrue(review.approved)
        self.assertTrue(verification.passed)
        self.assertEqual(len(orchestrator.changelog), 1)

    def test_classifier_and_strict_workflow(self):
        self.assertEqual(classify_task("查询 Redis 版本兼容性"), TaskType.TECHNICAL_RESEARCH)
        card, assessment, confirmation = Orchestrator().create_requirement("complex-1", "将 RabbitMQ 重构为 Kafka", 5, ["frontend", "backend"], ["Mq.java"])
        self.assertEqual(card.status, "awaiting_confirmation")
        self.assertTrue(assessment.needs_confirmation)
        self.assertTrue(assessment.needs_commander)
        self.assertEqual(confirmation.status, "pending")

    def test_commander_reports_and_review_rejects_scope(self):
        orchestrator = Orchestrator()
        reports = orchestrator.run_commander("project-1", "跨模块改造")
        self.assertEqual(len(reports), 3)
        result = SelfWorker().execute("t", "change", ["allowed.py"])
        bad = result.__class__(result.task_id, result.status, result.summary, ("other.py",), result.diff_summary, result.validation, result.risks, result.failure_reason, result.provider)
        self.assertFalse(SelfReviewer().review(bad, ["allowed.py"]).approved)

    def test_execute_task_uses_memory_and_self_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / "memory.json")
            orchestrator = Orchestrator(memory=memory)
            orchestrator.record_verified_solution("XP 样式", ["修改 page.vue"])
            outcome = orchestrator.execute_task("simple-2", "修改 XP 样式", allowed_files=["page.vue"])
            self.assertEqual(outcome["task_type"], "SIMPLE_TASK")
            self.assertEqual(len(outcome["memory_hits"]), 1)
            self.assertTrue(outcome["review"].approved)

    def test_scheduler_is_in_execute_task_result(self):
        outcome = Orchestrator().execute_task("brain", "修改一个变量")
        self.assertIn("decision", outcome)
        self.assertFalse(outcome["decision"].need_requirement_card)

    def test_requirement_card_is_rule_generated_and_carries_execution_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Mq.java"
            target.write_text("rabbitmq", encoding="utf-8")
            orchestrator = Orchestrator()
            outcome = orchestrator.execute_task(
                "mq-1", "把 MQ 改成 Kafka", modules=("backend", "config"),
                allowed_files=("Mq.java",), workspace=directory,
                edits={"Mq.java": "kafka"},
                verification_commands=("python3 -m compileall -q scripts",))
            self.assertTrue(outcome["decision"].need_requirement_card)
            self.assertIn("Kafka", outcome["card"].target_state)
            self.assertIn("Kafka", outcome["card"].acceptance_criteria[-2])
            self.assertEqual(outcome["confirmation"].workspace, directory)
            self.assertEqual(outcome["confirmation"].edits["Mq.java"], "kafka")
            self.assertTrue(outcome["card"].affected_files)
            self.assertTrue(outcome["card"].risk_analysis)
            self.assertTrue(outcome["card"].rollback_plan)

    def test_workflow_engine_approve_and_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "db.sql"
            target.write_text("old", encoding="utf-8")
            engine = WorkflowEngine(Orchestrator())
            submitted = engine.submit("wf-1", "修改数据库结构", modules=("api",),
                                      allowed_files=("db.sql",), workspace=directory,
                                      edits={"db.sql": "new"})
            self.assertEqual(submitted["confirmation"].status, "pending")
            staged = engine.approve_and_execute("wf-1")
            self.assertEqual(staged["status"], "awaiting_execution_confirmation")
            done = engine.approve_and_execute("wf-1")
            self.assertEqual(done["status"], "completed")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_task_state_machine_rejects_illegal_transition(self):
        machine = TaskStateMachine()
        with self.assertRaises(ValueError):
            machine.transition(TaskExecutionState.SUCCESS)
        for state in (TaskExecutionState.ANALYZING, TaskExecutionState.WAITING_CONFIRMATION,
                      TaskExecutionState.APPROVED, TaskExecutionState.EXECUTING,
                      TaskExecutionState.VERIFYING, TaskExecutionState.SUCCESS):
            machine.transition(state)

    def test_task_context_persists_and_escalates(self):
        with tempfile.TemporaryDirectory() as directory:
            context = TaskContext("ctx", directory, "失败任务")
            context.record_failure(elapsed_seconds=300)
            context.record_failure(elapsed_seconds=300)
            context.record_failure(elapsed_seconds=300)
            self.assertTrue(context.need_escalation)
            restored = TaskContext.load(context.save(Path(directory) / "tasks"))
            self.assertEqual(restored.retry_count, 3)
            self.assertEqual(restored.weight, 3)

    def test_simple_task_does_not_create_requirement_card_and_executes_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "x.txt"
            target.write_text("old", encoding="utf-8")
            orchestrator = Orchestrator()
            outcome = orchestrator.execute_task("simple-3", "修改变量", allowed_files=("x.txt",),
                                                workspace=directory, edits={"x.txt": "new"})
            self.assertNotIn("card", outcome)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_rejected_requirement_cannot_execute(self):
        orchestrator = Orchestrator()
        outcome = orchestrator.execute_task("reject-1", "重构订单数据库", modules=("api", "db"))
        orchestrator.confirmations.transition("reject-1", "rejected")
        with self.assertRaises(PermissionError):
            orchestrator.execute_confirmed_task("reject-1")

    def test_rollback_requires_confirmation_and_is_per_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "owned.txt"
            other = Path(directory) / "unrelated.txt"
            target.write_text("new", encoding="utf-8")
            other.write_text("keep", encoding="utf-8")
            orchestrator = Orchestrator()
            plan = orchestrator.rollback_plan("task-r", [str(target)], {str(target): "new", str(other): "other"}, {str(target): "old", str(other): "bad"})
            with self.assertRaises(PermissionError):
                orchestrator.apply_rollback("task-r", plan)
            log = orchestrator.apply_rollback("task-r", plan, confirmed=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(other.read_text(encoding="utf-8"), "keep")
            self.assertEqual(log.operation, "rollback")

    def test_self_worker_real_edit_and_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "page.vue"
            target.write_text("color: gray", encoding="utf-8")
            dry = SelfWorker().execute("t", "red", ["page.vue"], root, {"page.vue": "color: red"}, dry_run=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "color: gray")
            self.assertEqual(dry.changed_files, ("page.vue",))
            real = SelfWorker().execute("t", "red", ["page.vue"], root, {"page.vue": "color: red"})
            self.assertEqual(target.read_text(encoding="utf-8"), "color: red")
            self.assertEqual(real.before_snapshot["page.vue"], dry.before_snapshot["page.vue"])

    def test_verification_runner_success_failure_timeout_and_rejection(self):
        runner = VerificationRunner()
        root = Path.cwd()
        self.assertIn(default_compileall_command(),
                      {"python3 -m compileall -q scripts", "py -m compileall -q scripts"})
        self.assertEqual(runner.run("python3 -m compileall -q scripts", root).status, "passed")
        self.assertEqual(runner.run("python3 -c exit(1)", root).status, "rejected")
        self.assertIn(runner.run("python3 -m unittest", root, timeout_seconds=0).status, {"timeout", "failed"})
        self.assertEqual(runner.run("rm -rf .", root).status, "rejected")

    def test_confirmation_state_machine(self):
        store = ConfirmationStore()
        from scripts.orchestrator_core import ConfirmationCard
        store.create(ConfirmationCard("t", "execute", ("a",), ("check",)))
        self.assertFalse(store.can_execute("t"))
        store.transition("t", "approved")
        self.assertTrue(store.can_execute("t"))
        store.transition("t", "executing")
        with self.assertRaises(ValueError):
            store.transition("t", "approved")

    def test_confirmation_store_persists_atomically_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = ConfirmationStore(path)
            from scripts.orchestrator_core import ConfirmationCard
            store.create(ConfirmationCard("a", "execute", ("x.py",), ("test",)))
            self.assertFalse(store.can_execute("a"))
            store.transition("a", "approved")
            self.assertTrue(store.can_execute("a"))
            restored = ConfirmationStore(path)
            self.assertTrue(restored.can_execute("a"))
            restored.transition("a", "executing")
            with self.assertRaises(ValueError):
                restored.transition("a", "approved")
            (path / "broken.json").write_text("not json", encoding="utf-8")
            self.assertFalse(ConfirmationStore(path).can_execute("broken"))

    def test_execute_confirmed_task_persists_lifecycle_and_blocks_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "x.txt"; target.write_text("old", encoding="utf-8")
            confirmations = ConfirmationStore(root / "tasks")
            orchestrator = Orchestrator(checkpoints=CheckpointStore(root / "checkpoints"), confirmations=confirmations)
            orchestrator.create_requirement("confirmed", "edit x", 4, (), ("x.txt",))
            with self.assertRaises(PermissionError):
                orchestrator.execute_confirmed_task("confirmed", workspace=root, edits={"x.txt": "new"})
            confirmations.transition("confirmed", "approved")
            outcome = orchestrator.execute_confirmed_task("confirmed", workspace=root,
                                                           edits={"x.txt": "new"},
                                                           verification_commands=["python3 -m compileall -q ."])
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(confirmations.cards["confirmed"].status, "completed")
            with self.assertRaises(PermissionError):
                orchestrator.execute_confirmed_task("confirmed", workspace=root)

    def test_execute_confirmed_task_failure_persists_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "x.txt").write_text("old", encoding="utf-8")
            confirmations = ConfirmationStore(root / "tasks")
            orchestrator = Orchestrator(checkpoints=CheckpointStore(root / "checkpoints"), confirmations=confirmations)
            orchestrator.create_requirement("failed-task", "edit x", 4, (), ("x.txt",))
            confirmations.transition("failed-task", "approved")
            outcome = orchestrator.execute_confirmed_task("failed-task", workspace=root,
                                                           edits={"x.txt": "new"},
                                                           verification_commands=["python3 -m unittest no_such_test"])
            self.assertEqual(outcome["status"], "failed")
            self.assertEqual(confirmations.cards["failed-task"].status, "failed")

    def test_dispatcher_default_is_offline(self):
        request = DispatchRequest(task_id="offline", level="expert", model="deepseek-web", prompt="x")
        self.assertEqual(Dispatcher().dispatch(request).error.code, "PROVIDER_NOT_CONFIGURED")

    def test_provider_error_is_redacted(self):
        from scripts.orchestrator_dispatcher import DeepSeekWebProvider
        self.assertNotIn("secret", DeepSeekWebProvider._safe_error("token=secret cookie=abc password=hunter2"))

    @unittest.skipUnless(os.environ.get("RUN_EXTERNAL_PROVIDER_TESTS") == "1", "set RUN_EXTERNAL_PROVIDER_TESTS=1 for external integration tests")
    def test_external_provider_integration_opt_in(self):
        from scripts.orchestrator_dispatcher import DeepSeekWebProvider
        self.assertIsInstance(DeepSeekWebProvider().check_available(), bool)

    def test_task_cli_lists_shows_and_approves_persisted_tasks(self):
        from scripts.orchestrator_task import main
        with tempfile.TemporaryDirectory() as directory:
            store = ConfirmationStore(Path(directory) / "tasks")
            store.create(__import__("scripts.orchestrator_core", fromlist=["ConfirmationCard"]).ConfirmationCard(
                "cli-task", "execute", ("a.py",), ("check",), request="edit a", workspace=directory,
                allowed_files=("a.py",)))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--root", directory, "task", "list"]), 0)
            self.assertIn("cli-task", output.getvalue())
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["--root", directory, "task", "approve", "cli-task"]), 0)
            self.assertEqual(ConfirmationStore(Path(directory) / "tasks").cards["cli-task"].status, "approved")

    def test_task_cli_json_and_errors_are_machine_readable(self):
        from scripts.orchestrator_task import main
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--root", directory, "--json", "task", "show", "missing"]), 1)
            self.assertEqual(json.loads(output.getvalue())["error"].startswith("Task missing"), True)

    def test_recovery_and_safe_retry_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store = ConfirmationStore(root / "tasks")
            from scripts.orchestrator_core import ConfirmationCard
            store.create(ConfirmationCard("r", "execute", (), (), "executing", request="x"))
            store.mark_recovery_required("r", "process interrupted")
            self.assertEqual(ConfirmationStore(root / "tasks").cards["r"].status, "recovery_required")
            store.recover("r", "fail", "operator stopped")
            self.assertEqual(store.cards["r"].status, "failed")
            store.retry_failed("r")
            self.assertEqual(store.cards["r"].status, "approved")
            store.cards["r"] = ConfirmationCard("r", "execute", (), (), "failed", request="x", attempts=2)
            with self.assertRaises(ValueError): store.retry_failed("r")

    def test_cli_subprocess_json_and_missing_task(self):
        with tempfile.TemporaryDirectory() as directory:
            cmd=["python3","-m","scripts.orchestrator_task","--root",directory,"--json","task","list"]
            p=subprocess.run(cmd,capture_output=True,text=True,cwd=Path(__file__).parents[1])
            self.assertEqual(p.returncode,0); self.assertEqual(json.loads(p.stdout),[]); self.assertEqual(p.stderr,"")
            p=subprocess.run(["python3","-m","scripts.orchestrator_task","--root",directory,"task","show","missing"],capture_output=True,text=True,cwd=Path(__file__).parents[1])
            self.assertNotEqual(p.returncode,0); self.assertIn("not found",p.stderr)

    def test_fallback_and_task_call_budget(self):
        def unavailable(task_id, prompt):
            from scripts.orchestrator_core import AgentResult
            return AgentResult(task_id, "failed", failure_reason="down", provider="external")
        executor = FallbackExecutor(unavailable, max_calls=1)
        result = executor.execute("t", "research")
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.provider, "codex-subagent")
        self.assertEqual(executor.execute("t", "again").failure_reason, "MAX_CALLS_EXCEEDED")

    def test_failure_memory_record_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / "memory.json")
            record = Orchestrator(memory=memory).record_failure("t", "SIMPLE_TASK", "edit failed", failure_reason="scope")
            self.assertFalse(record.verified)
            self.assertIn("失败: scope", record.solution)
            self.assertEqual(len(memory.search("edit failed")), 1)

    def test_workspace_checkpoint_and_safe_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "owned.txt"; target.write_text("before", encoding="utf-8")
            store = CheckpointStore(root / "checkpoints")
            checkpoint = store.create_from_workspace("task", root, ["owned.txt", "new.txt", "gone.txt"])
            target.write_text("after", encoding="utf-8")
            (root / "new.txt").write_text("created", encoding="utf-8")
            payload = store.finalize_from_workspace(checkpoint, root)
            self.assertEqual(set(payload["changed_files"]), {"owned.txt", "new.txt"})
            log = Orchestrator().apply_rollback("task", payload, confirmed=True, workspace=root)
            detail = json.loads(log.rollback)
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertFalse((root / "new.txt").exists())
            self.assertEqual(set(detail["restored_files"]), {"owned.txt", "new.txt"})

    def test_rollback_refuses_user_changes_and_workspace_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "owned.txt"; target.write_text("before", encoding="utf-8")
            store = CheckpointStore(root / "checkpoints")
            checkpoint = store.create_from_workspace("task", root, ["owned.txt"])
            target.write_text("worker", encoding="utf-8")
            payload = store.finalize_from_workspace(checkpoint, root)
            target.write_text("user", encoding="utf-8")
            log = Orchestrator().apply_rollback("task", payload, confirmed=True, workspace=root)
            self.assertIn("owned.txt", json.loads(log.rollback)["conflict_files"])
            self.assertEqual(target.read_text(encoding="utf-8"), "user")
            with self.assertRaises(PermissionError):
                store.create_from_workspace("task", root, ["../outside.txt"])

    def test_simple_flow_snapshots_and_rolls_back_only_after_failed_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "demo.py"; target.write_text("before", encoding="utf-8")
            orchestrator = Orchestrator(checkpoints=CheckpointStore(root / "checkpoints"))
            result, review, verification = orchestrator.run_simple(
                "task", "update demo", ["demo.py"], workspace=root,
                edits={"demo.py": "after"}, verification_commands=["python3 -m compileall -q ."])
            self.assertTrue(verification.passed)
            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertTrue((root / "checkpoints").glob("*.json"))
            result, review, verification = orchestrator.run_simple(
                "task-2", "break demo", ["demo.py"], workspace=root,
                edits={"demo.py": "broken"}, verification_commands=["python3 -m unittest no_such_test"])
            self.assertFalse(verification.passed)
            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertIn("restored_files", orchestrator.changelog[-1].rollback)


class AgentRelayConsoleCardTests(unittest.TestCase):
    def setUp(self):
        self.requirement = {
            "task_id": "task-20261102-a1b2",
            "user_requirement": "重构 Fusion Provider 的选择与锁机制",
            "requirement_assessment": "跨模块或高风险任务",
            "plain_explanation": "分析并修改相关代码，然后执行验证。",
            "affected_modules": ["Fusion", "任务调度"],
            "affected_files": [
                "scripts/fusion_mode.py",
                "scripts/fusion_runner.py",
            ],
            "current_state": "现有实现待重构，等待确认",
            "target_state": "按目标架构完成重构并通过验证",
            "risks": ["范围扩大风险", "兼容性风险"],
            "verification_plan": ["修改后通过验证", "python -m pytest"],
            "rollback_plan": ["保存修改前内容", "失败时逐文件恢复"],
        }
        self.confirmation = ConfirmationCard(
            task_id="task-20261102-a1b2",
            action="execute",
            scope=("scripts/fusion_mode.py", "scripts/fusion_runner.py"),
            acceptance_criteria=("修改后通过验证", "不修改未授权文件"),
            status="pending",
            request="重构 Fusion Provider 的选择与锁机制",
            workspace="/tmp/agentrelay-test-workspace",
            allowed_files=("scripts/fusion_mode.py", "scripts/fusion_runner.py"),
            verification_commands=("python -m pytest tests/test_fusion.py",),
        )

    def test_default_requirement_card_matches_existing_plain_format(self):
        self.assertEqual(
            render_requirement_card(self.requirement, style="default"),
            "\n".join([
                "「需求卡」",
                CODEX_DIVIDER,
                "- 任务：     重构 Fusion Provider 的选择与锁机制",
                "- 任务ID：   task-20261102-a1b2",
                "- 需求评估： 跨模块或高风险任务",
                "- 大白话：   分析并修改相关代码，然后执行验证。",
                "- 影响模块： Fusion、任务调度",
                "- 影响文件： scripts/fusion_mode.py、scripts/fusion_runner.py",
                "- 当前功能： 现有实现待重构，等待确认",
                "- 目标功能： 按目标架构完成重构并通过验证",
                "- 风险：     范围扩大风险、兼容性风险",
                "- 验证：     修改后通过验证、python -m pytest",
                "- 回滚：     保存修改前内容、失败时逐文件恢复",
                CODEX_DIVIDER,
                "1. 确认审批",
                "2. 修改需求",
                "3. 拒绝审批",
                CODEX_DIVIDER,
            ]),
        )

    def test_default_confirmation_card_matches_existing_plain_format(self):
        output = render_confirmation_card(self.confirmation, style="default")
        self.assertTrue(output.startswith("「确认卡」\n" + CODEX_DIVIDER + "\n- 任务："))
        self.assertTrue(output.endswith(CODEX_DIVIDER))
        self.assertIn("- 任务：     重构 Fusion Provider 的选择与锁机制", output)
        self.assertIn("- 任务ID：   task-20261102-a1b2", output)
        self.assertIn("- 状态：     pending", output)
        self.assertIn("- 当前确认： 执行确认的修改", output)
        self.assertIn("- 修改尝试： 0", output)
        self.assertIn("- 最近错误： 无", output)
        self.assertIn("1. 确认审批\n2. 修改需求\n3. 拒绝审批", output)
        self.assertNotIn("\033[", output)

    def test_codex_requirement_card_is_colorful_and_keeps_all_fields(self):
        output = render_requirement_card(self.requirement, style="codex")
        self.assertIn(f"{ANSI_RED_BOLD}「需求卡」{ANSI_RESET}", output)
        self.assertIn(f"{ANSI_BLUE_BOLD}- 任务：     {ANSI_RESET}", output)
        self.assertIn(f"{ANSI_GREEN}task-20261102-a1b2{ANSI_RESET}", output)
        self.assertIn(
            f"{ANSI_RED_BOLD}范围扩大风险、兼容性风险{ANSI_RESET}",
            output,
        )
        for label in (
            "任务ID",
            "需求评估",
            "大白话",
            "影响模块",
            "影响文件",
            "当前功能",
            "目标功能",
            "风险",
            "验证",
            "回滚",
        ):
            self.assertIn(label, output)

    def test_codex_confirmation_card_keeps_details_and_uses_numbered_choices(self):
        output = render_confirmation_card(self.confirmation, style="codex")
        self.assertIn(f"{ANSI_RED_BOLD}「确认卡」{ANSI_RESET}", output)
        self.assertIn(f"{ANSI_GREEN}pending{ANSI_RESET}", output)
        self.assertIn(CODEX_DIVIDER, output)
        self.assertIn(f"{ANSI_GREEN}1. 确认审批{ANSI_RESET}", output)
        self.assertIn(f"{ANSI_BLUE_BOLD}2. 修改需求{ANSI_RESET}", output)
        self.assertIn(f"{ANSI_RED_BOLD}3. 拒绝审批{ANSI_RESET}", output)
        for label in (
            "任务",
            "任务ID",
            "状态",
            "当前确认",
            "授权文件",
            "验证命令",
            "验收条件",
            "工作区",
            "修改尝试",
            "最近错误",
            "恢复原因",
        ):
            self.assertIn(label, output)

    def test_style_resolution_keeps_default_plain(self):
        self.assertEqual(resolve_card_style(None, {}), "default")
        self.assertEqual(
            resolve_card_style(
                "auto",
                {"AGENTRELAY_TARGET": "codex"},
            ),
            "codex",
        )
        self.assertEqual(
            resolve_card_style(
                "auto",
                {"AGENTRELAY_TARGET": "claude"},
            ),
            "default",
        )
        self.assertEqual(
            resolve_card_style(
                None,
                {"AGENTRELAY_CARD_STYLE": "codex"},
            ),
            "codex",
        )


class CrossPlatformRuntimeTests(unittest.TestCase):
    def test_windows_provider_lock_is_exclusive_and_released(self):
        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            commander_provider_pool, "fcntl", None
        ), mock.patch.object(commander_provider_pool, "msvcrt", fake_msvcrt):
            pool = commander_provider_pool.CommanderProviderCoordinator(directory)
            lease = pool._try_lock("deepseek-web", "task-1", "backend-code")

            self.assertIsNotNone(lease)
            self.assertEqual(fake_msvcrt.calls, [(fake_msvcrt.LK_NBLCK, 1)])

            pool.release(lease)
            self.assertEqual(
                fake_msvcrt.calls,
                [
                    (fake_msvcrt.LK_NBLCK, 1),
                    (fake_msvcrt.LK_UNLCK, 1),
                ],
            )

    def test_windows_tracker_lock_uses_msvcrt(self):
        tracker = load_module(
            "agent_relay_tracker_cross_platform",
            PROJECT_ROOT / "hooks" / "agent_relay_tracker.py",
        )
        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            tracker, "fcntl", None
        ), mock.patch.object(tracker, "msvcrt", fake_msvcrt):
            tracker.LOCK_FILE = Path(directory) / ".lock"
            tracker.ensure_dirs = lambda: Path(directory).mkdir(
                parents=True, exist_ok=True
            )
            handle = tracker.acquire_lock()
            tracker.release_lock(handle)

        self.assertEqual(
            fake_msvcrt.calls,
            [
                (fake_msvcrt.LK_NBLCK, 1),
                (fake_msvcrt.LK_UNLCK, 1),
            ],
        )

    def test_windows_tracker_pid_probe_uses_win32_branch(self):
        tracker = load_module(
            "agent_relay_tracker_pid_probe",
            PROJECT_ROOT / "hooks" / "agent_relay_tracker.py",
        )
        with mock.patch.object(tracker.os, "name", "nt"), mock.patch.object(
            tracker, "windows_pid_alive", return_value=True
        ) as windows_probe:
            self.assertTrue(tracker.pid_alive(4242))
        windows_probe.assert_called_once_with(4242)

    def test_archive_hint_uses_the_active_python_interpreter(self):
        tracker = load_module(
            "agent_relay_tracker_archive_hint",
            PROJECT_ROOT / "hooks" / "agent_relay_tracker.py",
        )
        python_path = r"C:\Program Files\AgentRelay\python.exe"
        script_path = (
            r"C:\AgentRelayTest\skills\agent-relay\scripts"
            r"\agent_relay_archive.py"
        )
        with mock.patch.object(
            tracker.sys, "executable", python_path
        ), mock.patch.object(tracker, "ARCHIVE_SCRIPT", script_path):
            text = tracker.archive_reminder_text(
                {
                    "weight": 2,
                    "last_prompt": "检查跨平台路径",
                }
            )

        self.assertIn(f'"{python_path}" "{script_path}" search', text)
        self.assertNotIn("python3", text)

    def test_windows_fusion_pid_probe_uses_win32_branch(self):
        with mock.patch.object(fusion_mode.os, "name", "nt"), mock.patch.object(
            fusion_mode, "_windows_pid_alive", return_value=True
        ) as windows_probe:
            self.assertTrue(fusion_mode._pid_alive(4242))
        windows_probe.assert_called_once_with(4242)

    def test_default_compile_command_uses_python_launcher_on_windows(self):
        with mock.patch.object(orchestrator_core.os, "name", "nt"):
            self.assertEqual(
                orchestrator_core.default_compileall_command(),
                "py -m compileall -q scripts",
            )

    def test_python_launcher_compile_and_unittest_are_allowed(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        runner = VerificationRunner()
        with mock.patch.object(
            orchestrator_core.subprocess,
            "run",
            return_value=completed,
        ) as run:
            compile_result = runner.run(
                "py -m compileall -q scripts",
                PROJECT_ROOT,
            )
            unittest_result = runner.run(
                "py -m unittest",
                PROJECT_ROOT,
            )

        self.assertEqual(compile_result.status, "passed")
        self.assertEqual(unittest_result.status, "passed")
        self.assertEqual(run.call_count, 2)

    def test_python_launcher_does_not_bypass_command_allowlist(self):
        result = VerificationRunner().run(
            "py -c \"print('unsafe')\"",
            PROJECT_ROOT,
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.error, "COMMAND_NOT_ALLOWED")

    def test_commander_terminal_accepts_absolute_file(self):
        installer = load_module(
            "agent_relay_installer_cross_platform",
            PROJECT_ROOT / "install.py",
        )
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex-custom"
            executable.write_text("", encoding="utf-8")
            ok, detail = installer.commander_terminal_status(str(executable))
        self.assertTrue(ok)
        self.assertEqual(detail, str(executable))

    def test_commander_terminal_resolves_path_command(self):
        installer = load_module(
            "agent_relay_installer_path_command",
            PROJECT_ROOT / "install.py",
        )
        with mock.patch.object(
            installer.shutil,
            "which",
            return_value=r"C:\Tools\codex.exe",
        ) as which:
            ok, detail = installer.commander_terminal_status("codex")
        self.assertTrue(ok)
        self.assertEqual(detail, r"C:\Tools\codex.exe")
        which.assert_called_once_with("codex")

    def test_windows_local_service_scan_uses_netstat_without_lsof(self):
        installer = load_module(
            "agent_relay_installer_windows_local_scan",
            PROJECT_ROOT / "install.py",
        )
        completed = SimpleNamespace(
            stdout=(
                "Proto  Local Address          Foreign Address        State\n"
                "TCP    127.0.0.1:8088         0.0.0.0:0              LISTENING\n"
                "TCP    0.0.0.0:9000           0.0.0.0:0              LISTENING\n"
                "TCP    [::1]:9090             [::]:0                 LISTENING\n"
                "TCP    192.168.1.5:7000      0.0.0.0:0              LISTENING\n"
                "TCP    127.0.0.1:50000        127.0.0.1:443          ESTABLISHED\n"
            ),
            returncode=0,
        )
        with mock.patch.object(
            installer.os, "name", "nt"
        ), mock.patch.object(
            installer.subprocess, "run", return_value=completed
        ) as run:
            ports = installer._listening_ports()

        self.assertEqual(ports, ["8088", "9000", "9090"])
        run.assert_called_once_with(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

    def test_posix_local_service_scan_keeps_lsof(self):
        installer = load_module(
            "agent_relay_installer_posix_local_scan",
            PROJECT_ROOT / "install.py",
        )
        completed = SimpleNamespace(
            stdout=(
                "python 123 user 3u IPv4 0x0 0t0 TCP "
                "127.0.0.1:8088 (LISTEN)\n"
                "python 123 user 4u IPv4 0x0 0t0 TCP "
                "*:9000 (LISTEN)\n"
            ),
            returncode=0,
        )
        with mock.patch.object(
            installer.os, "name", "posix"
        ), mock.patch.object(
            installer.subprocess, "run", return_value=completed
        ) as run:
            ports = installer._listening_ports()

        self.assertEqual(ports, ["8088", "9000"])
        run.assert_called_once_with(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

    def test_windows_local_service_scan_probes_only_loopback(self):
        installer = load_module(
            "agent_relay_installer_windows_local_probe",
            PROJECT_ROOT / "install.py",
        )
        with mock.patch.object(
            installer, "_listening_ports", return_value=["8088", "8088"]
        ), mock.patch.object(
            installer,
            "_probe_models",
            side_effect=lambda endpoint: (
                endpoint == "http://127.0.0.1:8088/v1",
                ["local-model"] if endpoint == "http://127.0.0.1:8088/v1" else [],
                "ok",
            ),
        ) as probe:
            services = installer._scan_local_services()

        self.assertEqual(
            services,
            [{
                "name": "本机兼容服务",
                "endpoint": "http://127.0.0.1:8088/v1",
                "models": ["local-model"],
                "message": "ok",
            }],
        )
        self.assertTrue(all(
            call.args[0].startswith("http://127.0.0.1:")
            for call in probe.call_args_list
        ))
        probed_endpoints = [call.args[0] for call in probe.call_args_list]
        self.assertEqual(probed_endpoints.count("http://127.0.0.1:8088/v1"), 1)

    def test_installer_guidance_uses_platform_python_launcher(self):
        installer = load_module(
            "agent_relay_installer_platform_guidance",
            PROJECT_ROOT / "install.py",
        )
        with mock.patch.object(installer.os, "name", "nt"):
            self.assertEqual(installer.installer_command(), "py install.py")
        with mock.patch.object(installer.os, "name", "posix"):
            self.assertEqual(installer.installer_command(), "python3 install.py")

    def test_windows_daemon_generation_fails_clearly(self):
        installer = load_module(
            "agent_relay_installer_daemon",
            PROJECT_ROOT / "install.py",
        )
        with mock.patch.dict(
            os.environ, {"AGENTRELAY_INSTALL_DAEMON": "1"}
        ), mock.patch.object(installer.sys, "platform", "win32"):
            with self.assertRaisesRegex(installer.InstallError, "Windows"):
                installer.write_daemon_service_config()


class CrossPlatformHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = load_module(
            "agent_relay_installer_hook_commands",
            PROJECT_ROOT / "install.py",
        )
        cls.hook = load_module(
            "agent_relay_hook_cross_platform",
            PROJECT_ROOT / "hooks" / "agent_relay_hook.py",
        )

    def test_windows_command_quotes_paths_with_spaces(self):
        command = self.installer.build_hook_command(
            Path("C:/AgentRelayTest/Test User/.codex/agentrelay-env/Scripts/python.exe"),
            Path("C:/AgentRelayTest/Test User/.codex/hooks/agent_relay_hook.py"),
            "nt",
        )
        self.assertIn('"C:\\AgentRelayTest\\Test User', command.replace("/", "\\"))
        self.assertIn("python.exe", command)
        self.assertIn("agent_relay_hook.py", command)

    def test_posix_command_quotes_paths_with_spaces(self):
        command = self.installer.build_hook_command(
            Path("/opt/agentrelay-test/test user/.codex/agentrelay-env/bin/python"),
            Path("/opt/agentrelay-test/test user/.codex/hooks/agent_relay_hook.py"),
            "posix",
        )
        self.assertIn("'/opt/agentrelay-test/test user/", command)

    def test_installer_upgrades_old_script_hook_and_preserves_user_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            hooks_json = Path(directory) / "hooks.json"
            old_command = "/bin/sh /opt/agentrelay-test/.codex/hooks/agent_relay_hook.sh"
            user_command = "/usr/local/bin/user-hook.sh"
            hooks_json.write_text(json.dumps({
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": old_command,
                                    "timeout": 3,
                                },
                                {
                                    "type": "command",
                                    "command": old_command,
                                    "timeout": 3,
                                },
                            ],
                        },
                    ],
                    "PostToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": old_command,
                                    "timeout": 3,
                                },
                                {
                                    "type": "command",
                                    "command": user_command,
                                    "timeout": 20,
                                },
                                {
                                    "type": "command",
                                    "command": old_command,
                                    "timeout": 3,
                                },
                            ],
                        },
                    ],
                },
            }), encoding="utf-8")
            replacement = shlex_command(
                "/opt/agentrelay-test/.codex/agentrelay-env/bin/python",
                "/opt/agentrelay-test/.codex/hooks/agent_relay_hook.py",
            )

            with mock.patch.object(self.installer, "HOOKS_JSON", hooks_json), \
                    mock.patch.object(
                        self.installer,
                        "hook_command",
                        return_value=replacement,
                    ), redirect_stdout(StringIO()):
                self.installer.merge_hooks_json()

            config = json.loads(hooks_json.read_text(encoding="utf-8"))
            events = config["hooks"]
            self.assertEqual(set(events), set(self.installer.TRACKED_EVENTS))

            for event in self.installer.TRACKED_EVENTS:
                agent_hooks = [
                    hook
                    for entry in events[event]
                    if isinstance(entry, dict)
                    for hook in entry.get("hooks", [])
                    if "agent_relay_hook" in str(hook.get("command", ""))
                ]
                self.assertEqual(len(agent_hooks), 1, event)
                self.assertEqual(agent_hooks[0]["type"], "command")
                self.assertEqual(agent_hooks[0]["command"], replacement)
                self.assertEqual(agent_hooks[0]["timeout"], 10)

            commands = [
                hook.get("command")
                for entry in events["PostToolUse"]
                for hook in entry.get("hooks", [])
            ]
            self.assertIn(user_command, commands)
            self.assertNotIn(old_command, commands)

    def test_hook_forwards_supported_context_outputs(self):
        post_tool = (
            '{"hookSpecificOutput":{"hookEventName":"PostToolUse",'
            '"additionalContext":"ready"}}'
        )
        user_prompt = (
            '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",'
            '"additionalContext":"confirm first"}}'
        )
        self.assertTrue(self.hook.valid_hook_output(post_tool))
        self.assertTrue(self.hook.valid_hook_output(user_prompt))
        self.assertFalse(self.hook.valid_hook_output('{"value": 1}'))
        self.assertFalse(self.hook.valid_hook_output("not-json"))
        self.assertFalse(self.hook.valid_hook_output(
            '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit"}}'
        ))
        self.assertFalse(self.hook.valid_hook_output(
            '{"systemMessage":123,"hookSpecificOutput":'
            '{"hookEventName":"UserPromptSubmit","additionalContext":"confirm first"}}'
        ))
        self.assertTrue(self.hook.valid_hook_output(
            '{"systemMessage":"colored card","hookSpecificOutput":'
            '{"hookEventName":"UserPromptSubmit","additionalContext":"confirm first"}}'
        ))

    def test_hook_forwards_valid_pre_tool_deny(self):
        value = (
            '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
            '"permissionDecision":"deny",'
            '"permissionDecisionReason":"confirm the command"}}'
        )
        self.assertTrue(
            self.hook.valid_hook_output(value, event_name="PreToolUse")
        )
        self.assertFalse(self.hook.valid_hook_output(
            '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
            '"permissionDecision":"ask",'
            '"permissionDecisionReason":"confirm"}}',
            event_name="PreToolUse",
        ))
        self.assertFalse(self.hook.valid_hook_output(
            '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
            '"permissionDecision":"deny"}}',
            event_name="PreToolUse",
        ))

    def test_hook_forwards_valid_stop_block(self):
        value = '{"decision":"block","reason":"call AgentRelay"}'
        self.assertTrue(self.hook.valid_hook_output(value, event_name="Stop"))
        self.assertTrue(self.hook.valid_hook_output(
            '{"systemMessage":"本次修改：updated hooks\\n上次修改：updated tests"}',
            event_name="Stop",
        ))
        self.assertFalse(self.hook.valid_hook_output(
            '{"systemMessage":"   "}',
            event_name="Stop",
        ))
        self.assertFalse(
            self.hook.valid_hook_output(
                '{"decision":"block"}',
                event_name="Stop",
            )
        )


class SkillInstallerSourceTests(unittest.TestCase):
    def test_auxiliary_skills_copy_their_own_skill_md(self):
        installer = load_module(
            "agent_relay_installer_skill_sources",
            PROJECT_ROOT / "install.py",
        )
        from scripts import agent_targets

        meta = {
            "label": "测试目标",
            "skill_dir": str(Path(tempfile.gettempdir()) / "agent-relay"),
            "detect": "",
            "level": "skill-only",
        }
        with mock.patch.object(
            agent_targets,
            "agent_targets",
            return_value={"test": meta},
        ), mock.patch.object(
            agent_targets,
            "install_skill_to",
        ) as install_skill, mock.patch.object(
            agent_targets,
            "verify_target",
            return_value=[],
        ):
            results, errors = installer.apply_agent_targets(["test"], [])

        self.assertEqual(results, {"test": []})
        self.assertEqual(errors, {})
        self.assertEqual(install_skill.call_count, 3)
        self.assertNotIn("skill_name", install_skill.call_args_list[0].kwargs)
        self.assertEqual(
            install_skill.call_args_list[1].kwargs["skill_name"],
            "agent-image",
        )
        self.assertEqual(
            install_skill.call_args_list[2].kwargs["skill_name"],
            "agent-fusion",
        )

    def test_failed_target_verification_is_an_install_error(self):
        installer = load_module(
            "agent_relay_installer_failed_verification",
            PROJECT_ROOT / "install.py",
        )
        from scripts import agent_targets

        meta = {
            "label": "测试目标",
            "skill_dir": str(Path(tempfile.gettempdir()) / "agent-relay"),
            "detect": "",
            "level": "full",
        }
        with mock.patch.object(
            agent_targets, "agent_targets", return_value={"test": meta},
        ), mock.patch.object(
            agent_targets, "install_skill_to",
        ), mock.patch.object(
            agent_targets, "verify_target", return_value=[
                {"name": "可见卡协议", "ok": False, "detail": "缺少 systemMessage"},
                {"name": "Hook 干跑", "ok": True, "detail": "正常"},
            ],
        ):
            results, errors = installer.apply_agent_targets(["test"], [])

        self.assertEqual(len(results["test"]), 2)
        self.assertIn("可见卡协议", errors["test"])
        self.assertIn("缺少 systemMessage", errors["test"])

    def test_main_returns_nonzero_for_failed_target_verification(self):
        installer = load_module(
            "agent_relay_installer_main_failed_verification",
            PROJECT_ROOT / "install.py",
        )
        with mock.patch.object(installer, "validate_publish_tree"), \
                mock.patch.object(installer, "check_base_environment"), \
                mock.patch.object(installer, "ensure_virtualenv"), \
                mock.patch.object(installer, "install_dependencies"), \
                mock.patch.object(installer, "install_and_verify_chromium"), \
                mock.patch.object(installer, "install_files"), \
                mock.patch.object(installer, "merge_hooks_json"), \
                mock.patch.object(installer, "_load_local_config", return_value=({}, mock.Mock())), \
                mock.patch.object(installer, "write_daemon_service_config", return_value=None), \
                mock.patch.object(installer, "print_next_steps"), \
                mock.patch.object(installer, "print_verify_results"), \
                mock.patch.object(installer, "apply_agent_targets", return_value=(
                    {"claude": [{"name": "可见卡协议", "ok": False, "detail": "缺少 systemMessage"}]},
                    {"claude": "可见卡协议：缺少 systemMessage"},
                )):
            self.assertEqual(installer.main(["--targets", "claude", "--no-menu"]), 1)

    def test_claude_target_install_registers_explicit_shim_target(self):
        installer = load_module(
            "agent_relay_installer_claude_target",
            PROJECT_ROOT / "install.py",
        )
        from scripts import agent_targets

        with tempfile.TemporaryDirectory(
            prefix="agentrelay-install-claude-"
        ) as directory:
            tmp = Path(directory)
            skill_dir = tmp / "skills" / "agent-relay"
            meta = {
                "id": "claude", "label": "Claude Code", "level": "full",
                "skill_dir": str(skill_dir), "detect": "claude",
                "config_path": str(tmp / "settings.json"),
            }
            with mock.patch.object(
                agent_targets, "agent_targets", return_value={"claude": meta},
            ), mock.patch.object(
                agent_targets, "install_skill_to",
            ), mock.patch.object(
                agent_targets, "verify_target", return_value=[],
            ), mock.patch.object(
                agent_targets, "register_claude", return_value=True,
            ) as register:
                results, errors = installer.apply_agent_targets(["claude"], [])

        self.assertEqual(results, {"claude": []})
        self.assertEqual(errors, {})
        command = register.call_args.args[1]
        self.assertIn("agentrelay_hook_shim.py", command)
        self.assertIn("--target claude", command)


if __name__ == "__main__":
    unittest.main()
