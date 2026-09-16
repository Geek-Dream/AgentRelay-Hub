import tempfile
import unittest
import json
import subprocess
import os
from unittest import mock
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
                                        SelfWorker, SelfReviewer, VerificationRunner, ConfirmationStore, FallbackExecutor)
from scripts.orchestrator_core import WorkflowEngine, TaskExecutionState, TaskStateMachine, TaskContext


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


if __name__ == "__main__":
    unittest.main()
