import importlib.util
import io
import json
import os
import subprocess
import sys
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

    def run_write_turn(self, session_id, prompt, relative_path, assistant_message=None):
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
                last_assistant_message=(
                    assistant_message
                    or "已完成修改并通过定向验证。"
                ),
            )
        return state, output.getvalue()

    def run_tracker_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(TRACKER_PATH), *args],
            cwd=self.workspace,
            env={**os.environ, "CODEX_HOME": str(self.home)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

    def assert_change_summary_format(self, message):
        lines = message.splitlines()
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[1], "  📝 本次修改：")
        previous_index = lines.index("  🕘 上次修改：")
        self.assertGreater(previous_index, 2)
        self.assertTrue(lines[2:previous_index])
        self.assertTrue(lines[previous_index + 1:])
        self.assertTrue(
            all(
                not line or line.startswith("    ")
                for line in lines[2:previous_index]
            )
        )
        self.assertTrue(
            all(
                not line or line.startswith("    ")
                for line in lines[previous_index + 1:]
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
        previous_index = lines.index("  🕘 上次修改：")
        current_content = [
            line for line in lines[2:previous_index]
            if line
        ]
        self.assertGreater(len(current_content), 1)
        self.assertTrue(
            all(
                self.tracker._card_label_width(line)
                <= self.tracker.CHANGE_HISTORY_TEXT_WIDTH + 4
                for line in current_content
            )
        )

    def test_actual_assistant_change_replaces_user_request_as_summary(self):
        state = self.tracker.default_state(session_id="actual-change")
        state["agent_id"] = "default"
        state["change_tracking"] = self.tracker.new_change_tracking(
            "我查看了品牌热销地区图，都是未覆盖，没有接入数据",
            session_id="actual-change",
            workspace=str(self.workspace),
        )
        state["change_tracking"]["files"] = [
            "src/views/screen/datacenter/distData.js",
            "src/views/screen/datacenter/DistMap.vue",
        ]
        state["change_tracking"]["had_write"] = True

        message = self.tracker.record_change_history(
            state,
            "已接入品牌热销地区数据，并更新地图覆盖状态。",
        )

        self.assertIn("已接入品牌热销地区数据", message)
        self.assertNotIn("我查看了品牌热销地区图", message)
        self.assertIn("涉及到的文件：", message)
        self.assertIn("源代码", message)

    def test_assistant_hint_keeps_filename_instead_of_splitting_at_dot(self):
        hint = self.tracker._assistant_change_hint(
            "已修复 app.py 的拼写问题，并完成定向验证。"
        )

        self.assertEqual(hint, "已修复 app.py 的拼写问题，并完成定向验证")

    def test_missing_actual_change_uses_honest_file_fallback(self):
        state = self.tracker.default_state(session_id="fallback-change")
        state["agent_id"] = "default"
        state["change_tracking"] = self.tracker.new_change_tracking(
            "刷新品牌分布图即可看到效果",
            session_id="fallback-change",
            workspace=str(self.workspace),
        )
        state["change_tracking"]["files"] = [
            "tests/datacenter/distBrandCoverage.test.js",
            "src/views/screen/datacenter/DistMap.vue",
        ]
        state["change_tracking"]["had_write"] = True

        message = self.tracker.record_change_history(
            state,
            "刷新品牌分布图即可看到效果。",
        )

        self.assertIn("未记录具体修改说明", message)
        self.assertNotIn("按需求“刷新品牌分布图", message)
        self.assertIn("· 测试：", message)
        self.assertIn("· 源代码：", message)

    def test_explicit_groups_are_rendered_and_persisted(self):
        state = self.tracker.default_state(session_id="grouped-change")
        state["agent_id"] = "default"
        state["change_tracking"] = self.tracker.new_change_tracking(
            "优化页面并调整颜色",
            session_id="grouped-change",
            workspace=str(self.workspace),
        )
        state["change_tracking"].update({
            "files": [
                "test.vue",
                "testx01.vue",
                "src/disBran.vue",
                "src/view/DisMap.vue",
                "test/dddd/web.vue",
            ],
            "summary": (
                "优化了综合查询-整体情况页面，更新了数据中心按钮颜色，"
                "并调整同喜分析输入框颜色。"
            ),
            "file_groups": [
                {
                    "label": "页面综合查询-页面优化",
                    "files": ["test.vue", "testx01.vue", "src/disBran.vue"],
                },
                {
                    "label": "综合查询-数据中心-按钮颜色修改",
                    "files": ["src/view/DisMap.vue"],
                },
                {
                    "label": "同喜分析-输入框为绿色",
                    "files": ["test/dddd/web.vue"],
                },
            ],
            "had_write": True,
        })

        message = self.tracker.record_change_history(state, "")

        self.assertIn("优化了综合查询-整体情况页面", message)
        self.assertIn("· 页面综合查询-页面优化：", message)
        self.assertIn("· 综合查询-数据中心-按钮颜色修改：", message)
        self.assertIn("· 同喜分析-输入框为绿色：", message)
        self.assertIn("        src/view/DisMap.vue", message)
        history = self.tracker.read_change_history(
            str(self.workspace),
            "default",
        )
        self.assertEqual(len(history["entries"][0]["file_groups"]), 3)

    def test_annotate_change_summary_rejects_unknown_group_file(self):
        state = self.tracker.create_new_problem(
            "修复摘要",
            session_id="annotate-change",
            workspace=str(self.workspace),
        )
        self.tracker.write_state(state, session_id="annotate-change")
        self.tracker.handle_post_tool_use(
            tool_name="Edit",
            tool_input={"file_path": str(self.workspace / "src" / "app.py")},
            tool_output={"exit_code": 0},
            tool_use_id="annotate-change-write",
            session_id="annotate-change",
            workspace=str(self.workspace),
        )

        with self.assertRaisesRegex(ValueError, "未修改的文件"):
            self.tracker.annotate_change_summary(
                "annotate-change",
                "修复了应用逻辑。",
                ["应用逻辑=src/not-changed.py"],
            )

        result = self.tracker.annotate_change_summary(
            "annotate-change",
            "修复了应用逻辑。",
            ["应用逻辑=src/app.py"],
        )

        self.assertEqual(result["summary"], "修复了应用逻辑。")
        self.assertEqual(
            result["file_groups"],
            [{"label": "应用逻辑", "files": ["src/app.py"]}],
        )

    def test_change_summary_cli_persists_agent_groups_for_stop_output(self):
        session_id = "cli-summary"
        state = self.tracker.create_new_problem(
            "优化页面并补充测试",
            session_id=session_id,
            workspace=str(self.workspace),
        )
        self.tracker.write_state(state, session_id=session_id)
        for index, relative_path in enumerate(
            ("src/app.py", "tests/test_app.py"),
            start=1,
        ):
            self.tracker.handle_post_tool_use(
                tool_name="Edit",
                tool_input={
                    "file_path": str(self.workspace / relative_path),
                },
                tool_output={"exit_code": 0},
                tool_use_id=f"cli-summary-write-{index}",
                session_id=session_id,
                workspace=str(self.workspace),
            )

        completed = self.run_tracker_cli(
            "change-summary",
            "--session",
            session_id,
            "--summary",
            "优化了应用页面，并补充页面行为回归测试。",
            "--file-group",
            "应用页面优化=src/app.py",
            "--file-group",
            "回归测试=tests/test_app.py",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        message = self.tracker.record_change_history(
            self.tracker.read_state(session_id=session_id),
            "",
        )
        self.assertIn("优化了应用页面", message)
        self.assertIn("· 应用页面优化：", message)
        self.assertIn("· 回归测试：", message)
        self.assertIn("        tests/test_app.py", message)

    def test_five_write_turns_show_and_keep_only_latest_two(self):
        first_message = ""
        for index in range(1, 5):
            _, output = self.run_write_turn(
                "history-limit",
                f"完成第{index}次修改",
                f"file-{index}.py",
                f"已修复第{index}次修改问题并通过定向验证。",
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
            "已修复第5次修改问题并通过定向验证。",
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
        for workspace, label in (
            (self.workspace, "第一个工作区"),
            (second_workspace, "第二个工作区"),
        ):
            state = self.tracker.default_state(session_id=label)
            state["agent_id"] = "default"
            state["change_tracking"] = self.tracker.new_change_tracking(
                f"更新{label}的摘要",
                session_id=label,
                workspace=str(workspace),
            )
            state["change_tracking"]["files"] = ["README.md"]
            state["change_tracking"]["had_write"] = True
            self.tracker.record_change_history(
                state,
                f"已更新{label}的修改摘要。",
            )

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
