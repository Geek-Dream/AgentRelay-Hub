import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = PROJECT_ROOT / "hooks" / "agent_relay_hook.py"
HOOK_SH = PROJECT_ROOT / "hooks" / "agent_relay_hook.sh"
TRACKER = PROJECT_ROOT / "hooks" / "agent_relay_tracker.py"


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

    def assert_change_summary_format(self, message):
        lines = message.splitlines()
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[1], "  📝 本次修改：")
        separator = lines.index("", 2)
        self.assertEqual(lines[separator + 1], "  🕘 上次修改：")
        self.assertTrue(
            all(line.startswith("    ") for line in lines[2:separator])
        )
        self.assertTrue(
            all(
                line.startswith("    ")
                for line in lines[separator + 2:]
            )
        )
        self.assertTrue(message.startswith("\n  📝 本次修改："))
        self.assertNotIn("--------------------------------", message)
        self.assertNotIn("上上次修改", message)

    def run_tracker(self, *args):
        return subprocess.run(
            [sys.executable, str(TRACKER), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=self.env,
        )

    def semantic_source_hash(self, result):
        self.assert_hook_ok(result)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[AgentRelay 语义审批桥]", context)
        match = re.search(r"--source-sha256 ([0-9a-f]{64})", context)
        self.assertIsNotNone(match, context)
        return match.group(1)

    def resolve_intent(self, session_id, source_hash, decision="approve",
                       work_profile=None):
        if decision == "approve" and work_profile is None:
            work_profile = self.minimum_work_profile(session_id)
        args = [
            "resolve-intent",
            "--session", session_id,
            "--decision", decision,
            "--source-sha256", source_hash,
            "--reason", "测试主 Agent 已结合上下文判断用户意图",
        ]
        if decision == "approve" and work_profile:
            args.extend(["--work-profile", work_profile])
        return self.run_tracker(*args)

    def minimum_work_profile(self, session_id):
        state = json.loads(
            (
                self.codex_home / "agent_relay_tracker" / "sessions"
                / f"{session_id}.json"
            ).read_text(encoding="utf-8")
        )
        return (
            ((state.get("task_gate") or {}).get("work_profile") or {})
            .get("minimum")
            or "light"
        )

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
        self.assertIn("不得写入", context["additionalContext"])
        system_message = output["systemMessage"]
        self.assertIn("「需求卡」", system_message)
        self.assertIn("任务", system_message)
        self.assertIn("影响范围", system_message)
        self.assertIn("当前状态", system_message)
        self.assertIn("waiting_for_approval", system_message)
        self.assertIn("主要风险", system_message)
        self.assertIn("将修改工作区文件", system_message)
        self.assertNotIn("1. 确认审批", system_message)
        self.assertNotIn("2. 修改需求", system_message)
        self.assertNotIn("3. 拒绝审批", system_message)
        self.assertNotIn("\x1b[", system_message)
        self.assertNotIn("```", system_message)
        self.assertNotIn("<div", system_message)
        self.assertNotIn("[AgentRelay 语义审批桥]", context["additionalContext"])
        state = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions/large-task.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("semantic_approval_candidate", state)

    def test_all_hook_card_fields_share_the_widest_label_column(self):
        from hooks import agent_relay_tracker as tracker

        card = tracker.render_task_gate_card("完整实现前后端和数据库功能", {})
        lines = [line for line in card.splitlines() if line.startswith("- ")]
        expected_width = max(tracker._card_label_width(line[2:].split("：", 1)[0]) for line in lines)
        for line in lines:
            label, value = line[2:].split("：", 1)
            self.assertEqual(
                len(value) - len(value.lstrip()),
                expected_width - tracker._card_label_width(label) + 1,
                line,
            )
        self.assertIn("- 主要风险：   ", card)
        self.assertIn("- 验证：       ", card)
        self.assertIn("- 回滚：       ", card)

    def test_complex_write_card_exposes_work_profile_and_five_columns(self):
        result = self.run_hook(self.payload(
            "complex-five-column",
            "UserPromptSubmit",
            prompt="同时修改前端和后端，并更新数据库迁移",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        system_message = output["systemMessage"]
        self.assertIn("- 工作档：", system_message)
        self.assertIn("strict", system_message)
        for label in ("现象", "期望", "定位", "边界", "疑点"):
            self.assertIn(f"- {label}：", system_message)
        self.assertIn("推荐方案", system_message)
        state = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions/complex-five-column.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["task_gate"]["work_profile"]["profile"], "strict")

    def test_task_approval_requires_an_explicit_non_downgraded_work_profile(self):
        session = "explicit-work-profile"
        request = "同时修改前端和后端，并更新数据库迁移"
        initial = self.run_hook(self.payload(
            session, "UserPromptSubmit", prompt=request,
        ))
        self.assert_hook_ok(initial)
        candidate = self.run_hook(self.payload(
            session, "UserPromptSubmit", prompt="批准执行",
        ))
        source_hash = self.semantic_source_hash(candidate)

        missing = self.run_tracker(
            "resolve-intent",
            "--session", session,
            "--decision", "approve",
            "--source-sha256", source_hash,
            "--reason", "用户明确批准当前范围",
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("工作档", missing.stderr)

        downgraded = self.resolve_intent(
            session, source_hash, work_profile="light",
        )
        self.assertEqual(downgraded.returncode, 2)
        self.assertIn("不能降档", downgraded.stderr)

        approved = self.resolve_intent(
            session, source_hash, work_profile="user_required",
        )
        self.assert_hook_ok(approved)
        state = json.loads(
            (
                self.codex_home / "agent_relay_tracker" / "sessions"
                / f"{session}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["task_gate"]["work_profile"]["profile"],
            "user_required",
        )
        self.assertEqual(
            state["task_gate"]["work_profile"]["selection_source"],
            "agent",
        )

    def test_classification_fallback_cannot_downgrade_explicit_full_review(self):
        from hooks import agent_relay_tracker as tracker

        with mock.patch.object(
            tracker,
            "_shared_script_module",
            side_effect=ValueError("policy unavailable"),
        ):
            classification = tracker.classify_task_request(
                "修复 app.py 的 bug，必须完整审查并运行全量测试，不能只看 diff"
            )

        work_profile = classification["work_profile"]
        self.assertTrue(classification["requires_confirmation"])
        self.assertTrue(work_profile["required"])
        self.assertEqual(work_profile["minimum"], "user_required")
        self.assertEqual(work_profile["profile"], "user_required")
        self.assertTrue(work_profile["card_required"])

    def test_simple_write_selects_light_without_visible_card(self):
        result = self.run_hook(self.payload(
            "simple-light",
            "UserPromptSubmit",
            prompt="修改按钮颜色",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        self.assertNotIn("systemMessage", output)
        self.assertIn("[AgentRelay 工作档]", output["hookSpecificOutput"]["additionalContext"])
        state = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions/simple-light.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["task_gate"]["work_profile"]["profile"], "light")
        self.assertFalse(state["task_gate"]["work_profile"]["card_required"])
        self.assertNotEqual(
            state["task_gate"]["work_profile"].get("selection_source"),
            "agent",
        )

        blocked = self.run_hook(self.payload(
            "simple-light",
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="simple-light-before-selection",
        ))
        self.assert_hook_ok(blocked)
        denied = json.loads(blocked.stdout)
        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn("工作档", denied["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotIn("systemMessage", denied)

        selected = self.run_tracker(
            "select-work-profile",
            "--session", "simple-light",
            "--profile", "light",
            "--reason", "单点样式小改",
        )
        self.assert_hook_ok(selected)
        selection = json.loads(selected.stdout)
        self.assertEqual(selection["profile"], "light")
        self.assertEqual(selection["selection_source"], "agent")

        allowed = self.run_hook(self.payload(
            "simple-light",
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="simple-light-after-selection",
        ))
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")

    def test_actual_write_requires_profile_even_when_prompt_verb_is_not_detected(self):
        session = "write-profile-runtime-fallback"
        initial = self.run_hook(self.payload(
            session,
            "UserPromptSubmit",
            prompt="把按钮弄成蓝色",
        ))
        self.assert_hook_ok(initial)
        self.assertEqual(initial.stdout, "")

        blocked = self.run_hook(self.payload(
            session,
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="runtime-profile-before-selection",
        ))
        self.assert_hook_ok(blocked)
        denied = json.loads(blocked.stdout)
        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn("工作档", denied["hookSpecificOutput"]["permissionDecisionReason"])

        selected = self.run_tracker(
            "select-work-profile",
            "--session", session,
            "--profile", "light",
            "--reason", "单点样式小改",
        )
        self.assert_hook_ok(selected)
        selection = json.loads(selected.stdout)
        self.assertEqual(selection["profile"], "light")
        self.assertFalse(selection["card_required"])

        allowed = self.run_hook(self.payload(
            session,
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="runtime-profile-after-selection",
        ))
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")

    def test_agent_upgrade_from_light_to_standard_requires_a_five_column_card(self):
        session = "light-to-standard"
        initial = self.run_hook(self.payload(
            session,
            "UserPromptSubmit",
            prompt="修改按钮颜色",
        ))
        self.assert_hook_ok(initial)

        selected = self.run_tracker(
            "select-work-profile",
            "--session", session,
            "--profile", "standard",
            "--reason", "主 Agent 判断需要标准证据链",
        )
        self.assert_hook_ok(selected)
        selection = json.loads(selected.stdout)
        self.assertEqual(selection["profile"], "standard")
        self.assertTrue(selection["card_required"])
        self.assertEqual(selection["status"], "awaiting_confirmation")
        for label in ("现象", "期望", "定位", "边界", "疑点"):
            self.assertIn(f"- {label}：", selection["card"])

        state = json.loads(
            (
                self.codex_home / "agent_relay_tracker" / "sessions"
                / f"{session}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["task_gate"]["status"],
            "awaiting_confirmation",
        )
        self.assertEqual(
            state["task_gate"]["work_profile"]["profile"],
            "standard",
        )

        blocked = self.run_hook(self.payload(
            session,
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="light-to-standard-write",
        ))
        self.assert_hook_ok(blocked)
        decision = json.loads(blocked.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("尚未获得用户批准", decision["permissionDecisionReason"])

    def test_natural_short_replies_require_agent_semantic_resolution(self):
        for index, reply in enumerate(("好的", "ok", "没问题", "执行吧", "行吧", "1", "[1]")):
            with self.subTest(reply=reply):
                session_id = f"semantic-approval-{index}"
                self.run_hook(self.payload(
                    session_id, "UserPromptSubmit",
                    prompt="完整实现前后端和数据库功能，并补充部署文档",
                ))
                patch = self.payload(
                    session_id, "PreToolUse", tool_name="apply_patch",
                    tool_input={"input": "*** Begin Patch"},
                    tool_use_id=f"semantic-approval-patch-{index}",
                )
                denied = self.run_hook(patch)
                self.assertEqual(
                    json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"],
                    "deny",
                )

                candidate = self.run_hook(self.payload(
                    session_id, "UserPromptSubmit", prompt=reply,
                ))
                source_hash = self.semantic_source_hash(candidate)
                self.assertNotIn("审批已记录", candidate.stdout)
                still_denied = self.run_hook(patch)
                self.assertEqual(
                    json.loads(still_denied.stdout)["hookSpecificOutput"]["permissionDecision"],
                    "deny",
                )

                resolved = self.resolve_intent(session_id, source_hash)
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                self.assertEqual(json.loads(resolved.stdout)["decision"], "approve")
                allowed = self.run_hook(patch)
                self.assert_hook_ok(allowed)
                self.assertEqual(allowed.stdout, "")

    def test_questions_negations_and_hypotheticals_cannot_write_approval(self):
        for index, prompt in enumerate((
            "确认执行吗？", "先别确认执行", "不要执行", "如果确认执行会怎么样",
        )):
            with self.subTest(prompt=prompt):
                session_id = f"blocked-semantic-approval-{index}"
                self.run_hook(self.payload(
                    session_id, "UserPromptSubmit",
                    prompt="完整实现前后端和数据库功能，并补充部署文档",
                ))
                candidate = self.run_hook(self.payload(
                    session_id, "UserPromptSubmit", prompt=prompt,
                ))
                source_hash = self.semantic_source_hash(candidate)
                rejected = self.resolve_intent(session_id, source_hash)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("不能写回批准", rejected.stderr)

                patch = self.run_hook(self.payload(
                    session_id, "PreToolUse", tool_name="apply_patch",
                    tool_input={"input": "*** Begin Patch"},
                    tool_use_id=f"blocked-semantic-patch-{index}",
                ))
                self.assertEqual(
                    json.loads(patch.stdout)["hookSpecificOutput"]["permissionDecision"],
                    "deny",
                )

    def test_semantic_resolution_rejects_stale_hash_and_repeated_writeback(self):
        session_id = "semantic-stale-hash"
        initial = self.run_hook(self.payload(
            session_id, "UserPromptSubmit",
            prompt="完整实现前后端和数据库功能，并补充部署文档",
        ))
        self.assert_hook_ok(initial)
        candidate = self.run_hook(self.payload(
            session_id, "UserPromptSubmit", prompt="可以",
        ))
        source_hash = self.semantic_source_hash(candidate)
        stale_hash = hashlib.sha256(b"an older user message").hexdigest()

        stale = self.resolve_intent(session_id, stale_hash)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("最后一条用户消息不一致", stale.stderr)

        resolved = self.resolve_intent(session_id, source_hash)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        repeated = self.resolve_intent(session_id, source_hash)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("没有有效的语义审批候选", repeated.stderr)

    def test_semantic_rejection_keeps_pending_writes_blocked(self):
        session_id = "semantic-reject"
        self.run_hook(self.payload(
            session_id, "UserPromptSubmit",
            prompt="完整实现前后端和数据库功能，并补充部署文档",
        ))
        candidate = self.run_hook(self.payload(
            session_id, "UserPromptSubmit", prompt="算了，先不做",
        ))
        source_hash = self.semantic_source_hash(candidate)
        resolved = self.resolve_intent(session_id, source_hash, decision="reject")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["decision"], "reject")

        patch = self.run_hook(self.payload(
            session_id, "PreToolUse", tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="semantic-rejected-patch",
        ))
        denied = json.loads(patch.stdout)["hookSpecificOutput"]
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("用户已拒绝当前待确认范围", denied["permissionDecisionReason"])

    def test_permission_modes_control_cards_without_weakening_danger_gate(self):
        script = PROJECT_ROOT / "scripts" / "approval_policy.py"
        for mode in ("strict", "standard", "relaxed"):
            subprocess.run([sys.executable, str(script), "set", mode,
                            "--codex-home", str(self.codex_home)], check=True, capture_output=True)
            result = self.run_hook(self.payload(
                f"mode-{mode}", "UserPromptSubmit",
                prompt="修改右上角导出按钮颜色为绿色",
            ))
            self.assert_hook_ok(result)
            output = json.loads(result.stdout) if result.stdout.strip() else {}
            if mode == "strict":
                self.assertIn("systemMessage", output)
            else:
                self.assertNotIn("systemMessage", output)

        dangerous = self.run_hook(self.payload(
            "mode-relaxed", "PreToolUse", tool_name="exec_command",
            tool_input={"cmd": "git reset --hard"}, tool_use_id="danger-mode",
        ))
        self.assertEqual(
            json.loads(dangerous.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        subprocess.run([sys.executable, str(script), "set", "standard",
                        "--codex-home", str(self.codex_home)], check=True, capture_output=True)

    def test_settings_command_accepts_numbers_only_while_menu_is_open(self):
        menu = self.run_hook(self.payload("approval-settings", "UserPromptSubmit", prompt="/设置"))
        self.assert_hook_ok(menu)
        output = json.loads(menu.stdout)
        self.assertIn("[1]", output["systemMessage"])
        self.assertIn("标准", output["systemMessage"])
        self.assertIn("请输入 1、2、3", output["hookSpecificOutput"]["additionalContext"])

        selected = self.run_hook(self.payload("approval-settings", "UserPromptSubmit", prompt="2"))
        self.assert_hook_ok(selected)
        self.assertIn("已保存", selected.stdout)
        saved = json.loads((self.codex_home / "agent_relay_tracker" / "approval-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "standard")
        unchanged = self.run_hook(self.payload("approval-settings", "UserPromptSubmit", prompt="3"))
        self.assert_hook_ok(unchanged)
        self.assertEqual(json.loads((self.codex_home / "agent_relay_tracker" / "approval-policy.json").read_text(encoding="utf-8"))["mode"], "standard")

        quick = self.run_hook(self.payload("approval-settings-quick", "UserPromptSubmit", prompt="/agent-approval 1"))
        self.assert_hook_ok(quick)
        self.assertIn("严格", quick.stdout)
        task = self.run_hook(self.payload("approval-settings-task", "UserPromptSubmit", prompt="完整重构整个项目"))
        self.assert_hook_ok(task)
        self.run_hook(self.payload("approval-settings-task", "UserPromptSubmit", prompt="/agent-approval 3"))
        pending = self.run_hook(self.payload("approval-settings-task", "UserPromptSubmit", prompt="请解释方案"))
        self.assert_hook_ok(pending)
        self.assertNotIn("审批已记录", pending.stdout)

    def test_installed_settings_menu_loads_its_policy_without_source_checkout(self):
        (self.codex_home / "hooks").mkdir(parents=True)
        for filename in ("agent_relay_hook.py", "agent_relay_tracker.py"):
            shutil.copy2(PROJECT_ROOT / "hooks" / filename, self.codex_home / "hooks" / filename)
        scripts = self.codex_home / "skills/agent-relay/scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(PROJECT_ROOT / "scripts/approval_policy.py", scripts / "approval_policy.py")
        shutil.copy2(PROJECT_ROOT / "scripts/page_verification.py", scripts / "page_verification.py")
        shutil.copy2(PROJECT_ROOT / "scripts/fusion_assist.py", scripts / "fusion_assist.py")
        installed = subprocess.run(
            [sys.executable, str(self.codex_home / "hooks/agent_relay_hook.py")],
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "installed-settings", "prompt": "/设置"}),
            text=True, capture_output=True, check=False, env=self.env, cwd=Path(tempfile.gettempdir()), timeout=30,
        )
        self.assert_hook_ok(installed)
        self.assertIn("[1]", json.loads(installed.stdout)["systemMessage"])

    def test_relaxed_small_style_write_does_not_create_full_audit(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "page.css"
        source.write_text(".button { border-radius: 50%; }", encoding="utf-8")
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "relaxed",
                        "--codex-home", str(self.codex_home)], check=True, capture_output=True)
        self.run_hook(self.payload("relaxed-style", "UserPromptSubmit", cwd=str(workspace),
                                  prompt="把圆形按钮改成方块，只改 border-radius"))
        blocked = self.run_hook(self.payload(
            "relaxed-style", "PreToolUse", cwd=str(workspace),
            tool_name="Edit", tool_input={"file_path": str(source)},
            tool_use_id="relaxed-style-before-profile",
        ))
        self.assert_hook_ok(blocked)
        self.assertIn("工作档", json.loads(blocked.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        selected = self.run_tracker(
            "select-work-profile",
            "--session", "relaxed-style",
            "--profile", "light",
            "--reason", "单点 border-radius 样式修改",
        )
        self.assert_hook_ok(selected)
        allowed = self.run_hook(self.payload(
            "relaxed-style", "PreToolUse", cwd=str(workspace),
            tool_name="Edit", tool_input={"file_path": str(source)},
            tool_use_id="relaxed-style-edit",
        ))
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")
        source.write_text(".button { border-radius: 0; }\n", encoding="utf-8")
        self.run_hook(self.payload("relaxed-style", "PostToolUse", cwd=str(workspace), tool_name="Edit",
                                  tool_input={"file_path": str(source)}, tool_response={"success": True}))
        result = self.run_hook(self.payload("relaxed-style", "Stop", cwd=str(workspace),
                                           last_assistant_message="按钮已改为方块，目标样式检查通过"))
        self.assert_hook_ok(result)
        self.assertNotIn("independent_review", result.stdout)
        self.assertIn("轻量样式验收", result.stdout)
        self.assertNotIn("完整交付审计", result.stdout.replace("不需要完整交付审计", ""))

    def test_relaxed_small_code_write_does_not_create_formal_audit(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "relaxed",
                        "--codex-home", str(self.codex_home)], check=True, capture_output=True)
        self.run_hook(self.payload("relaxed-code", "UserPromptSubmit", cwd=str(workspace),
                                  prompt="修复 app.py 的常量"))
        self.run_hook(self.payload("relaxed-code", "PostToolUse", cwd=str(workspace), tool_name="Edit",
                                  tool_input={"file_path": str(source)}, tool_response={"success": True}))
        audit_path = self.codex_home / "agent_relay_tracker" / "delivery-audit" / "relaxed-code.json"
        self.assertFalse(audit_path.exists())

    def test_shell_wrapper_forwards_change_summary_after_final_stop(self):
        workspace = self.codex_home / "shell-wrapper-workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        session = "shell-wrapper-change-summary"

        prompt = self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="修复 app.py 里的一个拼写错误",
        ), shell_wrapper=True)
        self.assert_hook_ok(prompt)

        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        written = self.run_hook(self.payload(
            session, "PostToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)},
            tool_response={"success": True},
        ), shell_wrapper=True)
        self.assert_hook_ok(written)

        stopped = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="已完成修改，拼写问题已修复。",
        ), shell_wrapper=True)
        self.assert_hook_ok(stopped)
        output = json.loads(stopped.stdout)
        self.assert_change_summary_format(output["systemMessage"])
        self.assertIn("app.py", output["systemMessage"])
        self.assertNotIn("decision", output)

    def test_claude_shim_forwards_change_summary_after_final_stop(self):
        workspace = self.codex_home / "claude-workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        session = "claude-change-summary"
        env = {
            **self.env,
            "AGENTRELAY_MAIN_HOOK": str(HOOK),
        }

        def run_claude(event, payload):
            return subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts/agentrelay_hook_shim.py"),
                    "--target",
                    "claude",
                    "--event",
                    event,
                ],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
                env=env,
            )

        prompt = run_claude("UserPromptSubmit", self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="修复 app.py 里的一个拼写错误",
        ))
        self.assert_hook_ok(prompt)

        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        written = run_claude("PostToolUse", self.payload(
            session, "PostToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)},
            tool_response={"success": True},
        ))
        self.assert_hook_ok(written)

        stopped = run_claude("Stop", self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="已完成修改，拼写问题已修复。",
        ))
        self.assert_hook_ok(stopped)
        output = json.loads(stopped.stdout)
        self.assert_change_summary_format(output["systemMessage"])
        self.assertIn("app.py", output["systemMessage"])
        self.assertNotIn("decision", output)

    def test_blocked_stop_does_not_record_until_the_gate_allows_completion(self):
        from scripts import delivery_audit

        workspace = self.codex_home / "blocked-stop-workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        session = "blocked-stop-change-summary"

        prompt = self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="修复 app.py 的常量错误",
        ))
        self.assert_hook_ok(prompt)
        state = json.loads(
            (
                self.codex_home / "agent_relay_tracker" / "sessions"
                / f"{session}.json"
            ).read_text(encoding="utf-8")
        )

        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        written = self.run_hook(self.payload(
            session, "PostToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)},
            tool_response={"success": True},
        ))
        self.assert_hook_ok(written)
        delivery_audit.begin(
            self.codex_home,
            session,
            workspace,
            "修复 app.py 的常量错误",
            mode="strict",
            files=[str(source)],
            task_id=state["task_id"],
            activate=True,
            work_profile="light",
            logic_change=False,
        )

        blocked = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="已完成修改，常量和测试均已修复。",
        ))
        self.assert_hook_ok(blocked)
        blocked_output = json.loads(blocked.stdout)
        self.assertEqual(blocked_output["decision"], "block")
        self.assertIn("交付审计", blocked_output["reason"])
        history_dir = (
            self.codex_home / "agent_relay_tracker" / "history"
            / "change-summaries"
        )
        self.assertEqual(list(history_dir.glob("*.json")), [])

        delivery_audit.close(self.codex_home, session)
        allowed = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="已完成修改，常量和测试均已修复。",
        ))
        self.assert_hook_ok(allowed)
        allowed_output = json.loads(allowed.stdout)
        self.assertIn("本次修改：", allowed_output["systemMessage"])
        history_files = list(history_dir.glob("*.json"))
        self.assertEqual(len(history_files), 1)
        history = json.loads(history_files[0].read_text(encoding="utf-8"))
        self.assertEqual(len(history["entries"]), 1)

    def test_delivery_audit_freezes_baseline_before_write_and_advances_candidate_after(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        session = "audit-baseline-before-write"

        self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="修复 app.py 的常量错误",
        ))
        selected = self.run_tracker(
            "select-work-profile",
            "--session", session,
            "--profile", "light",
            "--reason", "局部常量修复",
        )
        self.assert_hook_ok(selected)
        before = self.run_hook(self.payload(
            session, "PreToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_use_id="baseline-edit",
        ))
        self.assert_hook_ok(before)
        audit_path = (
            self.codex_home / "agent_relay_tracker" / "delivery-audit"
            / f"{session}.json"
        )
        initial = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(
            initial["baseline_fingerprint"],
            initial["candidate_fingerprint"],
        )
        self.assertTrue(initial["logic_change"])
        self.assertEqual(initial["work_profile"], "light")

        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        after = self.run_hook(self.payload(
            session, "PostToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_response={"success": True},
        ))
        self.assert_hook_ok(after)
        current = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(current["baseline_fingerprint"], initial["baseline_fingerprint"])
        self.assertNotEqual(current["candidate_fingerprint"], current["baseline_fingerprint"])
        self.assertEqual(current["fingerprint"], current["candidate_fingerprint"])

    def test_strict_delivery_requires_audit_before_completion(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "strict",
                        "--codex-home", str(self.codex_home)], check=True, capture_output=True)
        session = "audit-gate"
        initial = self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="修复 app.py 的空输入错误",
        ))
        self.assert_hook_ok(initial)
        self.assertIn("任务门禁", initial.stdout)
        candidate = self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace), prompt="批准执行",
        ))
        source_hash = self.semantic_source_hash(candidate)
        resolved = self.resolve_intent(session, source_hash)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        allowed = self.run_hook(self.payload(
            session, "PreToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_use_id="audit-gate-edit",
        ))
        self.assert_hook_ok(allowed)
        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        self.run_hook(self.payload(
            session, "PostToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_response={"success": True},
        ))
        blocked = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="已修复并验证通过",
        ))
        self.assert_hook_ok(blocked)
        self.assertIn("交付审计", blocked.stdout)
        self.assertIn("agent-audit", blocked.stdout)
        status = self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace), prompt="/agent-audit",
        ))
        self.assert_hook_ok(status)
        self.assertIn("pending", status.stdout)
        explanation = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message=(
                "agent-audit 用来检查交付是否真的完成了，"
                "不会仅凭一句“已完成”或“测试通过”就放行。"
            ),
        ))
        self.assert_hook_ok(explanation)
        self.assertNotIn("交付审计", explanation.stdout)
        screenshot_explanation = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message=(
                "/agent-audit 是修改完成后的交付检查清单。\n\n"
                "它用于检查：\n"
                "- 是否真的完成了你的原始需求，而不只是改了文件；\n"
                "- 实际 diff 是否越界、有没有未声明的副作用；\n"
                "- 测试或真实行为检查是否执行、结果如何；\n"
                "- 页面是否完成目标、边界和截图/无视觉验收；\n"
                "- 主 Agent 是否审查过；\n"
                "- 需要独立审查时，是否真的调用了 Fusion。\n\n"
                "输入 /agent-audit 只会显示当前待办，不会自动检查或自动通过。"
                "Agent 还需要实际执行检查、登记证据；缺证据、检查失败或审计后又有修改，"
                "就不能汇报“全部完成”。\n\n"
                "普通问答、搜索、只读检查不会创建这套审计流程。"
            ),
        ))
        self.assert_hook_ok(screenshot_explanation)
        self.assertNotIn("交付审计", screenshot_explanation.stdout)

    def test_informational_prompt_does_not_resume_old_audit_or_timer(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        self.run_hook(self.payload("question-lifecycle", "UserPromptSubmit", cwd=str(workspace),
                                  prompt="修复 app.py 的空输入错误"))
        self.run_hook(self.payload("question-lifecycle", "PreToolUse", cwd=str(workspace), tool_name="Edit",
                                  tool_input={"file_path": str(source)}, tool_use_id="question-edit"))
        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        self.run_hook(self.payload("question-lifecycle", "PostToolUse", cwd=str(workspace), tool_name="Edit",
                                  tool_input={"file_path": str(source)}, tool_response={"success": True}))
        question = self.run_hook(self.payload("question-lifecycle", "UserPromptSubmit", cwd=str(workspace),
                                            prompt="agent-audit 是干嘛的？"))
        self.assert_hook_ok(question)
        self.assertIn("问答模式", question.stdout)
        result = self.run_hook(self.payload("question-lifecycle", "Stop", cwd=str(workspace),
                                           last_assistant_message="它只是查看历史待办，不是继续执行的授权。"))
        self.assert_hook_ok(result)
        self.assertNotIn("交付审计", result.stdout)

    def test_background_notification_does_not_create_new_problem(self):
        result = self.run_hook(self.payload("background-lifecycle", "UserPromptSubmit",
                                           prompt="<task-notification>background command completed</task-notification>"))
        self.assert_hook_ok(result)
        self.assertEqual(result.stdout, "")
        session = self.codex_home / "agent_relay_tracker/sessions/background-lifecycle.json"
        self.assertFalse(session.exists())

    def test_requirement_confirmation_skips_delivery_audit_from_old_task(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "strict",
                        "--codex-home", str(self.codex_home)], check=True, capture_output=True)
        session = "requirement-not-delivery"
        self.run_hook(self.payload(session, "UserPromptSubmit",
                                  cwd=str(workspace), prompt="修复 app.py 的空输入错误"))
        self.run_hook(self.payload(
            session, "PreToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_use_id="requirement-edit",
        ))
        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        self.run_hook(self.payload(
            session, "PostToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_response={"success": True},
        ))
        self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="请完整重构整个项目的前端、后端和数据库，并补充测试和部署文档",
        ))

        result = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="需求已理解，现有测试通过；等待你确认后实施。",
        ))
        self.assert_hook_ok(result)
        self.assertNotIn("交付审计", result.stdout)

    def test_old_delivery_audit_is_ignored_after_small_task_switch(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        (workspace / "README.md").write_text("# Demo\n", encoding="utf-8")
        session = "old-audit-task-switch"
        self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="修复 app.py 的空输入错误",
        ))
        self.run_hook(self.payload(
            session, "PreToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_use_id="old-audit-edit",
        ))
        source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        self.run_hook(self.payload(
            session, "PostToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_response={"success": True},
        ))
        first = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions" / f"{session}.json").read_text(
                encoding="utf-8"
            )
        )
        self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="已定位问题，后续再修改。",
        ))
        self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="检查 README 的错别字",
        ))
        second = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions" / f"{session}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotEqual(first["task_id"], second["task_id"])
        result = self.run_hook(self.payload(
            session, "Stop", cwd=str(workspace),
            last_assistant_message="检查方案已整理，现有测试通过，等待你确认后再修改。",
        ))
        self.assert_hook_ok(result)
        self.assertNotIn("交付审计", result.stdout)

    def test_delivery_audit_infrastructure_error_fails_closed(self):
        from hooks import agent_relay_tracker as tracker

        session = "audit-error-fails-closed"
        audit_dir = self.codex_home / "agent_relay_tracker" / "delivery-audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / f"{session}.json").write_text('{"status": "pending"}', encoding="utf-8")
        with mock.patch.object(tracker, "CODEX_DIR", self.codex_home), \
                mock.patch.object(
                    tracker,
                    "read_state",
                    return_value={
                        "task_id": "current-task",
                        "problem_id": "current-task",
                        "task_gate": {"status": "clear"},
                    },
                ), \
                mock.patch.object(
                    tracker,
                    "_shared_script_module",
                    side_effect=ValueError("broken audit state"),
                ):
            reason = tracker.delivery_audit_completion_block(
                session, "已修复并通过测试。"
            )
        self.assertIn("不能声称完成", reason)

    def test_pre_write_audit_baseline_error_blocks_required_write(self):
        workspace = self.codex_home / "workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        session = "pre-write-audit-error"
        initial = self.run_hook(self.payload(
            session, "UserPromptSubmit", cwd=str(workspace),
            prompt="修复 app.py 的常量错误",
        ))
        self.assert_hook_ok(initial)
        selected = self.run_tracker(
            "select-work-profile",
            "--session", session,
            "--profile", "light",
            "--reason", "局部常量修复",
        )
        self.assert_hook_ok(selected)
        audit_dir = self.codex_home / "agent_relay_tracker" / "delivery-audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / f"{session}.json").write_text("{not-json", encoding="utf-8")

        blocked = self.run_hook(self.payload(
            session, "PreToolUse", cwd=str(workspace), tool_name="Edit",
            tool_input={"file_path": str(source)}, tool_use_id="audit-error-edit",
        ))
        self.assert_hook_ok(blocked)
        decision = json.loads(blocked.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("审计基线", decision["permissionDecisionReason"])

    def test_corrupt_delivery_audit_json_fails_closed(self):
        from hooks import agent_relay_tracker as tracker

        session = "corrupt-audit-fails-closed"
        audit_dir = self.codex_home / "agent_relay_tracker" / "delivery-audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / f"{session}.json").write_text("{not-json", encoding="utf-8")
        with mock.patch.object(tracker, "CODEX_DIR", self.codex_home), \
                mock.patch.object(
                    tracker,
                    "read_state",
                    return_value={
                        "task_id": "current-task",
                        "problem_id": "current-task",
                        "task_gate": {"status": "clear"},
                    },
                ):
            reason = tracker.delivery_audit_completion_block(
                session, "已修复并通过测试。"
            )
        self.assertIn("不能声称完成", reason)

    def test_legacy_or_previous_task_audit_cannot_block_new_completion_claim(self):
        from hooks import agent_relay_tracker as tracker

        session = "stale-audit-does-not-block"
        audit_dir = self.codex_home / "agent_relay_tracker" / "delivery-audit"
        audit_dir.mkdir(parents=True)
        audit_path = audit_dir / f"{session}.json"
        with mock.patch.object(tracker, "CODEX_DIR", self.codex_home), \
                mock.patch.object(
                    tracker,
                    "read_state",
                    return_value={
                        "task_id": "current-task",
                        "problem_id": "current-task",
                        "task_gate": {"status": "clear"},
                    },
                ):
            for stale_identity in ({}, {"task_id": "previous-task"}):
                with self.subTest(stale_identity=stale_identity):
                    audit_path.write_text(json.dumps({
                        "status": "pending",
                        "workspace": str(self.codex_home / "workspace"),
                        "checks": {},
                        **stale_identity,
                    }), encoding="utf-8")
                    reason = tracker.delivery_audit_completion_block(
                        session, "已修复并通过测试。"
                    )
                    self.assertIsNone(reason)

    def test_visual_gate_does_not_infer_missing_vision_from_missing_certificate(self):
        from hooks import agent_relay_tracker as tracker
        from scripts import page_verification
        from scripts import visual_review

        self.assertFalse(visual_review.visual_claim("我已读取你提供的图片"))
        messages = (
            "我已读取你提供的图片",
            "左右留白调整截图已实际查看，但 AgentRelay 未记录有效的视觉验收结果，"
            "因此不标记其验收通过。",
            "已停止，不再声明完成或视觉验收通过，等待后续指示。",
            "收到。左右留白调整截图已实际查看，但 AgentRelay 未记录有效的视觉验收结果，"
            "因此不标记其验收通过。\n\n代码修改及 72/72 测试通过已核实；"
            "AgentRelay 视觉验收仍未完成。",
        )
        for message in messages:
            with self.subTest(message=message), \
                    mock.patch.object(tracker, "_page_verification_module") as page:
                self.assertFalse(page_verification.completion_claim(message))
                self.assertFalse(visual_review.visual_claim(message))
                page.return_value.completion_claim.return_value = False
                page.return_value.read_state.return_value = {"files": {}}
                reason = tracker.visual_review_block_reason("no-page", message)
            self.assertIsNone(reason)

    def test_informational_prompt_is_not_hijacked_by_old_page_gate(self):
        session = "informational-does-not-audit"
        state_path = (
            self.codex_home / "agent_relay_tracker" / "page-verification"
            / f"{session}.json"
        )
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "files": {"index.html": {"sha256": "old-page-version"}},
        }), encoding="utf-8")

        explanation = self.run_hook(self.payload(
            session, "UserPromptSubmit", prompt="agent-audit 是干嘛的？",
        ))
        self.assert_hook_ok(explanation)
        self.assertNotIn("页面验证门禁", explanation.stdout)

        screenshot_wording = self.run_hook(self.payload(
            session,
            "UserPromptSubmit",
            prompt="你看看咋回事，为什么我让他回复这是干嘛的，他就直接开始审计了",
        ))
        self.assert_hook_ok(screenshot_wording)
        self.assertNotIn("页面验证门禁", screenshot_wording.stdout)

        action = self.run_hook(self.payload(
            session, "UserPromptSubmit", prompt="请帮我运行视觉检查并完成验收",
        ))
        self.assert_hook_ok(action)
        self.assertIn("页面验证门禁", action.stdout)

    def test_visible_card_is_not_requested_twice(self):
        session = "one-card-only"
        first = self.run_hook(self.payload(
            session, "UserPromptSubmit",
            prompt="完整实现前后端和数据库功能，并补充部署文档",
        ))
        self.assertIn("确认卡已显示", first.stdout)
        second = self.run_hook(self.payload(
            session, "UserPromptSubmit", prompt="继续检查这个方案",
        ))
        self.assertNotIn("systemMessage", second.stdout)

    def test_single_file_small_fix_does_not_emit_a_card(self):
        result = self.run_hook(self.payload(
            "small-fix",
            "UserPromptSubmit",
            prompt="修复 app.py 里的一个拼写错误",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        self.assertNotIn("systemMessage", output)
        self.assertIn(
            "[AgentRelay 工作档]",
            output["hookSpecificOutput"]["additionalContext"],
        )

    def test_finish_check_is_read_only_for_delivery_audit(self):
        from scripts import delivery_audit

        session = "finish-check-readonly"
        workspace = self.codex_home / "finish-check-workspace"
        workspace.mkdir()
        source = workspace / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        delivery_audit.begin(
            self.codex_home,
            session,
            workspace,
            "修改一个局部常量",
            mode="relaxed",
            files=[str(source)],
            activate=True,
        )
        before = delivery_audit.read_state(self.codex_home, session)
        self.assertTrue(before["active_edit"])

        completed = self.run_tracker(
            "finish-check",
            "--session", session,
            "--message", "修改已完成，验证通过。",
        )
        self.assert_hook_ok(completed)
        output = json.loads(completed.stdout)
        self.assertTrue(output["ok"])
        self.assertEqual(output["items"], [])
        after = delivery_audit.read_state(self.codex_home, session)
        self.assertTrue(after["active_edit"])
        self.assertEqual(after["status"], before["status"])

    def test_claude_target_keeps_context_and_supports_visible_card(self):
        self.env["AGENTRELAY_TARGET"] = "claude"
        result = self.run_hook(self.payload(
            "non-codex-large-task",
            "UserPromptSubmit",
            prompt="请重构整个项目的前端、后端和数据库，并补充测试和部署文档",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertIn("systemMessage", output)

    def test_kimi_target_keeps_context_without_visible_card(self):
        self.env["AGENTRELAY_TARGET"] = "kimi"
        result = self.run_hook(self.payload(
            "kimi-large-task",
            "UserPromptSubmit",
            prompt="请重构整个项目的前端、后端和数据库，并补充测试和部署文档",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertNotIn("systemMessage", output)

    def test_codex_without_target_environment_still_emits_visible_card(self):
        env = dict(self.env)
        for key in ("AGENTRELAY_TARGET", "CODEX_HOME", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            env.pop(key, None)
        env["HOME"] = str(self.codex_home)
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "strict",
             "--codex-home", str(self.codex_home / ".codex")],
            check=True, capture_output=True,
        )
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(self.payload(
                "codex-without-target-env", "UserPromptSubmit",
                prompt="请完整实现前后端和数据库功能，并补充测试和部署文档",
            )),
            text=True, capture_output=True, check=False, env=env, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertIn("「需求卡」", output["systemMessage"])

    def test_pi_target_keeps_context_and_supports_visible_card(self):
        self.env["AGENTRELAY_TARGET"] = "pi"
        result = self.run_hook(self.payload(
            "pi-large-task",
            "UserPromptSubmit",
            prompt="请重构整个项目的前端、后端和数据库，并补充测试和部署文档",
        ))

        self.assert_hook_ok(result)
        output = json.loads(result.stdout)
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertIn("systemMessage", output)

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
        self.assertIn("「确认卡」", system_message)
        self.assertIn("工具", system_message)
        self.assertIn("批准边界", system_message)
        self.assertNotIn("1. 确认审批", system_message)
        self.assertNotIn("\x1b[", system_message)

        candidate = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="OK",
        ))
        source_hash = self.semantic_source_hash(candidate)
        resolved = self.resolve_intent(session_id, source_hash)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)

        allowed = self.run_hook(patch)
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")

    def test_auto_ui_draft_does_not_break_task_approval(self):
        session_id = "approve-auto-ui-draft"
        prompt = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="修改综合查询页面样式，同时调整表格宽度",
        ))
        self.assert_hook_ok(prompt)
        self.assertIn("任务门禁", prompt.stdout)

        candidate = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="行吧",
        ))
        source_hash = self.semantic_source_hash(candidate)
        resolved = self.resolve_intent(session_id, source_hash)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)

        allowed = self.run_hook(self.payload(
            session_id,
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"file_path": "src/views/query/LinkedView.vue"},
            tool_use_id="auto-ui-draft-patch",
        ))
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")

    def test_reference_ui_follow_up_shows_requirement_card(self):
        session_id = "reference-ui-follow-up"
        initial = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="你现在启动这个前端，并且使用5181端口",
        ))
        self.assert_hook_ok(initial)
        initial_state = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions" / f"{session_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(initial_state["task_gate"]["status"], "clear")
        self.assertNotIn("systemMessage", initial.stdout and json.loads(initial.stdout))

        follow_up = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt=(
                "没错，但是你应该仔细看，他有一个点击事件，点击之后，他有一个遮罩层，"
                "这个得注意，得和修改完成的一样"
            ),
        ))
        self.assert_hook_ok(follow_up)
        output = json.loads(follow_up.stdout)
        self.assertIn("任务门禁", output["hookSpecificOutput"]["additionalContext"])
        self.assertIn("参考图或截图推导的 UI 修改范围", output["systemMessage"])
        self.assertIn("「需求卡」", output["systemMessage"])
        state = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions" / f"{session_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["task_gate"]["status"], "awaiting_confirmation")
        self.assertIsNotNone(state["task_gate"]["card_shown_sha256"])
        self.assertEqual(state["change_contract"]["draft"], True)

        blocked = self.run_hook(self.payload(
            session_id,
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"file_path": "src/views/reference-ui.vue"},
            tool_use_id="reference-ui-patch",
        ))
        self.assertEqual(
            json.loads(blocked.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

    def test_screenshot_ui_question_does_not_open_requirement_card(self):
        result = self.run_hook(self.payload(
            "reference-ui-question",
            "UserPromptSubmit",
            prompt="截图里这个遮罩是什么作用？",
        ))
        self.assert_hook_ok(result)
        if result.stdout:
            output = json.loads(result.stdout)
            self.assertNotIn("systemMessage", output)
        state = json.loads(
            (self.codex_home / "agent_relay_tracker/sessions/reference-ui-question.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["task_gate"]["status"], "clear")
        self.assertNotIn("change_contract", state)

    def test_approved_task_reopens_gate_for_a_new_large_scope(self):
        session_id = "new-scope"
        initial = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="完整实现前后端和数据库功能，并补充部署文档",
        ))
        self.assert_hook_ok(initial)
        self.assertIn("任务门禁", initial.stdout)

        candidate = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="1",
        ))
        source_hash = self.semantic_source_hash(candidate)
        resolved = self.resolve_intent(session_id, source_hash)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)

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
            tool_input={"cmd": "git reset --hard"},
            tool_use_id="rm-1",
        )

        denied = self.run_hook(command)
        self.assert_hook_ok(denied)
        denied_payload = json.loads(denied.stdout)
        denied_output = denied_payload["hookSpecificOutput"]
        self.assertEqual(denied_output["permissionDecision"], "deny")
        self.assertIn("Git 破坏性操作", denied_output["permissionDecisionReason"])
        system_message = denied_payload["systemMessage"]
        self.assertIn("危险操作确认卡", system_message)
        self.assertIn("主要风险", system_message)
        self.assertIn("[AgentRelay 安全门禁]", system_message)
        self.assertIn("检测到高风险操作：Git 破坏性操作", system_message)
        self.assertNotIn("2. 修改需求", system_message)
        self.assertNotIn("3. 拒绝审批", system_message)
        self.assertNotIn("\x1b[", system_message)

        candidate = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="没问题",
        ))
        source_hash = self.semantic_source_hash(candidate)
        resolved = self.resolve_intent(session_id, source_hash)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)

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
            "tool_input": {"cmd": "git reset --hard HEAD~1"},
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

    def test_explicit_temp_verification_screenshots_can_be_cleaned(self):
        evidence = Path(tempfile.mkdtemp(prefix="agentrelay-review-"))
        self.addCleanup(shutil.rmtree, evidence, True)
        screenshots = [
            evidence / "dcm-skeleton-review-before.png",
            evidence / "dcm-skeleton-review-after.png",
            evidence / "dcm-skeleton-review-final.png",
        ]
        for screenshot in screenshots:
            screenshot.write_bytes(b"\x89PNG\r\n\x1a\n")

        for index, command in enumerate((
            f"rm {' '.join(map(str, screenshots))}",
            f"rm -f {' '.join(map(str, screenshots))}",
            f"unlink {screenshots[0]}",
        )):
            result = self.run_hook(self.payload(
                f"safe-screenshot-cleanup-{index}",
                "PreToolUse",
                tool_name="exec_command",
                tool_input={"cmd": command},
                tool_use_id=f"safe-screenshot-cleanup-{index}",
            ))
            output = json.loads(result.stdout)
            self.assertNotIn("systemMessage", output)
            self.assertIn("deletion_policy.py", output["hookSpecificOutput"]["permissionDecisionReason"])

        chain = self.run_hook(self.payload(
            "safe-screenshot-chain", "PreToolUse", tool_name="exec_command",
            tool_input={"cmd": f"unlink {screenshots[1]} && unlink {screenshots[2]}"},
            tool_use_id="safe-screenshot-chain",
        ))
        self.assertIn("不允许整链一次批准", chain.stdout)

    def test_explicit_dcm_verification_directory_screenshots_can_be_cleaned(self):
        temp = Path(tempfile.mkdtemp(prefix="agentrelay-review-"))
        self.addCleanup(shutil.rmtree, temp, True)
        target_dir = temp / "dcm-analysis-browser"
        target_dir.mkdir()
        screenshots = [
            target_dir / "config-desktop.png",
            target_dir / "config-mobile.png",
        ]
        for screenshot in screenshots:
            screenshot.write_bytes(b"\x89PNG\r\n\x1a\n")

        for index, screenshot in enumerate(screenshots):
            result = self.run_hook(self.payload(
                f"safe-dcm-screenshot-cleanup-{index}",
                "PreToolUse", tool_name="exec_command",
                tool_input={"cmd": f"unlink {screenshot}"},
                tool_use_id=f"safe-dcm-screenshot-cleanup-{index}",
            ))
            output = json.loads(result.stdout)
            self.assertNotIn("systemMessage", output)
            self.assertIn("deletion_policy.py", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_screenshot_cleanup_exemption_remains_narrow(self):
        evidence = Path(tempfile.mkdtemp(prefix="agentrelay-review-"))
        self.addCleanup(shutil.rmtree, evidence, True)
        screenshot = evidence / "review-before.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\n")
        ordinary_image = evidence / "important.png"
        ordinary_image.write_bytes(b"\x89PNG\r\n\x1a\n")
        outside_image = PROJECT_ROOT / f".screenshot-cleanup-test-{os.getpid()}.png"
        self.addCleanup(outside_image.unlink, missing_ok=True)
        outside_image.write_bytes(b"\x89PNG\r\n\x1a\n")

        cases = (
            f"rm {screenshot} {ordinary_image}",
            f"rm {evidence / 'review-*.png'}",
            f"rm {outside_image}",
        )
        for index, command in enumerate(cases):
            result = self.run_hook(self.payload(
                f"blocked-screenshot-cleanup-{index}",
                "PreToolUse",
                tool_name="exec_command",
                tool_input={"cmd": command},
                tool_use_id=f"blocked-screenshot-cleanup-{index}",
            ))
            self.assert_hook_ok(result)
            output = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(output["permissionDecision"], "deny", command)
            self.assertIn("删除文件或目录", output["permissionDecisionReason"])

    def test_policy_tightening_invalidates_pending_danger_approval(self):
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "relaxed",
             "--codex-home", str(self.codex_home)],
            check=True, capture_output=True,
        )
        session = "policy-tightening"
        command = self.payload(
            session, "PreToolUse", tool_name="exec_command",
            tool_input={"cmd": "git reset --hard"}, tool_use_id="policy-danger",
        )
        denied = self.run_hook(command)
        self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        candidate = self.run_hook(self.payload(session, "UserPromptSubmit", prompt="批准"))
        resolved = self.resolve_intent(session, self.semantic_source_hash(candidate))
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        # Do not consume the one-shot approval before tightening the policy.

        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "standard",
             "--codex-home", str(self.codex_home)],
            check=True, capture_output=True,
        )
        blocked = self.run_hook(command)
        self.assertEqual(json.loads(blocked.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("权限模式比批准时更严格", blocked.stdout)

    def test_default_temporary_evidence_is_recycled_without_direct_delete(self):
        evidence = Path(tempfile.mkdtemp(prefix="agentrelay-review-")) / "agentrelay-review-before.png"
        evidence.write_bytes(b"image")
        payload = self.payload(
            "recycle-default", "PreToolUse", tool_name="exec_command",
            tool_input={"cmd": f"rm {evidence}"}, tool_use_id="recycle-default",
        )
        result = self.run_hook(payload)
        output = json.loads(result.stdout)
        self.assertNotIn("systemMessage", output)
        replacement = output["hookSpecificOutput"]["permissionDecisionReason"].splitlines()[-1]
        self.assertIn("deletion_policy.py", replacement)
        self.assertEqual(self.run_hook({**payload, "tool_input": {"cmd": replacement}}).stdout, "")
        completed = subprocess.run(replacement, shell=True, cwd=PROJECT_ROOT, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(evidence.exists())
        manifest = self.codex_home / "agent_relay_tracker" / "trash" / "manifests"
        self.assertTrue(list(manifest.glob("*.json")))

    def test_default_persistent_delete_still_requires_approval(self):
        target = self.codex_home / "important-user-document.txt"
        target.write_text("keep")
        result = self.run_hook(self.payload(
            "persistent-default", "PreToolUse", tool_name="exec_command",
            tool_input={"cmd": f"rm {target}"}, tool_use_id="persistent-default",
        ))
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("危险操作确认卡", output["systemMessage"])
        self.assertIn("尚未获得用户批准", output["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertTrue(target.exists())

    def test_strict_delete_confirms_then_requires_managed_recycle(self):
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "strict",
             "--codex-home", str(self.codex_home)],
            check=True, capture_output=True,
        )
        session_id = "strict-recycle"
        target = self.codex_home / "old-output.txt"
        target.write_text("generated")
        payload = self.payload(
            session_id,
            "PreToolUse",
            tool_name="exec_command",
            tool_input={"cmd": f"rm {target}"},
            tool_use_id="strict-rm",
        )

        denied = self.run_hook(payload)
        self.assert_hook_ok(denied)
        denied_output = json.loads(denied.stdout)
        self.assertEqual(
            denied_output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn("危险操作确认卡", denied_output["systemMessage"])
        self.assertIn("尚未获得用户批准", denied_output["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotIn("未显示额外审批卡", denied.stdout)
        self.assertTrue(target.exists())

        candidate = self.run_hook(self.payload(
            session_id,
            "UserPromptSubmit",
            prompt="批准",
        ))
        resolved = self.resolve_intent(session_id, self.semantic_source_hash(candidate))
        self.assertEqual(resolved.returncode, 0, resolved.stderr)

        recycled = self.run_hook(payload)
        self.assert_hook_ok(recycled)
        recycled_output = json.loads(recycled.stdout)
        self.assertEqual(
            recycled_output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertNotIn("systemMessage", recycled_output)
        reason = recycled_output["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("strict 模式不执行原始永久删除", reason)
        replacement = reason.splitlines()[-1]
        self.assertIn("deletion_policy.py", replacement)
        self.assertTrue(target.exists())

        managed = self.run_hook({
            **payload,
            "tool_input": {"cmd": replacement},
            "tool_use_id": "strict-managed-rm",
        })
        self.assert_hook_ok(managed)
        self.assertEqual(managed.stdout, "")
        completed = subprocess.run(
            replacement,
            shell=True,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(target.exists())
        manifests = self.codex_home / "agent_relay_tracker" / "trash" / "manifests"
        manifest = max(manifests.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
        receipt = json.loads(manifest.read_text(encoding="utf-8"))
        destination = Path(receipt["items"][0]["destination"])
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_text(encoding="utf-8"), "generated")

    def test_relaxed_delete_keeps_direct_behavior(self):
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts/approval_policy.py"), "set", "relaxed",
             "--codex-home", str(self.codex_home)],
            check=True, capture_output=True,
        )
        target = self.codex_home / "disposable.txt"
        target.write_text("delete")
        result = self.run_hook(self.payload(
            "relaxed-delete", "PreToolUse", tool_name="exec_command",
            tool_input={"cmd": f"rm {target}"}, tool_use_id="relaxed-delete",
        ))
        self.assert_hook_ok(result)
        self.assertEqual(result.stdout, "")
        self.assertTrue(target.exists())
        completed = subprocess.run(["rm", str(target)], check=False, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertFalse(target.exists())

    def test_dangerous_chain_cannot_be_approved_as_one_action(self):
        result = self.run_hook(self.payload(
            "danger-chain", "PreToolUse", tool_name="exec_command",
            tool_input={"cmd": "rm -rf /tmp/example && git clone https://example.invalid/repo /tmp/example"},
            tool_use_id="danger-chain",
        ))
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("不允许整链一次批准", output["hookSpecificOutput"]["permissionDecisionReason"])

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
        self.assertIn("当前任务尚未获得用户批准", output["permissionDecisionReason"])

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
        other_output = json.loads(safe_other_session.stdout)["hookSpecificOutput"]
        self.assertEqual(other_output["permissionDecision"], "deny")
        self.assertIn("工作档", other_output["permissionDecisionReason"])
        self.assertNotIn(
            "当前任务尚未获得用户批准",
            other_output["permissionDecisionReason"],
        )
        selected = self.run_tracker(
            "select-work-profile",
            "--session", "session-b",
            "--profile", "light",
            "--reason", "隔离测试中的单点写入",
        )
        self.assert_hook_ok(selected)
        allowed = self.run_hook(self.payload(
            "session-b",
            "PreToolUse",
            tool_name="apply_patch",
            tool_input={"input": "*** Begin Patch"},
            tool_use_id="b-patch-after-profile",
        ))
        self.assert_hook_ok(allowed)
        self.assertEqual(allowed.stdout, "")

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
            "「需求卡」",
            json.loads(result.stdout)["systemMessage"],
        )


if __name__ == "__main__":
    unittest.main()
