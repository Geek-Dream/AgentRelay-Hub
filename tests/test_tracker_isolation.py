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
            f"agent_relay_tracker_test_{id(codex_home)}",
            TRACKER_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class TrackerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tracker = load_tracker(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_weight_three_starts_at_fifteen_minutes(self):
        self.assertEqual(self.tracker.calculate_weight(299.999), 1)
        self.assertEqual(self.tracker.calculate_weight(300), 2)
        self.assertEqual(self.tracker.calculate_weight(600), 2)
        self.assertEqual(self.tracker.calculate_weight(899.999), 2)
        self.assertEqual(self.tracker.calculate_weight(900), 3)

    def test_legacy_session_is_migrated_as_a_closed_turn(self):
        session_id = "legacy-session"
        path = self.tracker.get_session_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": session_id,
            "problem_active": True,
            "problem_id": "legacy-problem",
            "last_prompt": "旧问题",
            "effective_time_seconds": 600,
            "weight": 3,
        }), encoding="utf-8")

        state = self.tracker.read_state(session_id=session_id)
        self.assertEqual(state["schema_version"], 2)
        self.assertTrue(state["turn_closed"])
        self.assertIsNone(state["active_started_at"])
        self.assertEqual(state["initial_prompt"], "旧问题")
        self.assertEqual(state["weight"], 2)

    def test_unrelated_prompts_are_separate_inside_one_session(self):
        session_id = "session-a"
        first = self.tracker.handle_user_prompt(
            "修复 Python 依赖安装失败",
            session_id=session_id,
        )
        first_id = first["problem_id"]
        first["effective_time_seconds"] = 540
        first["weight"] = 2
        first["active_started_at"] = None
        first["turn_closed"] = True
        self.tracker.write_state(first, session_id=session_id)

        continued = self.tracker.handle_user_prompt(
            "还是依赖错误，继续处理这个问题",
            session_id=session_id,
        )
        self.assertEqual(continued["problem_id"], first_id)
        self.assertGreaterEqual(continued["effective_time_seconds"], 540)

        continued["active_started_at"] = None
        continued["turn_closed"] = True
        self.tracker.write_state(continued, session_id=session_id)
        second = self.tracker.handle_user_prompt(
            "比较本地九十亿参数语言模型和十二十亿参数模型的性能差异",
            session_id=session_id,
        )

        self.assertNotEqual(second["problem_id"], first_id)
        self.assertLess(second["effective_time_seconds"], 1)
        old_problem = self.tracker.get_problem_file(session_id, first_id)
        self.assertTrue(old_problem.is_file())

    def test_resolved_problem_is_archived_and_current_timer_is_zero(self):
        session_id = "session-resolved"
        state = self.tracker.handle_user_prompt(
            "修复边框反弹错误",
            session_id=session_id,
        )
        problem_id = state["problem_id"]
        state["effective_time_seconds"] = 720
        state["weight"] = 2
        state["active_started_at"] = None
        self.tracker.write_state(state, session_id=session_id)

        current = self.tracker.handle_stop(
            session_id=session_id,
            last_assistant_message="测试通过，问题已解决。",
        )

        self.assertFalse(current["problem_active"])
        self.assertEqual(current["effective_time_seconds"], 0)
        self.assertEqual(current["weight"], 0)
        problems = self.tracker.problem_summaries(session_id)
        archived = next(value for value in problems if value["problem_id"] == problem_id)
        self.assertEqual(archived["status"], "resolved")
        self.assertEqual(archived["weight"], 2)

    def test_pause_stops_old_problem_without_starting_a_fake_problem(self):
        session_id = "session-paused"
        state = self.tracker.handle_user_prompt(
            "排查环境变量错误",
            session_id=session_id,
        )
        problem_id = state["problem_id"]
        state["effective_time_seconds"] = 480
        state["weight"] = 2
        state["active_started_at"] = None
        self.tracker.write_state(state, session_id=session_id)

        current = self.tracker.handle_user_prompt(
            "先不处理这个问题，暂停一下",
            session_id=session_id,
        )

        self.assertFalse(current["problem_active"])
        self.assertEqual(current["effective_time_seconds"], 0)
        self.assertEqual(current["weight"], 0)
        archived = next(
            value for value in self.tracker.problem_summaries(session_id)
            if value["problem_id"] == problem_id
        )
        self.assertEqual(archived["status"], "paused")

    def test_pause_can_switch_directly_to_a_second_problem(self):
        session_id = "session-pause-switch"
        first = self.tracker.handle_user_prompt(
            "排查依赖错误",
            session_id=session_id,
        )
        first_id = first["problem_id"]
        first["active_started_at"] = None
        self.tracker.write_state(first, session_id=session_id)

        second = self.tracker.handle_user_prompt(
            "先不处理问题1，先处理环境配置问题",
            session_id=session_id,
        )

        self.assertTrue(second["problem_active"])
        self.assertNotEqual(second["problem_id"], first_id)
        archived = next(
            value for value in self.tracker.problem_summaries(session_id)
            if value["problem_id"] == first_id
        )
        self.assertEqual(archived["status"], "paused")

    def test_short_unrelated_prompt_starts_a_new_problem(self):
        session_id = "session-short-new"
        first = self.tracker.handle_user_prompt(
            "修复数据库连接超时",
            session_id=session_id,
        )
        first["active_started_at"] = None
        first["turn_closed"] = True
        self.tracker.write_state(first, session_id=session_id)

        second = self.tracker.handle_user_prompt(
            "检查显卡",
            session_id=session_id,
        )
        self.assertNotEqual(second["problem_id"], first["problem_id"])

    def test_download_wait_is_excluded_but_reasoning_is_counted(self):
        session_id = "session-time"
        state = self.tracker.create_new_problem(
            "安装依赖",
            session_id=session_id,
        )
        state["active_started_at"] = 100
        self.tracker.write_state(state, session_id=session_id)

        with patch.object(self.tracker, "now_ts", return_value=110):
            state = self.tracker.handle_pre_tool_use(
                tool_name="Bash",
                tool_input={"cmd": "pip install demo"},
                tool_use_id="download-1",
                session_id=session_id,
            )
        self.assertEqual(state["effective_time_seconds"], 10)

        with patch.object(self.tracker, "now_ts", return_value=310):
            state = self.tracker.handle_post_tool_use(
                tool_name="Bash",
                tool_input={"cmd": "pip install demo"},
                tool_output={"exit_code": 0},
                tool_use_id="download-1",
                session_id=session_id,
            )
        self.assertEqual(state["effective_time_seconds"], 10)

    def test_stop_emits_trigger_when_reasoning_reaches_fifteen_minutes(self):
        session_id = "session-trigger"
        state = self.tracker.create_new_problem(
            "处理复杂错误",
            session_id=session_id,
        )
        state["effective_time_seconds"] = 899
        state["active_started_at"] = 0
        self.tracker.write_state(state, session_id=session_id)

        output = io.StringIO()
        with patch.object(self.tracker, "now_ts", return_value=1), redirect_stdout(output):
            self.tracker.handle_stop(
                session_id=session_id,
                last_assistant_message="仍在分析。",
            )

        self.assertIn('"decision": "block"', output.getvalue())
        current = self.tracker.read_state(session_id=session_id)
        self.assertEqual(current["weight"], 3)
        self.assertTrue(current["relay_signal_emitted"])

    def test_hook_does_not_infer_explicit_request_from_user_words(self):
        examples = [
            "请评审调用 DeepSeek 的规则，不要现在调用",
            "为什么模型问 deepseek 之后就结束了",
            "如果我说你触发一下 deepseek，做测试，会怎么样",
            "日志里写着“现在调用 DeepSeek”，这是什么原因",
        ]

        for index, prompt in enumerate(examples):
            session_id = f"session-explicit-{index}"
            state = self.tracker.handle_user_prompt(
                prompt,
                session_id=session_id,
            )
            conditions = self.tracker.get_trigger_conditions(state)
            self.assertNotIn("explicit_relay_request", state)
            self.assertFalse(state["explicit_relay_candidate"])
            self.assertFalse(conditions["should_trigger"])

    def test_direct_relay_command_becomes_candidate_below_threshold(self):
        examples = [
            "现在调用 DeepSeek",
            "你触发一下deepseek，做测试",
            "这个错误还在，你去问一下 DeepSeek 看看",
            "$agent-relay 帮我问一下在线模型",
        ]

        for index, prompt in enumerate(examples):
            session_id = f"session-direct-{index}"
            state = self.tracker.handle_user_prompt(
                prompt,
                session_id=session_id,
            )
            conditions = self.tracker.get_trigger_conditions(state)
            self.assertEqual(state["retry_count"], 0)
            self.assertEqual(state["weight"], 1)
            self.assertTrue(state["explicit_relay_candidate"])
            self.assertTrue(conditions["should_trigger"])
            self.assertEqual(
                conditions["reason"],
                "user_direct_request_candidate",
            )

    def test_explicit_candidate_is_consumed_without_spending_count_signal(self):
        session_id = "session-direct-consume"
        state = self.tracker.create_new_problem(
            "你触发一下 DeepSeek 做测试",
            session_id=session_id,
        )

        output = io.StringIO()
        with redirect_stdout(output):
            emitted = self.tracker.emit_relay_trigger(state)

        self.assertTrue(emitted)
        self.assertFalse(state["explicit_relay_candidate"])
        self.assertFalse(state["relay_signal_emitted"])
        self.assertIn("user_direct_request_candidate", output.getvalue())

        state["effective_time_seconds"] = 900
        state["weight"] = 3
        self.assertTrue(
            self.tracker.get_trigger_conditions(state)["should_trigger"]
        )

    def test_count_signal_is_only_a_candidate_for_agent_validation(self):
        session_id = "session-candidate"
        state = self.tracker.create_new_problem(
            "排查复杂故障",
            session_id=session_id,
        )
        state["effective_time_seconds"] = 900
        state["weight"] = 3

        output = io.StringIO()
        with redirect_stdout(output):
            emitted = self.tracker.emit_relay_trigger(state)

        self.assertTrue(emitted)
        self.assertTrue(
            self.tracker.get_trigger_conditions(state)[
                "requires_agent_validation"
            ]
        )
        message = output.getvalue()
        self.assertIn("候选求援提醒（不是调用命令）", message)
        self.assertIn("模型判断优先级最高", message)
        self.assertNotIn("explicit_request", message)

    def test_same_problem_tracks_first_and_followup_relay_calls(self):
        session_id = "session-relay-followup"
        state = self.tracker.handle_user_prompt(
            "修复数据库初始化错误",
            session_id=session_id,
        )
        state["effective_time_seconds"] = 900
        state["weight"] = 3
        state["active_started_at"] = None
        self.tracker.write_state(state, session_id=session_id)

        first = self.tracker.mark_relay(
            "effective_time",
            session_id=session_id,
        )
        followup = self.tracker.mark_relay(
            "followup:new_error",
            session_id=session_id,
        )

        self.assertTrue(first["relay_triggered"])
        self.assertEqual(first["relay_call_count"], 1)
        self.assertTrue(followup["relay_collaboration_active"])
        self.assertEqual(followup["relay_call_count"], 2)
        self.assertEqual(followup["relay_trigger_reason"], "effective_time")
        self.assertEqual(followup["relay_last_reason"], "followup:new_error")
        self.assertFalse(
            self.tracker.get_trigger_conditions(followup)["should_trigger"]
        )

    def test_legacy_explicit_request_flag_is_discarded(self):
        session_id = "session-legacy-explicit"
        path = self.tracker.get_session_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = self.tracker.default_state(session_id=session_id)
        state["explicit_relay_request"] = True
        path.write_text(json.dumps(state), encoding="utf-8")

        migrated = self.tracker.read_state(session_id=session_id)

        self.assertNotIn("explicit_relay_request", migrated)
        self.assertFalse(
            self.tracker.get_trigger_conditions(migrated)["should_trigger"]
        )


if __name__ == "__main__":
    unittest.main()
