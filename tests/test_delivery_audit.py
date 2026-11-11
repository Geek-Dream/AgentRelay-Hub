import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import delivery_audit as audit
from scripts import approval_policy as policy


class DeliveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.source = self.workspace / "app.py"
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.session = "delivery-test"

    def start(self, mode="strict", files=None, task_id=""):
        return audit.begin(self.home, self.session, self.workspace,
                           "修复输入为空时的错误", mode=mode,
                           files=files or [str(self.source)], task_id=task_id)

    def complete(self, independent=True, include_requirements=True,
                 include_behavior=True):
        if include_requirements:
            state = audit.read_state(self.home, self.session)
            requirement_id = (state.get("canonical_requirements") or [{"id": "R1"}])[0]["id"]
            audit.record_check(self.home, self.session, "requirements", True,
                               [{"requirement_id": requirement_id,
                                 "item": "空输入返回明确错误", "status": "done",
                                 "evidence": "app.py:validate"}])
        audit.record_check(self.home, self.session, "diff", True, "读取完整 git diff；仅 app.py，无越界行为")
        if include_behavior:
            audit.record_check(self.home, self.session, "behavior", True,
                               {"command": "python -m unittest tests.test_app",
                                "exit_code": 0, "tests_ran": 3,
                                "executed": True, "kind": "test"})
        audit.record_check(self.home, self.session, "self_review", True,
                           "检查异常路径、相邻调用与目标外影响；未发现问题")
        if independent:
            from scripts import fusion_assist
            fusion_assist.record_result(self.home, self.session, {
                "status": "success", "answer": "未发现问题", "session_id": "fusion-real"})
            audit.record_check(self.home, self.session, "independent_review", True,
                               {"provider": "fusion", "status": "success", "session_id": "fusion-real", "findings": []})

    def test_strict_requires_all_evidence_and_fresh_workspace(self):
        self.start()
        reason = audit.completion_block(self.home, self.session, "strict", "已修复")
        self.assertIn("需求", reason)
        self.complete()
        self.assertIsNone(audit.completion_block(self.home, self.session, "strict", "已修复"))
        self.source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        audit.begin(self.home, self.session, self.workspace, "修复输入为空时的错误",
                    mode="strict", files=[str(self.source)], task_id="delivery-test")
        self.assertIn("behavior", audit.required_checks(self.home, self.session))

    def test_completion_claim_excludes_questions_explanations_and_examples(self):
        self.assertFalse(audit.completion_claim(
            "agent-audit 是干嘛的？它用于检查交付是否真的完成了，而不是只看一句测试通过。"
        ))
        self.assertFalse(audit.completion_claim(
            "完成声明规则会识别“已修复”“验证通过”等措辞吗？"
        ))
        self.assertFalse(audit.completion_claim(
            "修复完成后还需要运行真实测试，并把证据逐项登记。"
        ))
        self.assertFalse(audit.completion_claim(
            "输入 /agent-audit 只会显示当前待办，不会自动检查或自动通过。"
            "缺证据、检查失败或审计后又有修改，就不能汇报“全部完成”。"
        ))
        self.assertFalse(audit.completion_claim(
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
        ))
        self.assertTrue(audit.completion_claim("已修复，相关 18 项测试全部通过。"))
        self.assertTrue(audit.completion_claim("修改已完成，行为探针验证通过。"))
        self.assertFalse(audit.completion_claim(
            "代码修改及 72/72 测试通过已核实；AgentRelay 视觉验收仍未完成。"
        ))
        self.assertFalse(audit.completion_claim(
            "左右留白调整截图已实际查看，但 AgentRelay 未记录有效的视觉验收结果，"
            "因此不标记其验收通过。"
        ))
        self.assertFalse(audit.completion_claim(
            "已停止，不再声明完成或视觉验收通过，等待后续指示。"
        ))

    def test_new_problem_resets_old_delivery_evidence(self):
        second = self.workspace / "second.py"
        second.write_text("VALUE = 2\n", encoding="utf-8")
        audit.begin(self.home, self.session, self.workspace, "修复第一个问题",
                    mode="standard", files=[str(self.source)], task_id="problem-one")
        audit.record_check(self.home, self.session, "diff", True,
                           "读取第一个问题的实际 diff，只有 app.py 的变化")
        audit.begin(self.home, self.session, self.workspace, "实现第二个功能",
                    mode="standard", files=[str(second)], task_id="problem-two")
        state = audit.read_state(self.home, self.session)
        self.assertEqual(state["task_id"], "problem-two")
        self.assertEqual(state["request"], "实现第二个功能")
        self.assertEqual(state["files"], [str(second)])
        self.assertEqual(state["checks"], {})

    def test_legacy_evidence_without_task_id_is_not_inherited_by_new_task(self):
        second = self.workspace / "second.py"
        second.write_text("VALUE = 2\n", encoding="utf-8")
        audit.begin(self.home, self.session, self.workspace, "修复旧任务",
                    mode="standard", files=[str(self.source)])
        audit.record_check(self.home, self.session, "diff", True,
                           "读取旧任务的实际 diff，只有 app.py 的变化")

        audit.begin(self.home, self.session, self.workspace, "解释新问题",
                    mode="standard", files=[str(second)], task_id="problem-two")
        state = audit.read_state(self.home, self.session)
        self.assertEqual(state["task_id"], "problem-two")
        self.assertEqual(state["request"], "解释新问题")
        self.assertEqual(state["files"], [str(second)])
        self.assertEqual(state["checks"], {})

    def test_standard_small_change_is_lightweight_but_complex_escalates(self):
        self.start("standard")
        self.assertEqual(audit.required_checks(self.home, self.session),
                         {"requirements", "diff", "behavior", "self_review"})
        self.start("standard", files=[str(self.source), str(self.workspace / "api.py")])
        self.assertIn("independent_review", audit.required_checks(self.home, self.session))

    def test_relaxed_small_task_skips_audit_but_large_scope_escalates(self):
        self.start("relaxed")
        self.assertEqual(audit.required_checks(self.home, self.session), set())
        self.assertIsNone(audit.completion_block(self.home, self.session, "relaxed", "已修复"))
        self.start("relaxed", files=[str(self.source), str(self.workspace / "other.py")])
        self.assertEqual(
            audit.required_checks(self.home, self.session),
            {"requirements", "diff", "behavior", "self_review", "independent_review"},
        )

    def test_single_style_change_is_lightweight_not_full_audit(self):
        page = self.workspace / "page.css"
        page.write_text(".button { border-radius: 50%; }", encoding="utf-8")
        audit.begin(self.home, self.session, self.workspace, "把圆形按钮改成方块，只改 border-radius",
                    mode="standard", files=[str(page)], style_change=True)
        self.assertEqual(
            audit.required_checks(self.home, self.session),
            {"visual"},
        )
        self.assertNotIn("independent_review", audit.required_checks(self.home, self.session))

    def test_relaxed_recorded_behavior_still_rejects_zero_tests(self):
        self.start("relaxed")
        audit.record_check(self.home, self.session, "behavior", True,
                           {"command": "python -m unittest", "exit_code": 0, "tests_ran": 0, "executed": True, "kind": "test"})
        self.complete(independent=False)
        audit.record_check(self.home, self.session, "behavior", True,
                           {"command": "python -m unittest", "exit_code": 0, "tests_ran": 0, "executed": True, "kind": "test"})
        self.assertIn("没有实际执行测试", audit.completion_block(self.home, self.session, "relaxed", "已修复"))
    def test_real_behavior_execution_and_zero_test_failure(self):
        tests = self.workspace / "tests"
        tests.mkdir()
        (tests / "test_app.py").write_text(
            "import unittest\nimport app\nclass AppTest(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(app.VALUE, 1)\n",
            encoding="utf-8",
        )
        self.start("relaxed")
        result = audit.run_behavior(self.home, self.session,
                                    f'"{sys.executable}" -m unittest discover -s tests',
                                    timeout=30)
        self.assertEqual(result["checks"]["behavior"]["evidence"]["exit_code"], 0)
        self.assertGreater(result["checks"]["behavior"]["evidence"]["tests_ran"], 0)
        empty = self.workspace / "empty-tests"
        empty.mkdir()
        result = audit.run_behavior(self.home, self.session,
                                    f'"{sys.executable}" -m unittest discover -s empty-tests')
        self.assertFalse(result["checks"]["behavior"]["passed"])
        self.assertEqual(result["checks"]["behavior"]["evidence"]["tests_ran"], 0)
        probe = audit.run_behavior(self.home, self.session,
                                   f'"{sys.executable}" -c "import app; print(app.VALUE)"', kind="probe")
        self.assertTrue(probe["checks"]["behavior"]["passed"])

    def test_red_green_evidence_binds_baseline_and_candidate_fingerprints(self):
        tests = self.workspace / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_logic.py").write_text(
            "import unittest\nimport app\n"
            "class LogicTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(app.VALUE, 2)\n",
            encoding="utf-8",
        )
        command = f'"{sys.executable}" -m unittest discover -s tests'
        self.start("strict", files=[str(self.source)], task_id="delivery-test")
        state = audit.read_state(self.home, self.session)
        state.update(work_profile="strict", logic_change=True)
        audit.write_state(self.home, self.session, state)
        baseline = state["fingerprint"]

        red = audit.run_behavior(self.home, self.session, command, phase="red")
        self.assertEqual(red["red_behavior"]["exit_code"], 1)
        self.assertEqual(red["red_behavior"]["fingerprint"], baseline)

        self.source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        green = audit.run_behavior(self.home, self.session, command, phase="green")
        behavior = green["checks"]["behavior"]
        self.assertTrue(behavior["passed"])
        self.assertEqual(behavior["evidence"]["exit_code"], 0)
        self.assertGreater(behavior["evidence"]["tests_ran"], 0)

        stored = audit.read_state(self.home, self.session)
        self.assertEqual(stored["red_behavior"]["fingerprint"], baseline)
        self.assertEqual(
            stored["checks"]["behavior"]["fingerprint"],
            stored["candidate_fingerprint"],
        )
        self.assertNotEqual(stored["candidate_fingerprint"], baseline)

    def test_red_green_requires_real_failure_before_change_and_pass_after(self):
        tests = self.workspace / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_logic.py").write_text(
            "import unittest\nimport app\n"
            "class LogicTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(app.VALUE, 2)\n",
            encoding="utf-8",
        )
        command = f'"{sys.executable}" -m unittest discover -s tests'
        self.start("strict", files=[str(self.source)], task_id="delivery-test")
        state = audit.read_state(self.home, self.session)
        state.update(work_profile="strict", logic_change=True)
        audit.write_state(self.home, self.session, state)
        self.assertIn("RED", audit.completion_block(
            self.home, self.session, "strict", "已修复",
        ))

        audit.run_behavior(self.home, self.session, command, phase="red")
        self.source.write_text("VALUE = 2  # fixed\n", encoding="utf-8")
        audit.begin(
            self.home, self.session, self.workspace, "修复输入为空时的错误",
            mode="strict", files=[str(self.source)], task_id="delivery-test",
        )
        audit.run_behavior(self.home, self.session, command, phase="green")
        self.complete(independent=True, include_behavior=False)
        self.assertIsNone(audit.completion_block(
            self.home, self.session, "strict", "已修复",
        ))

    def test_review_freezes_candidate_and_later_edit_invalidates_it(self):
        self.start("strict")
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        audit.begin(
            self.home, self.session, self.workspace, "修复输入为空时的错误",
            mode="strict", files=[str(self.source)], task_id="delivery-test",
        )
        audit.record_check(
            self.home, self.session, "self_review", True,
            "审查异常路径、相邻调用和目标外影响；未发现问题",
        )
        state = audit.read_state(self.home, self.session)
        self.assertEqual(state["review_fingerprint"], state["candidate_fingerprint"])
        self.assertEqual(
            state["checks"]["self_review"]["fingerprint"],
            state["review_fingerprint"],
        )

        self.source.write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(audit.status(self.home, self.session)["status"], "stale")
        self.assertIn("过期", audit.completion_block(
            self.home, self.session, "strict", "已修复",
        ))

    def test_independent_success_requires_actual_fusion_result(self):
        self.start("strict")
        with self.assertRaisesRegex(ValueError, "真实 Fusion"):
            audit.record_check(self.home, self.session, "independent_review", True,
                               {"provider": "fusion", "status": "success", "findings": []})

    def test_behavior_probe_reuses_dangerous_command_gate(self):
        self.start("standard")
        with self.assertRaisesRegex(ValueError, "危险"):
            audit.run_behavior(self.home, self.session, "rm -rf build", kind="probe")
        with self.assertRaisesRegex(ValueError, "危险"):
            audit.run_behavior(self.home, self.session, "git push --force origin main", kind="probe")

    def test_independent_review_entry_uses_real_delegate_and_records_fallback(self):
        self.start("strict")
        from unittest.mock import patch
        with patch("scripts.fusion_assist.delegate", return_value={"status": "rejected", "error": "FUSION_PROVIDER_NOT_CONFIGURED"}) as delegate:
            result = audit.independent_review(self.home, self.session)
        self.assertEqual(result["checks"]["independent_review"]["passed"], False)
        self.assertEqual(result["checks"]["independent_review"]["evidence"]["status"], "unavailable")
        delegate.assert_called_once()

    def test_requirement_gaps_and_unverifiable_items_block_clean_completion(self):
        self.start("standard")
        for status in ("partial", "not_done", "unverifiable"):
            state = audit.read_state(self.home, self.session)
            requirement_id = state["canonical_requirements"][0]["id"]
            audit.record_check(self.home, self.session, "requirements", False,
                               [{"requirement_id": requirement_id,
                                 "item": "未覆盖需求", "status": status,
                                 "evidence": "缺少当前版本证据"}])
            self.complete(independent=False, include_requirements=False)
            with self.assertRaisesRegex(ValueError, "复检"):
                audit.record_check(self.home, self.session, "requirements", True,
                                   [{"requirement_id": requirement_id,
                                     "item": "仍未覆盖", "status": "done",
                                     "evidence": "没有新证据"}])
            self.assertIn("requirements", audit.completion_block(self.home, self.session, "standard", "已修复"))
        with self.assertRaisesRegex(ValueError, "复检"):
            audit.record_check(self.home, self.session, "requirements", True,
                               [{"requirement_id": requirement_id,
                                 "item": "未验证项", "status": "unverifiable",
                                 "evidence": "外部状态未检查"}])

    def test_fusion_timeout_is_unavailable_not_a_false_success(self):
        self.start("strict")
        from unittest.mock import patch
        with patch("scripts.fusion_assist.delegate", side_effect=TimeoutError("read timed out")):
            result = audit.independent_review(self.home, self.session)
        self.assertFalse(result["checks"]["independent_review"]["passed"])
        self.assertEqual(result["checks"]["independent_review"]["evidence"]["status"], "unavailable")
        self.assertIn("timeout", result["checks"]["independent_review"]["evidence"]["reason"].lower())

    def test_fusion_unavailable_requires_separate_local_review_completion(self):
        self.start("strict")
        self.complete(independent=False)
        audit.record_check(self.home, self.session, "independent_review", False,
                           {"status": "unavailable", "reason": "FUSION_PROVIDER_NOT_CONFIGURED",
                            "fallback": "main_agent_local_adversarial_review_required"})
        self.assertIn("independent_review",
                      audit.completion_block(self.home, self.session, "strict", "已修复"))
        with self.assertRaisesRegex(ValueError, "不能把独立审计标记为通过"):
            audit.record_check(self.home, self.session, "independent_review", True,
                               {"status": "unavailable", "reason": "FUSION_PROVIDER_NOT_CONFIGURED",
                                "fallback": "main_agent_local_adversarial_review_required"})
        audit.record_check(self.home, self.session, "independent_review", True,
                           {"status": "unavailable", "reason": "FUSION_PROVIDER_NOT_CONFIGURED",
                            "fallback": "main_agent_completed_local_adversarial_review"})
        self.assertIsNone(audit.completion_block(self.home, self.session, "strict", "已修复"))

    def test_false_or_unread_evidence_is_rejected(self):
        self.start("strict")
        with self.assertRaises(ValueError):
            audit.record_check(self.home, self.session, "requirements", True, [])
        with self.assertRaises(ValueError):
            audit.record_check(self.home, self.session, "diff", True, "")
        audit.record_check(self.home, self.session, "behavior", False,
                           {"command": "pytest", "exit_code": 1, "tests_ran": 2})
        self.assertIn("行为", audit.completion_block(self.home, self.session, "strict", "已修复"))

    def test_non_visual_review_remains_user_review_required(self):
        self.start("standard")
        self.complete(independent=False)
        audit.record_check(self.home, self.session, "visual", True,
                           {"status": "user_review_required", "reason": "无视觉 Provider"})
        self.assertIn("用户复查", audit.completion_block(self.home, self.session, "standard", "页面已完成"))

    def test_page_files_require_visual_evidence_even_in_standard(self):
        page = self.workspace / "page.html"
        page.write_text("<button>save</button>", encoding="utf-8")
        self.start("standard", [str(page)])
        self.assertIn("visual", audit.required_checks(self.home, self.session))

    def test_cli_status_and_record_are_structured(self):
        self.start("standard")
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/delivery_audit.py"),
                                 "status", "--home", str(self.home), "--session", self.session],
                                text=True, encoding="utf-8", errors="replace",
                                capture_output=True, check=True)
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "pending")
        self.assertIn("behavior", data["pending_checks"])

    def test_begin_does_not_reset_unchanged_evidence_and_untracked_change_invalidates(self):
        self.start("standard")
        self.complete(independent=False)
        before = audit.read_state(self.home, self.session)["checks"]
        audit.begin(self.home, self.session, self.workspace, "修改常量", mode="standard", files=[str(self.source)])
        self.assertEqual(audit.read_state(self.home, self.session)["checks"], before)
        other = self.workspace / "unreported.py"
        other.write_text("VALUE = 2\n", encoding="utf-8")
        self.assertEqual(audit.status(self.home, self.session)["status"], "stale")
        with self.assertRaisesRegex(ValueError, "过期"):
            audit.record_check(self.home, self.session, "diff", True, "重新读取当前版本差异")

    def test_concurrent_evidence_updates_are_not_lost(self):
        self.start("strict")
        errors = []

        def record(name, evidence):
            try:
                audit.record_check(
                    self.home, self.session, name, True, evidence, recheck=True
                )
            except Exception as exc:  # pragma: no cover - assertion reports details
                errors.append(exc)

        checks = [
            ("requirements", [{
                "requirement_id": "R1",
                "item": "空输入返回明确错误",
                "status": "done",
                "evidence": "app.py:validate",
            }]),
            ("diff", "读取完整 git diff；仅 app.py，无越界行为"),
            ("behavior", {
                "command": "python -m unittest tests.test_app",
                "exit_code": 0,
                "tests_ran": 3,
                "executed": True,
                "kind": "test",
            }),
            ("self_review", "检查异常路径、相邻调用与目标外影响；未发现问题"),
        ]
        threads = [threading.Thread(target=record, args=item) for item in checks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        stored = audit.read_state(self.home, self.session)
        self.assertEqual(set(stored["checks"]), {name for name, _ in checks})

    def test_canonical_requirements_require_complete_coverage_and_valid_statuses(self):
        self.start("standard")
        audit.set_requirements(
            self.home, self.session,
            ["第一项必须完成", "第二项必须完成"],
        )
        with self.assertRaisesRegex(ValueError, "完整覆盖"):
            audit.record_check(self.home, self.session, "requirements", True, [{
                "requirement_id": "R1", "item": "第一项",
                "status": "done", "evidence": "真实检查 R1",
            }])
        with self.assertRaisesRegex(ValueError, "不存在"):
            audit.record_check(self.home, self.session, "requirements", False, [{
                "requirement_id": "R9", "item": "范围外",
                "status": "not_done", "evidence": "不存在",
            }])
        audit.record_check(self.home, self.session, "requirements", True, [
            {"requirement_id": "R1", "item": "第一项",
             "status": "done", "evidence": "真实检查 R1"},
            {"requirement_id": "R2", "item": "第二项",
             "status": "changed", "evidence": "用户同意改为真实检查 R2"},
        ])
        coverage = audit.status(self.home, self.session)["requirement_coverage"]
        self.assertEqual(
            [(item["id"], item["status"]) for item in coverage],
            [("R1", "DONE"), ("R2", "CHANGED")],
        )

    def test_canonical_requirements_cannot_be_redefined_after_write(self):
        self.start("standard", task_id="delivery-test")
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        audit.begin(
            self.home, self.session, self.workspace, "修复输入为空时的错误",
            mode="standard", files=[str(self.source)], task_id="delivery-test",
        )
        with self.assertRaisesRegex(ValueError, "首次写入后"):
            audit.set_requirements(self.home, self.session, ["缩小后的范围"])

    def test_run_behavior_cli_supports_explicit_red_phase(self):
        tests = self.workspace / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_logic.py").write_text(
            "import unittest\nimport app\n"
            "class LogicTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(app.VALUE, 2)\n",
            encoding="utf-8",
        )
        self.start("strict", task_id="delivery-test")
        state = audit.read_state(self.home, self.session)
        state.update(work_profile="strict", logic_change=True)
        audit.write_state(self.home, self.session, state)
        result = subprocess.run([
            sys.executable, str(PROJECT_ROOT / "scripts/delivery_audit.py"),
            "run-behavior", "--home", str(self.home), "--session", self.session,
            "--cmd", f'"{sys.executable}" -m unittest discover -s tests',
            "--phase", "red",
        ], text=True, encoding="utf-8", errors="replace", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["red_behavior"]["phase"], "red")
        self.assertEqual(data["red_behavior"]["exit_code"], 1)

    def test_task_notification_cannot_replace_original_request(self):
        audit.begin(self.home, self.session, self.workspace, "实现原始功能",
                    mode="standard", files=[str(self.source)], task_id="original-task")
        audit.begin(self.home, self.session, self.workspace,
                    "<task-notification>background command failed</task-notification>",
                    mode="standard", files=[str(self.source)], task_id="original-task")
        self.assertEqual(audit.read_state(self.home, self.session)["request"], "实现原始功能")

    def test_closed_problem_stops_audit_reminders_without_deleting_history(self):
        self.start("standard")
        audit.close(self.home, self.session)
        self.assertIsNone(audit.completion_block(self.home, self.session, "standard", "已修复"))
        self.assertEqual(audit.read_state(self.home, self.session)["status"], "closed")
        self.assertTrue(audit.state_path(self.home, self.session).is_file())

    def test_legacy_audit_without_active_edit_does_not_restart(self):
        self.start("standard")
        state = audit.read_state(self.home, self.session)
        state.pop("active_edit", None)
        audit.write_state(self.home, self.session, state)
        self.assertIsNone(audit.completion_block(self.home, self.session, "standard", "已修复"))

    def test_passed_audit_closes_active_edit(self):
        self.start("standard")
        self.complete(independent=False)
        self.assertIsNone(audit.completion_block(self.home, self.session, "standard", "已修复"))
        self.assertFalse(audit.read_state(self.home, self.session)["active_edit"])

    def test_policy_menu_binds_audit_depth(self):
        menu = policy.render_menu(self.home)
        self.assertIn("完整交付审计", menu)
        self.assertIn("轻量检查", menu)
        self.assertIn("复杂/共享/高风险自动升级", menu)
        self.assertIn("安全底线", menu)


if __name__ == "__main__":
    unittest.main()
