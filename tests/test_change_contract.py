import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import change_contract as cc
from scripts import agentrelay_console
from scripts import config_manager
from scripts.orchestrator_core import ConfirmationCard, ConfirmationStore, Orchestrator
from scripts.research_commander import CommanderAgent, CommanderProcess


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "page.css").write_text("a { color: red; }", encoding="utf-8")
        (self.root / "api.py").write_text("VALUE = 1", encoding="utf-8")
        (self.root / "components").mkdir(exist_ok=True)
        (self.root / "components" / "SkeletonLoader.vue").write_text("<template />", encoding="utf-8")
        (self.root / "components" / "GlobalScrollbar.vue").write_text("<template />", encoding="utf-8")
        (self.root / "styles").mkdir()
        (self.root / "styles" / "scrollbar-base.css").write_text("/* shared */", encoding="utf-8")

    def contract(self, **updates):
        value = cc.contract_for_prompt("只修改样式，参考旧版表格", str(self.root),
                                       ["page.css"], ["legacy/OrderTable.vue"], style_only=True)
        value.update(updates)
        return value

    def test_reuse_evidence_is_required_before_ui_write(self):
        contract = cc.approve(self.contract(target_scope="A 模块骨架屏", reuse_checked=False))
        reason = cc.check_tool_write(contract, "Edit", {"file_path": "page.css"})
        self.assertIn("复用", reason)
        self.assertIn("检索", reason)
        contract["reuse_checked"] = True
        reason = cc.check_tool_write(contract, "Edit", {"file_path": "page.css"})
        self.assertIn("另造", reason)

    def test_existing_component_can_be_reused_without_new_component_approval(self):
        contract = cc.approve(self.contract(target_scope="A 模块骨架屏",
            reuse_checked=True, reuse_evidence=["components/SkeletonLoader.vue"]))
        self.assertIsNone(cc.check_tool_write(contract, "Edit", {"file_path": "page.css"}))
        fields = dict(cc.card_fields(contract))
        self.assertIn("SkeletonLoader", fields["复用优先"])

    def test_new_component_requires_explicit_approval(self):
        contract = cc.approve(self.contract(target_scope="新骨架组件", reuse_checked=True,
                                             new_component_approved=False))
        self.assertIn("另造", cc.check_tool_write(contract, "Edit", {"file_path": "page.css"}))
        contract["new_component_approved"] = True
        self.assertIsNone(cc.check_tool_write(contract, "Edit", {"file_path": "page.css"}))

    def test_ui_prompt_automatically_creates_pending_contract(self):
        from hooks import agent_relay_tracker as tracker
        state = tracker.default_state(session_id="auto-ui")
        tracker.ensure_change_contract(state, "给订单页面增加骨架屏", {"cwd": str(self.root)})
        contract = state["change_contract"]
        self.assertFalse(contract["approved"])
        self.assertIn("骨架屏", contract["target_scope"])
        self.assertFalse(contract["reuse_checked"])
        with self.assertRaisesRegex(ValueError, "具体 UI 参考"):
            cc.approve(contract)

    def test_ordinary_button_text_change_creates_nonblocking_draft(self):
        from hooks import agent_relay_tracker as tracker
        state = tracker.default_state(session_id="button-text")
        tracker.ensure_change_contract(state, "把页面上这个按钮文字改成保存", {"cwd": str(self.root)})
        self.assertTrue(state["change_contract"]["draft"])
        self.assertIn("按钮", state["change_contract"]["target_scope"])
        self.assertIsNone(cc.check_tool_write(state["change_contract"], "Edit", {"file_path": "page.css"}))
        self.assertIsNone(cc.completion_block(state["change_contract"], "页面已完成"))

    def test_non_ui_prompt_does_not_create_contract(self):
        from hooks import agent_relay_tracker as tracker
        state = tracker.default_state(session_id="auto-backend")
        tracker.ensure_change_contract(state, "修改 Python API 的超时时间", {"cwd": str(self.root)})
        self.assertNotIn("change_contract", state)

    def test_reuse_evidence_must_point_to_real_project_files(self):
        contract = cc.approve(self.contract(target_scope="A 模块骨架屏",
            reuse_checked=True, reuse_evidence=["components/DoesNotExist.vue"]))
        with self.assertRaisesRegex(ValueError, "复用证据不存在"):
            cc.validate_reuse_evidence(contract)
        (self.root / "components").mkdir(exist_ok=True)
        (self.root / "components" / "SkeletonLoader.vue").write_text("<template />", encoding="utf-8")
        contract["reuse_evidence"] = ["components/SkeletonLoader.vue"]
        self.assertEqual(cc.validate_reuse_evidence(contract), ["components/SkeletonLoader.vue"])

    def test_target_outside_checks_require_actual_results(self):
        contract = self.contract(unchanged_checks=["导出按钮不移位", "其他模块滚动条无变化"])
        with self.assertRaisesRegex(ValueError, "目标外检查结果"):
            cc.validate_unchanged_evidence(contract)
        contract["unchanged_results"] = [{"check": "导出按钮不移位", "passed": True,
                                           "actual": "x,y,width,height 与修改前一致"}]
        with self.assertRaisesRegex(ValueError, "目标外检查未覆盖"):
            cc.validate_unchanged_evidence(contract)
        contract["unchanged_results"].append({"check": "其他模块滚动条无变化", "passed": True,
                                               "actual": "样式与滚动行为一致"})
        self.assertTrue(cc.validate_unchanged_evidence(contract))

    def test_local_scrollbar_scope_blocks_shared_component(self):
        contract = self.contract(target_scope="订单模块 .details-panel 的滚动条",
                                 allowed_properties=["scrollbar-width"],
                                 reuse_checked=True, reuse_evidence=["styles/scrollbar-base.css"],
                                 shared_files=["page.css"], shared_impact=False)
        contract = cc.approve(contract)
        reason = cc.check_tool_write(contract, "Edit", {"file_path": "page.css"})
        self.assertIn("共享", reason)
        self.assertIn("订单模块", reason)
        self.assertIn("重新确认", reason)

    def test_local_scrollbar_shared_change_also_fails_final_diff(self):
        contract = cc.approve(self.contract(target_scope="A 模块滚动条",
                                            reuse_checked=True, reuse_evidence=["styles/scrollbar-base.css"],
                                            shared_files=["page.css"], shared_impact=False))
        (self.root / "page.css").write_text("::-webkit-scrollbar { width: 2px; }", encoding="utf-8")
        self.assertTrue(any("共享组件" in item for item in cc.diff_violations(contract)))

    def test_explicit_global_scope_can_use_shared_component(self):
        contract = self.contract(target_scope="整个项目全部滚动条",
                                 allowed_properties=["scrollbar-width"],
                                 reuse_checked=True, reuse_evidence=["components/GlobalScrollbar.vue"],
                                 shared_files=["page.css"], shared_impact=True)
        contract = cc.approve(contract)
        self.assertIsNone(cc.check_tool_write(contract, "Edit", {"file_path": "page.css"}))

    def test_card_shows_element_scope_side_effects_and_unchanged_checks(self):
        contract = self.contract(target_scope="A 模块输入框",
                                 allowed_properties=["max-width"],
                                 forbidden_effects=["导出按钮位置", "父容器 flex 布局"],
                                 unchanged_checks=["导出按钮仍在最右侧", "B/C 模块无变化"])
        fields = dict(cc.card_fields(contract))
        self.assertIn("A 模块输入框", fields["作用域"])
        self.assertIn("max-width", fields["允许属性"])
        self.assertIn("导出按钮位置", fields["禁止副作用"])
        self.assertIn("B/C 模块", fields["不变验收"])

    def test_strict_is_default_and_card_contains_all_boundaries(self):
        contract = self.contract()
        self.assertEqual(contract["mode"], "strict")
        fields = dict(cc.card_fields(contract))
        self.assertIn("legacy/OrderTable.vue", fields["UI 参考"])
        self.assertIn("page.css", fields["允许修改"])
        self.assertIn("业务逻辑", fields["禁止修改"])
        self.assertIn("不修复", fields["额外 Bug"])

    def test_explicit_creative_permission_does_not_expand_file_scope(self):
        contract = cc.contract_for_prompt("可以自由发挥优化得更好看", str(self.root),
                                          ["page.css"], ["prototype.png"])
        self.assertEqual(contract["mode"], "creative")
        approved = cc.approve(contract)
        self.assertIn("不在允许修改名单", cc.check_tool_write(approved, "Edit", {"file_path": "api.py"}))

    def test_reference_and_allowlist_are_required(self):
        with self.assertRaisesRegex(ValueError, "UI 参考"):
            cc.approve(cc.contract_for_prompt("按当前风格改", str(self.root), ["page.css"]))
        with self.assertRaisesRegex(ValueError, "允许修改"):
            cc.approve(cc.contract_for_prompt("按当前风格改", str(self.root), [], ["old.html"]))

    def test_write_outside_scope_and_workspace_is_blocked(self):
        contract = cc.approve(self.contract())
        self.assertIn("不在允许修改名单", cc.check_tool_write(contract, "Edit", {"file_path": "api.py"}))
        self.assertIn("越出 workspace", cc.check_tool_write(contract, "Edit", {"file_path": "/tmp/outside.css"}))
        self.assertIsNone(cc.check_tool_write(contract, "Edit", {"file_path": "page.css"}))

    def test_approval_freezes_baseline_and_final_diff_detects_out_of_scope(self):
        contract = cc.approve(self.contract())
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "page.css", "api.py"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "baseline"], cwd=self.root, check=True)
        (self.root / "api.py").write_text("VALUE = 2", encoding="utf-8")
        contract["reference_verified"] = True
        evidence = self.root / "reference-check.txt"
        evidence.write_text("compared with legacy table", encoding="utf-8")
        contract["reference_evidence"] = [str(evidence)]
        reason = cc.completion_block(contract, "页面已完成，验证通过")
        self.assertIn("api.py", reason)
        self.assertIn("不能声称按约束完成", reason)

    def test_style_task_rejects_business_file_even_if_mistakenly_listed(self):
        contract = self.contract(allowed_files=["page.css", "api.py"])
        contract = cc.approve(contract)
        (self.root / "api.py").write_text("VALUE = 2", encoding="utf-8")
        self.assertTrue(any("样式任务" in item for item in cc.diff_violations(contract)))

    def test_reference_evidence_must_be_itemized_and_cover_target_and_invariants(self):
        contract = cc.approve(self.contract(target_scope="A 模块输入框", unchanged_checks=["导出按钮不移位"]))
        contract["unchanged_results"] = [{"check": "导出按钮不移位", "passed": True, "actual": "rect 未变化"}]
        state_dir = self.root / "agent_relay_tracker/sessions"
        state_dir.mkdir(parents=True)
        (state_dir / "reference.json").write_text(json.dumps({"change_contract": contract}), encoding="utf-8")
        arbitrary = self.root / "one-character.txt"
        arbitrary.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "对照"):
            cc.record_reference_evidence(self.root, "reference", [str(arbitrary)])
        incomplete = self.root / "incomplete.txt"
        incomplete.write_text("A 模块输入框：颜色一致", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "目标外"):
            cc.record_reference_evidence(self.root, "reference", [str(incomplete)])
        complete = self.root / "comparison.txt"
        complete.write_text("A 模块输入框：computed style、位置尺寸与参考一致\n导出按钮不移位：before/after rect 相同\n", encoding="utf-8")
        verified = cc.record_reference_evidence(self.root, "reference", [str(complete)])
        self.assertTrue(verified["reference_verified"])

    def test_child_environment_and_prompt_share_same_contract(self):
        contract = cc.approve(self.contract())
        env = cc.with_child_environment(contract, {"PATH": "/bin"})
        self.assertEqual(cc.from_environment(env)["allowed_files"], ["page.css"])
        self.assertNotIn("PATH", cc.child_prompt(contract))
        self.assertIn("子 Agent 必须原样遵守", cc.child_prompt(contract))
        self.assertIn("legacy/OrderTable.vue", cc.child_prompt(contract))
    def test_scope_change_requires_new_approval_and_baseline(self):
        contract = cc.approve(self.contract())
        expanded = dict(contract, allowed_files=["page.css", "api.py"], approved=False)
        self.assertIsNone(cc.check_tool_write(expanded, "Edit", {"file_path": "api.py"}))
        self.assertEqual(expanded["approved"], False)
        with self.assertRaisesRegex(ValueError, "允许修改的文件"):
            cc.approve(dict(expanded, allowed_files=[]))

    def test_reference_comparison_is_required_for_completion_claim(self):
        contract = cc.approve(self.contract())
        contract = dict(contract, preexisting_changes=["comparison.md", "reference-comparison.txt"])
        reason = cc.completion_block(contract, "页面已完成")
        self.assertIn("尚未对照指定 UI 参考", reason)
        evidence = self.root / "comparison.md"
        evidence.write_text("color, type, spacing and interactions compared", encoding="utf-8")
        contract.update(reference_verified=True, reference_evidence=[str(evidence)])
        self.assertIsNone(cc.completion_block(contract, "页面已完成"))

    def test_non_git_new_out_of_scope_file_is_detected(self):
        contract = cc.approve(self.contract())
        (self.root / "unrelated.py").write_text("VALUE = 3", encoding="utf-8")
        self.assertTrue(any("unrelated.py" in item for item in cc.diff_violations(contract)))

    def test_preexisting_changes_are_not_misattributed_to_this_contract(self):
        (self.root / "api.py").write_text("VALUE = 2", encoding="utf-8")
        contract = cc.approve(self.contract())
        self.assertEqual(cc.diff_violations(contract), [])

    def test_card_persistence_and_render_keep_contract(self):
        contract = self.contract()
        store = ConfirmationStore(self.root / "cards")
        store.create(ConfirmationCard("style-task", "execute", ("page.css",), ("参考一致",),
                                      workspace=str(self.root), allowed_files=("page.css",),
                                      change_contract=contract))
        loaded = ConfirmationStore(self.root / "cards").cards["style-task"]
        self.assertEqual(loaded.change_contract["references"], ["legacy/OrderTable.vue"])
        rendered = agentrelay_console.render_confirmation_card(loaded)
        self.assertIn("UI 参考", rendered)
        self.assertIn("默认严格参考", rendered)
        self.assertIn("额外 Bug", rendered)
        transitioned = store.transition("style-task", "approved")
        self.assertEqual(transitioned.change_contract, contract)

    def test_commander_child_process_receives_contract_without_api_keys(self):
        contract = self.contract()
        agent = CommanderAgent("worker", "frontend", str(self.root), model_config={
            "change_contract": contract, "task_id": "task-1"})
        captured = {}
        class Process:
            pid = 123
            returncode = 0
            def communicate(self, timeout=None):
                return "", ""
        with mock.patch.dict(os.environ, {"SECRET_API_KEY": "do-not-copy"}, clear=False), \
             mock.patch("subprocess.Popen", return_value=Process()) as popen:
            CommanderProcess().run(agent, ["python", "-c", "print('ok')"])
        env = popen.call_args.kwargs["env"]
        self.assertEqual(json.loads(env[cc.ENV_CONTRACT])["allowed_files"], ["page.css"])
        self.assertNotIn("SECRET_API_KEY", env)

    def test_fusion_session_contains_contract(self):
        from scripts import fusion_runner
        contract = cc.approve(self.contract())
        captured = {}
        def call_model(messages, tools, timeout):
            captured["messages"] = messages
            return {"role": "assistant", "content": "done"}
        config_manager.save_config({"fusion": {"enabled": True, "provider_id": "local:test"},
            "local_providers": [{"id": "test", "endpoint": "http://127.0.0.1:9/v1", "model": "fusion-test"}]}, self.root / "home")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "home")}, clear=False), \
             mock.patch.object(fusion_runner.fusion_mode, "finish_fusion_run"):
            result = fusion_runner.run_sidekick("改样式", workspace=self.root, provider_ref="local:test",
                provider_kind="local", tool_mode="readonly", max_steps=1, timeout=1,
                home=self.root / "home", lock_dir=self.root / "locks",
                environ=cc.with_child_environment(contract, {}), call_model=call_model)
        self.assertEqual(result["status"], "success")
        self.assertIn("子 Agent 必须原样遵守", captured["messages"][1]["content"])


class HookContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        (self.workspace / "page.css").write_text("a{}", encoding="utf-8")
        (self.workspace / "api.py").write_text("VALUE=1", encoding="utf-8")
        self.session = "strict-contract"
        self.env = {**os.environ, "CODEX_HOME": str(self.home), "AGENTRELAY_TARGET": "claude"}

    def hook(self, event, **values):
        payload = {"hook_event_name": event, "session_id": self.session,
                   "cwd": str(self.workspace), **values}
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "hooks" / "agent_relay_hook.py")],
                                input=json.dumps(payload), text=True, capture_output=True,
                                env=self.env, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def test_hook_blocks_approved_write_outside_contract(self):
        contract = cc.contract_for_prompt("只修改样式", str(self.workspace), ["page.css"], ["old.css"])
        contract["approved"] = True
        state_dir = self.home / "agent_relay_tracker" / "sessions"
        state_dir.mkdir(parents=True)
        (state_dir / f"{self.session}.json").write_text(json.dumps({"session_id": self.session,
            "task_gate": {"status": "approved"}, "change_contract": contract}), encoding="utf-8")
        denied = self.hook("PreToolUse", tool_name="Edit", tool_use_id="out-of-scope",
                           tool_input={"file_path": str(self.workspace / "api.py")})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("不在允许修改名单", denied["hookSpecificOutput"]["permissionDecisionReason"])
    def test_hook_stop_rejects_real_diff_outside_contract(self):
        contract = cc.approve(cc.contract_for_prompt("只修改样式", str(self.workspace),
                                                     ["page.css"], ["old.css"], style_only=True))
        state_dir = self.home / "agent_relay_tracker" / "sessions"
        state_dir.mkdir(parents=True)
        (state_dir / f"{self.session}.json").write_text(json.dumps({"session_id": self.session,
            "task_gate": {"status": "approved"}, "change_contract": contract}), encoding="utf-8")
        contract["reference_verified"] = True
        evidence = self.home / "reference-comparison.txt"
        evidence.write_text("compared", encoding="utf-8")
        contract["reference_evidence"] = [str(evidence)]
        (state_dir / f"{self.session}.json").write_text(json.dumps({"session_id": self.session,
            "task_gate": {"status": "approved"}, "change_contract": contract}), encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(["git", "add", "page.css", "api.py"], cwd=self.workspace, check=True)
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "baseline"], cwd=self.workspace, check=True)
        (self.workspace / "api.py").write_text("VALUE=2", encoding="utf-8")
        denied = self.hook("Stop", last_assistant_message="页面已完成，验证通过")
        self.assertEqual(denied["decision"], "block")
        self.assertIn("api.py", denied["reason"])
    def test_hook_contract_approval_and_reference_evidence_flow(self):
        proposal = cc.contract_for_prompt("只修改样式", str(self.workspace), ["page.css"], ["old.css"])
        proposed = self.hook("UserPromptSubmit", prompt="只修改样式", change_contract=proposal)
        context = proposed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("严格变更约束", context)
        approval_reply = "确认，按这个方案执行"
        candidate = self.hook("UserPromptSubmit", prompt=approval_reply)
        candidate_context = candidate["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[AgentRelay 语义审批桥]", candidate_context)
        match = re.search(r"--source-sha256 ([0-9a-f]{64})", candidate_context)
        self.assertIsNotNone(match, candidate_context)
        resolved = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "hooks" / "agent_relay_tracker.py"),
                "resolve-intent",
                "--session", self.session,
                "--decision", "approve",
                "--source-sha256", match.group(1),
                "--reason", "主 Agent 结合确认卡和完整上下文判断用户已批准",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
            timeout=30,
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        allowed = self.hook("PreToolUse", tool_name="Edit", tool_use_id="style-edit",
                            tool_input={"file_path": str(self.workspace / "page.css")})
        self.assertNotIn("hookSpecificOutput", allowed)
        evidence = self.home / "reference-comparison.txt"
        evidence.write_text("colors, spacing and behavior compared", encoding="utf-8")
        contract = cc.record_reference_evidence(self.home, self.session, [str(evidence)])
        self.assertTrue(contract["reference_verified"])
        result = self.hook("Stop", last_assistant_message="页面已完成，验证通过")
        self.assertNotIn("变更约束验收失败", result.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
