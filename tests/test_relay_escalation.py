import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


TRACKER_PATH = Path(__file__).resolve().parents[1] / "hooks" / "agent_relay_tracker.py"


def load_tracker(codex_home):
    with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
        spec = importlib.util.spec_from_file_location(
            f"agent_relay_tracker_escalation_{id(codex_home)}",
            TRACKER_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class RelayEscalationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tracker = load_tracker(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def make_state(self, **overrides):
        state = self.tracker.default_state(
            session_id="escalation-test",
            agent_id="default",
        )
        state.update({
            "problem_active": True,
            "problem_id": "problem-1",
            "initial_prompt": "问题A",
            "last_prompt": "问题A",
            "retry_count": 3,
            "failed_tool_call_count": 3,
            "effective_time_seconds": 300.0,
            "relay_signal_count": 0,
        })
        state.update(overrides)
        return state

    def emit(self, state, hook_event_name="PostToolUse", now=1000):
        output = io.StringIO()
        with patch.object(self.tracker, "now_ts", return_value=now), redirect_stdout(output):
            emitted = self.tracker.emit_relay_trigger(
                state,
                hook_event_name=hook_event_name,
            )
        return emitted, output.getvalue()

    def test_first_signal_emitted_at_threshold(self):
        state = self.make_state()
        emitted, raw = self.emit(state, now=1000)
        self.assertTrue(emitted)
        self.assertEqual(state["relay_signal_count"], 1)
        self.assertEqual(state["relay_last_signal_at"], 1000)
        self.assertEqual(state["relay_signal_retry_count"], 3)
        payload = json.loads(raw)
        self.assertIn("候选求援提醒", payload["hookSpecificOutput"]["additionalContext"])

    def test_second_signal_waits_ten_minutes(self):
        # 只过了 599 秒：不升级
        state = self.make_state(
            relay_signal_count=1,
            relay_last_signal_at=1000 - 599,
            relay_signal_retry_count=3,
        )
        with patch.object(self.tracker, "now_ts", return_value=1000):
            conditions = self.tracker.get_trigger_conditions(state)
        self.assertFalse(conditions["should_trigger"])

        # 满 600 秒且 retry 仍在涨：发出第 2 次
        state["retry_count"] = 5
        state["relay_last_signal_at"] = 1000 - 600
        with patch.object(self.tracker, "now_ts", return_value=1000):
            conditions = self.tracker.get_trigger_conditions(state)
        self.assertTrue(conditions["should_trigger"])
        self.assertTrue(conditions["escalation_due"])

        emitted, raw = self.emit(state, now=1000)
        self.assertTrue(emitted)
        self.assertEqual(state["relay_signal_count"], 2)
        self.assertIn("第 2 次求援提醒", raw)

    def test_escalation_skipped_when_retry_regressed(self):
        # retry 从提醒时的 5 回落到 2，说明模型已经换方向取得进展，不打扰
        state = self.make_state(
            retry_count=2,
            relay_signal_count=1,
            relay_last_signal_at=1000 - 700,
            relay_signal_retry_count=5,
        )
        with patch.object(self.tracker, "now_ts", return_value=1000):
            conditions = self.tracker.get_trigger_conditions(state)
        self.assertFalse(conditions["should_trigger"])
        self.assertFalse(conditions["escalation_due"])

    def test_third_signal_after_five_minutes(self):
        state = self.make_state(
            retry_count=8,
            relay_signal_count=2,
            relay_last_signal_at=1000 - 300,
            relay_signal_retry_count=5,
        )
        emitted, raw = self.emit(state, now=1000)
        self.assertTrue(emitted)
        self.assertEqual(state["relay_signal_count"], 3)
        self.assertIn("第 3 次求援提醒", raw)
        self.assertIn("强制执行", raw)

    def test_force_after_third_signal_plus_five_minutes(self):
        state = self.make_state(
            retry_count=12,
            relay_signal_count=3,
            relay_last_signal_at=1000 - 300,
            relay_signal_retry_count=8,
        )

        script = (
            self.root
            / "skills" / "agent-relay" / "scripts" / "agent_relay.py"
        )
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# fake relay script\n", encoding="utf-8")
        python = self.root / "agentrelay-env" / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("# fake python\n", encoding="utf-8")

        process = unittest.mock.Mock()
        process.pid = 4242
        with patch.object(self.tracker.subprocess, "Popen", return_value=process) as popen:
            emitted, raw = self.emit(state, now=1000)

        self.assertTrue(emitted)
        self.assertTrue(popen.called)
        self.assertEqual(state["relay_force_state"], "launched")
        self.assertIn("强制执行已启动", raw)
        out_file, pid_file = self.tracker.forced_relay_artifacts("problem-1")
        self.assertEqual(pid_file.read_text(encoding="utf-8"), "4242")

    def test_force_launches_only_once(self):
        state = self.make_state(
            retry_count=12,
            relay_signal_count=3,
            relay_last_signal_at=1000 - 900,
            relay_signal_retry_count=8,
            relay_force_state="launched",
        )
        with patch.object(self.tracker.subprocess, "Popen") as popen:
            with patch.object(self.tracker, "now_ts", return_value=1000):
                conditions = self.tracker.get_trigger_conditions(state)
            self.assertFalse(conditions["force_due"])
            emitted, _ = self.emit(state, now=1000)
        self.assertFalse(popen.called)
        self.assertFalse(emitted)

    def test_delivery_injects_result_and_stops_escalation(self):
        state = self.make_state(
            retry_count=12,
            relay_signal_count=3,
            relay_last_signal_at=1000 - 900,
            relay_signal_retry_count=8,
            relay_force_state="launched",
        )
        out_file, pid_file = self.tracker.forced_relay_artifacts("problem-1")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("建议：改用 applypatch 写入文件。\n", encoding="utf-8")
        pid_file.write_text("4242", encoding="utf-8")

        with patch.object(self.tracker, "pid_alive", return_value=False):
            emitted, raw = self.emit(state, now=1000)

        self.assertTrue(emitted)
        self.assertIn("强制执行结果", raw)
        self.assertIn("改用 applypatch", raw)
        self.assertEqual(state["relay_force_state"], "delivered")
        self.assertTrue(state["relay_triggered"])
        self.assertEqual(state["relay_last_reason"], "forced")

        # 只投递一次：再次调用不再输出
        with patch.object(self.tracker, "pid_alive", return_value=False):
            emitted_again, _ = self.emit(state, now=1001)
        self.assertFalse(emitted_again)

    def test_no_escalation_after_relay_triggered(self):
        state = self.make_state(
            retry_count=12,
            relay_triggered=True,
            relay_signal_count=1,
            relay_last_signal_at=1000 - 900,
            relay_signal_retry_count=3,
        )
        with patch.object(self.tracker, "now_ts", return_value=1000):
            conditions = self.tracker.get_trigger_conditions(state)
        self.assertFalse(conditions["should_trigger"])
        self.assertFalse(conditions["force_due"])

    def test_no_escalation_when_problem_inactive(self):
        state = self.make_state(
            problem_active=False,
            relay_signal_count=2,
            relay_last_signal_at=1000 - 900,
            relay_signal_retry_count=3,
        )
        with patch.object(self.tracker, "now_ts", return_value=1000):
            conditions = self.tracker.get_trigger_conditions(state)
        self.assertFalse(conditions["should_trigger"])
        self.assertFalse(conditions["force_due"])

    def test_stop_event_blocks_with_launch_notice(self):
        state = self.make_state(
            retry_count=12,
            relay_signal_count=3,
            relay_last_signal_at=1000 - 300,
            relay_signal_retry_count=8,
        )
        script = (
            self.root
            / "skills" / "agent-relay" / "scripts" / "agent_relay.py"
        )
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# fake relay script\n", encoding="utf-8")

        process = unittest.mock.Mock()
        process.pid = 4343
        with patch.object(self.tracker.subprocess, "Popen", return_value=process):
            emitted, raw = self.emit(state, hook_event_name="Stop", now=1000)

        self.assertTrue(emitted)
        payload = json.loads(raw)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("强制执行已启动", payload["reason"])


if __name__ == "__main__":
    unittest.main()
