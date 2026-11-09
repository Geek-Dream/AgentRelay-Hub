import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


TRACKER_PATH = (
    Path(__file__).resolve().parents[1]
    / "hooks"
    / "agent_relay_tracker.py"
)


def load_tracker(codex_home):
    with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
        spec = importlib.util.spec_from_file_location(
            f"agent_relay_change_history_test_{id(codex_home)}",
            TRACKER_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class ChangeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.tracker = load_tracker(self.home)

    def run_write_turn(self, session_id, prompt, relative_path):
        state = self.tracker.create_new_problem(
            prompt,
            session_id=session_id,
            workspace=str(self.workspace),
        )
        self.tracker.write_state(state, session_id=session_id)
        state = self.tracker.handle_post_tool_use(
            tool_name="Edit",
            tool_input={"file_path": str(self.workspace / relative_path)},
            tool_output={"exit_code": 0},
            tool_use_id=f"write-{session_id}",
            session_id=session_id,
            workspace=str(self.workspace),
        )
        output = io.StringIO()
        with patch.dict(os.environ, {"AGENTRELAY_TARGET": "codex"}), \
                redirect_stdout(output):
            self.tracker.handle_stop(
                session_id=session_id,
                last_assistant_message="已完成修改并通过定向验证。",
            )
        return state, output.getvalue()

    def assert_change_summary_format(self, message):
        lines = message.splitlines()
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[1], "  📝 本次修改：")
        separator = lines.index("", 2)
        self.assertEqual(lines[separator + 1], "  🕘 上次修改：")
        self.assertTrue(lines[2:separator])
        self.assertTrue(lines[separator + 2:])
        self.assertTrue(
            all(line.startswith("    ") for line in lines[2:separator])
        )
        self.assertTrue(
            all(
                line.startswith("    ")
                for line in lines[separator + 2:]
            )
        )
        self.assertIn("\n\n  🕘 上次修改：", message)
        self.assertNotIn(self.tracker.HOOK_CARD_DIVIDER, message)
        self.assertEqual(message.count("本次修改："), 1)
        self.assertEqual(message.count("上次修改："), 1)
        self.assertNotIn("上上次修改", message)

    def test_long_change_summary_wraps_with_indented_continuation_lines(self):
        message = self.tracker.render_change_history([
            {
                "summary": (
                    "按需求“验证摘要换行”：已完成较长摘要的自动换行处理，"
                    "包含 hooks/agent_relay_tracker.py、README.md、"
                    "tests/test_change_history.py 以及跨宿主转发验证。"
                )
            },
            {"summary": "按需求“补充说明”：已更新 README.md"},
        ])
        lines = message.splitlines()
        separator = lines.index("", 2)
        current_content = lines[2:separator]
        self.assertGreater(len(current_content), 1)
        self.assertTrue(
            all(
                self.tracker._card_label_width(line)
                <= self.tracker.CHANGE_HISTORY_TEXT_WIDTH + 4
                for line in current_content
            )
        )

    def test_five_write_turns_show_and_keep_only_latest_two(self):
        first_message = ""
        for index in range(1, 5):
            _, output = self.run_write_turn(
                "history-limit",
                f"完成第{index}次修改",
                f"file-{index}.py",
            )
            payload = json.loads(output)
            self.assert_change_summary_format(payload["systemMessage"])
            if index == 1:
                first_message = payload["systemMessage"]

        self.assertIn("🕘 上次修改：\n    暂无记录", first_message)

        history = self.tracker.read_change_history(
            str(self.workspace),
            "default",
        )
        self.assertEqual(len(history["entries"]), 2)
        self.assertIn("第4次修改", history["entries"][0]["summary"])
        self.assertIn("第3次修改", history["entries"][1]["summary"])
        self.assertNotIn(
            "第2次修改",
            "\n".join(item["summary"] for item in history["entries"]),
        )

        _, output = self.run_write_turn(
            "history-limit",
            "完成第5次修改",
            "file-5.py",
        )
        message = json.loads(output)["systemMessage"]
        self.assert_change_summary_format(message)
        self.assertIn("第5次修改", message)
        self.assertIn("第4次修改", message)
        self.assertNotIn("第3次修改", message)
        self.assertNotIn("第1次修改", message)
        self.assertNotIn("第2次修改", message)

    def test_question_and_read_only_turn_do_not_create_history(self):
        state = self.tracker.create_new_problem(
            "解释一下 Hook 是什么",
            session_id="question-only",
            workspace=str(self.workspace),
        )
        self.tracker.write_state(state, session_id="question-only")
        output = io.StringIO()
        with patch.dict(os.environ, {"AGENTRELAY_TARGET": "codex"}), \
                redirect_stdout(output):
            self.tracker.handle_stop(
                session_id="question-only",
                last_assistant_message="这只是概念解释，没有修改文件。",
            )

        self.assertEqual(output.getvalue(), "")
        history = self.tracker.read_change_history(
            str(self.workspace),
            "default",
        )
        self.assertEqual(history["entries"], [])

    def test_same_turn_is_recorded_once(self):
        state = self.tracker.default_state(session_id="dedupe")
        state["agent_id"] = "default"
        state["change_tracking"] = self.tracker.new_change_tracking(
            "修复 Hook 记录",
            session_id="dedupe",
            workspace=str(self.workspace),
        )
        state["change_tracking"]["files"] = ["hooks/example.py"]
        state["change_tracking"]["had_write"] = True

        first = self.tracker.record_change_history(
            state,
            "已完成修改。",
        )
        second = self.tracker.record_change_history(
            state,
            "已完成修改。",
        )

        self.assertIn("本次修改：", first)
        self.assertEqual(second, "")
        history = self.tracker.read_change_history(
            str(self.workspace),
            "default",
        )
        self.assertEqual(len(history["entries"]), 1)

    def test_workspace_histories_are_isolated(self):
        second_workspace = self.home / "second-workspace"
        second_workspace.mkdir()
        for workspace, summary in (
            (self.workspace, "第一个工作区"),
            (second_workspace, "第二个工作区"),
        ):
            state = self.tracker.default_state(session_id=summary)
            state["agent_id"] = "default"
            state["change_tracking"] = self.tracker.new_change_tracking(
                summary,
                session_id=summary,
                workspace=str(workspace),
            )
            state["change_tracking"]["files"] = ["README.md"]
            state["change_tracking"]["had_write"] = True
            self.tracker.record_change_history(state, summary)

        first = self.tracker.read_change_history(
            str(self.workspace),
            "default",
        )
        second = self.tracker.read_change_history(
            str(second_workspace),
            "default",
        )
        self.assertIn("第一个工作区", first["entries"][0]["summary"])
        self.assertNotIn("第二个工作区", first["entries"][0]["summary"])
        self.assertIn("第二个工作区", second["entries"][0]["summary"])

    def test_changed_paths_are_relative_and_deduplicated(self):
        absolute = str(self.workspace / "src" / "app.py")
        patch_text = (
            "*** Update File: src/app.py\n"
            "*** Move to: src/main.py\n"
        )
        targets = self.tracker.written_file_targets(
            "apply_patch",
            {
                "file_path": absolute,
                "path": "src/app.py",
                "patch": patch_text,
            },
            str(self.workspace),
        )

        self.assertEqual(targets, ["src/app.py", "src/main.py"])

    def test_failed_write_is_not_recorded(self):
        state = self.tracker.create_new_problem(
            "修复失败写入",
            session_id="failed-write",
            workspace=str(self.workspace),
        )
        self.tracker.write_state(state, session_id="failed-write")
        state = self.tracker.handle_post_tool_use(
            tool_name="Edit",
            tool_input={"file_path": str(self.workspace / "broken.py")},
            tool_output={"exit_code": 1},
            tool_use_id="failed-write",
            session_id="failed-write",
            workspace=str(self.workspace),
        )

        self.assertFalse(state["change_tracking"]["had_write"])
        self.assertEqual(state["change_tracking"]["files"], [])

    def test_post_tool_use_reuses_pre_tool_input_for_change_summary(self):
        state = self.tracker.create_new_problem(
            "修复 Hook 修改摘要",
            session_id="pending-input",
            workspace=str(self.workspace),
        )
        self.tracker.write_state(state, session_id="pending-input")
        self.tracker.handle_pre_tool_use(
            tool_name="apply_patch",
            tool_input={
                "patch": "*** Update File: hooks/agent_relay_tracker.py\n",
            },
            tool_use_id="pending-input-write",
            session_id="pending-input",
            workspace=str(self.workspace),
        )
        state = self.tracker.handle_post_tool_use(
            tool_name="apply_patch",
            tool_input={},
            tool_output={"exit_code": 0},
            tool_use_id="pending-input-write",
            session_id="pending-input",
            workspace=str(self.workspace),
        )

        self.assertEqual(
            state["change_tracking"]["files"],
            ["hooks/agent_relay_tracker.py"],
        )

    def test_change_summary_does_not_echo_acknowledgement_prefix(self):
        state = self.tracker.default_state(session_id="ack-prefix")
        state["agent_id"] = "default"
        state["change_tracking"] = self.tracker.new_change_tracking(
            "调整修改摘要",
            session_id="ack-prefix",
            workspace=str(self.workspace),
        )
        state["change_tracking"]["tools"] = ["apply_patch"]
        state["change_tracking"]["had_write"] = True

        message = self.tracker.record_change_history(
            state,
            "是的，修改摘要已完成，并补充了换行显示。",
        )

        self.assertIn("修改摘要已完成", message)
        self.assertNotIn("是的", message)
        self.assert_change_summary_format(message)
        self.assertIn("🕘 上次修改：\n    暂无记录", message)

    def test_generic_continue_keeps_the_original_change_request(self):
        state = self.tracker.create_new_problem(
            "修复 Hook 修改历史",
            session_id="continue-request",
            workspace=str(self.workspace),
        )
        self.tracker.write_state(state, session_id="continue-request")
        state = self.tracker.handle_post_tool_use(
            tool_name="Edit",
            tool_input={"file_path": str(self.workspace / "hooks" / "x.py")},
            tool_output={"exit_code": 0},
            tool_use_id="continue-request",
            session_id="continue-request",
            workspace=str(self.workspace),
        )
        with patch.dict(os.environ, {"AGENTRELAY_TARGET": "codex"}), \
                redirect_stdout(io.StringIO()):
            self.tracker.handle_stop(
                session_id="continue-request",
                last_assistant_message="已修改 Hook，但还要继续检查。",
            )

        continued = self.tracker.handle_user_prompt(
            "继续",
            session_id="continue-request",
            workspace=str(self.workspace),
        )

        self.assertEqual(
            continued["change_tracking"]["request"],
            "修复 Hook 修改历史",
        )


if __name__ == "__main__":
    unittest.main()
