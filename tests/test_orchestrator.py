import tempfile
import unittest
from pathlib import Path

from hooks.agent_relay_tracker import calculate_weight, get_trigger_conditions
from scripts.orchestrator_runtime import ModelRegistry, route_task
from scripts.orchestrator_store import CheckpointStore, KnowledgeRecord, MemoryStore
from scripts.orchestrator_dispatcher import Dispatcher
from scripts.orchestrator_store import DispatchRequest


class OrchestratorTests(unittest.TestCase):
    def test_weight_and_time_trigger(self):
        self.assertEqual(calculate_weight(299), 1)
        self.assertEqual(calculate_weight(300), 2)
        self.assertEqual(calculate_weight(600), 3)
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


if __name__ == "__main__":
    unittest.main()
