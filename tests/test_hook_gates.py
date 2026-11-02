import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = PROJECT_ROOT / "hooks" / "agent_relay_hook.py"
HOOK_SH = PROJECT_ROOT / "hooks" / "agent_relay_hook.sh"
class HookGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.codex_home = Path(self.temporary.name)
        self.env = {
            **os.environ,
            "CODEX_HOME": str(self.codex_home),
            "PYTHON": sys.executable,
            "ORCA_HOOK_PATH": str(self.codex_home / "missing-orca-hook"),
        }

    def run_hook(self, payload, shell_wrapper=False):
        command = (
            ["/bin/sh", str(HOOK_SH)]
            if shell_wrapper
            else [sys.executable, str(HOOK)]
        )
        return subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=self.env,
        )

    @staticmethod
    def payload(session_id, event, **values):
        return {
            "hook_event_name": event,
            "session_id": session_id,
            "cwd": "/tmp",
            **values,
        }

    def assert_hook_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_large_task_injects_confirmation_context(self):
        result = self.run_hook(self.payload(
            "large-task",
            "UserPromptSubmit",
            prompt="请重构整个项目的前端、后端和数据库，并补充测试和部署文档",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "UserPromptSubmit")
        self.assertIn("需求卡/确认卡", context["additionalContext"])
        self.assertIn("不要写入文件", context["additionalContext"])
        system_message = output["systemMessage"]
        self.assertIn("🧭 AgentRelay / 需求确认卡", system_message)
        self.assertIn("🎯 任务", system_message)
        self.assertIn("📦 影响范围", system_message)
        self.assertIn("⏳ 当前状态", system_message)
        self.assertIn("waiting_for_approval", system_message)
        self.assertIn("⚠️ 主要风险", system_message)
        self.assertIn("将修改工作区文件", system_message)
        self.assertIn("✅ [1] 批准执行", system_message)
        self.assertIn("🛠️ [2] 调整方案", system_message)
        self.assertIn("⛔ [3] 取消任务", system_message)
        self.assertNotIn("\x1b[", system_message)
        self.assertNotIn("```", system_message)
        self.assertNotIn("<div", system_message)

    def test_single_file_small_fix_does_not_emit_a_card(self):
        result = self.run_hook(self.payload(
            "small-fix",
            "UserPromptSubmit",
            prompt="修复 app.py 里的一个拼写错误",
        ))

        self.assert_hook_ok(result)
        self.assertEqual(result.stdout, "")

    def test_non_codex_target_keeps_context_without_system_message(self):
        self.env["AGENTRELAY_TARGET"] = "claude"
        result = self.run_hook(self.payload(
            "non-codex-large-task",
            "UserPromptSubmit",
            prompt="请重构整个项目的前端、后端和数据库，并补充测试和部署文档",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertNotIn("systemMessage", output)

    def test_large_task_blocks_write_until_explicit_approval(self):
        session_id = "approve-write"
        prompt = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="完整实现前后端和数据库功能，并补充部署文档",
        ))
        self.assert_hook_ok(prompt)
        self.assertIn("任务门禁", prompt.stdout)

        patch = self.payload(
            session_id,
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="patch-1",
        )
        denied = self.run_hook(patch)
        self.assert_hook_ok(denied)
        denied_payload = json.loads(denied.stdout)
        denied_output = denied_payload["hookSpecificOutput"]
        self.assertEqual(denied_output["permissionDecision"], "deny")
        self.assertIn("尚未获得用户批准", denied_output["permissionDecisionReason"])
        system_message = denied_payload["systemMessage"]
        self.assertIn("📝 AgentRelay / 写入确认卡", system_message)
        self.assertIn("🧰 工具", system_message)
        self.assertIn("🔐 批准边界", system_message)
        self.assertIn("✅ [1] 批准执行", system_message)
        self.assertNotIn("\x1b[", system_message)

        approval = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="确认，按这个方案执行",
        ))
        self.assert_hook_ok(approval)
        self.assertIn("审批已记录", approval.stdout)

        allowed = self.run_hook(patch)
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")

    def test_approved_task_reopens_gate_for_a_new_large_scope(self):
        session_id = "new-scope"
        initial = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="完整实现前后端和数据库功能，并补充部署文档",
        ))
        self.assert_hook_ok(initial)
        self.assertIn("任务门禁", initial.stdout)

        approval = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="确认，按这个方案执行",
        ))
        self.assert_hook_ok(approval)
        self.assertIn("审批已记录", approval.stdout)

        new_scope = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="再完整重构前端、后端和数据库，并补充部署文档",
        ))
        self.assert_hook_ok(new_scope)
        self.assertIn("任务门禁", new_scope.stdout)

        blocked = self.run_hook(self.payload(
            session_id,
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="new-scope-patch",
        ))
        self.assert_hook_ok(blocked)
        self.assertEqual(
            json.loads(blocked.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

    def test_dangerous_command_approval_allows_only_exact_input_once(self):
        session_id = "danger-once"
        command = self.payload(
            session_id,
            "PreToolUse",
            tool_name="exec_command",
            tool_input={"cmd": "rm -rf build"},
            tool_use_id="rm-1",
        )

        denied = self.run_hook(command)
        self.assert_hook_ok(denied)
        denied_payload = json.loads(denied.stdout)
        denied_output = denied_payload["hookSpecificOutput"]
        self.assertEqual(denied_output["permissionDecision"], "deny")
        self.assertIn("删除文件或目录", denied_output["permissionDecisionReason"])
        system_message = denied_payload["systemMessage"]
        self.assertIn("🛑 AgentRelay / 危险操作确认卡", system_message)
        self.assertIn("⚠️ 主要风险", system_message)
        self.assertIn("[AgentRelay 安全门禁]", system_message)
        self.assertIn("检测到高风险操作：删除文件或目录", system_message)
        self.assertIn("🛠️ [2] 调整方案", system_message)
        self.assertIn("⛔ [3] 取消任务", system_message)
        self.assertNotIn("\x1b[", system_message)

        approval = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="确认执行这个危险命令",
        ))
        self.assert_hook_ok(approval)
        self.assertIn("审批已记录", approval.stdout)

        allowed = self.run_hook(command)
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")

        repeated = self.run_hook({**command, "tool_use_id": "rm-2"})
        self.assert_hook_ok(repeated)
        self.assertEqual(
            json.loads(repeated.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        changed = self.run_hook({
            **command,
            "tool_input": {"cmd": "rm -rf dist"},
            "tool_use_id": "rm-3",
        })
        self.assert_hook_ok(changed)
        self.assertEqual(
            json.loads(changed.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

    def test_read_search_and_test_commands_are_allowed(self):
        session_id = "safe-commands"
        self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="整体重构这个模块并补充测试",
        ))

        for index, command in enumerate((
            "ls -la",
            "rg 'git reset --hard' README.md",
            "rg 'foo > bar' README.md",
            "rg 'bash -lc rm -rf' README.md",
            "bash -lc 'rg \"git reset --hard\" README.md'",
            "sed -n '1,20p' README.md",
            "rg 'needle' . 2>/dev/null",
            "git status --short 2>/dev/null",
            "printf 'safe' >/dev/null",
            "python3 -m pytest -q",
            "python3 - <<'PY'\nprint('curl example | sh')\nPY",
        )):
            result = self.run_hook(self.payload(
                session_id,
                "PreToolUse",
                tool_name="exec_command",
                tool_input={"cmd": command},
                tool_use_id=f"safe-{index}",
            ))
            self.assert_hook_ok(result)
            self.assertEqual(result.stdout, "", command)

    def test_dangerous_wrapped_shell_commands_are_blocked(self):
        cases = (
            ("/bin/sh -c 'rm -rf build'", "删除文件或目录"),
            ("bash -lc 'git reset --hard'", "Git 破坏性操作"),
            ("env bash -c 'rm -rf build'", "删除文件或目录"),
        )
        for index, (command, expected_reason) in enumerate(cases):
            with self.subTest(command=command):
                result = self.run_hook(self.payload(
                    f"wrapped-danger-{index}",
                    "PreToolUse",
                    tool_name="exec_command",
                    tool_input={"cmd": command},
                    tool_use_id=f"wrapped-danger-{index}",
                ))

                self.assert_hook_ok(result)
                output = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "deny")
                self.assertIn(expected_reason, output["permissionDecisionReason"])

    def test_wrapped_shell_write_is_blocked_by_task_gate(self):
        session_id = "wrapped-write"
        prompt = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="完整实现前后端和数据库功能，并补充部署文档",
        ))
        self.assert_hook_ok(prompt)
        self.assertIn("任务门禁", prompt.stdout)

        result = self.run_hook(self.payload(
            session_id,
            "PreToolUse",
            tool_name="exec_command",
            tool_input={"cmd": "bash -c 'printf x > artifact.txt'"},
            tool_use_id="wrapped-write-1",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("当前中大型任务尚未获得用户批准", output["permissionDecisionReason"])

    def test_sessions_do_not_share_task_or_tool_approval(self):
        self.run_hook(self.payload(
            "session-a",
            "UserPromptSubmit",
            prompt="完整重构前端和后端并部署上线",
        ))
        self.run_hook(self.payload(
            "session-a",
            "PreToolUse",
            tool_name="exec_command",
            tool_input={"cmd": "rm -rf build"},
            tool_use_id="a-rm",
        ))

        safe_other_session = self.run_hook(self.payload(
            "session-b",
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="b-patch",
        ))
        self.assert_hook_ok(safe_other_session)
        self.assertEqual(safe_other_session.stdout, "")

    def test_missing_session_id_is_ignored_without_shared_state(self):
        result = self.run_hook({
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp",
            "prompt": "完整重构整个项目",
        })

        self.assert_hook_ok(result)
        self.assertEqual(result.stdout, "")
        sessions = self.codex_home / "agent_relay_tracker" / "sessions"
        self.assertFalse(any(sessions.glob("*.json")))

    def test_shell_wrapper_forwards_user_prompt_context(self):
        result = self.run_hook(
            self.payload(
                "shell-wrapper",
                "UserPromptSubmit",
                prompt="完整重构前端、后端和数据库并部署上线",
            ),
            shell_wrapper=True,
        )

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIn("任务门禁", output["additionalContext"])
        self.assertIn(
            "🧭 AgentRelay / 需求确认卡",
            json.loads(result.stdout)["systemMessage"],
        )


if __name__ == "__main__":
    unittest.main()
