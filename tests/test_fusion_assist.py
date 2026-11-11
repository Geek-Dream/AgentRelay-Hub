import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import fusion_assist as assist
from scripts import fusion_trace


class FusionAssistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.session = "fusion-assist-test"

    def test_suitable_tasks_and_child_sessions(self):
        self.assertTrue(assist.suitable("请独立复核这个跨文件的修改", {}))
        self.assertTrue(assist.suitable("运行全量测试并汇总结果", {}))
        self.assertFalse(assist.suitable("你好", {}))
        self.assertFalse(assist.suitable("审查代码", {"AGENTRELAY_COMMANDER_CHILD": "1"}))
        self.assertFalse(assist.suitable("审查代码", {"AGENTRELAY_FUSION_ASSIST": "off"}))

    def test_reminder_is_once_and_does_not_fake_delegate(self):
        first = assist.reminder(self.home, self.session, "独立复核跨文件实现", stage="prompt")
        self.assertIn("实际 delegate", first)
        self.assertEqual(assist.reminder(self.home, self.session, "独立复核跨文件实现", stage="prompt"), "")
        self.assertEqual(assist.read_state(self.home, self.session)["status"], "reminded")

    def test_failure_notice_is_bounded_and_main_agent_continues(self):
        for index in range(4):
            state = assist.record_result(self.home, self.session, {"status": "failed", "error": f"API_EXPIRED_{index}"})
            self.assertEqual(state["failure_notices"], min(index + 1, 1))
            self.assertEqual(state["should_notify"], index < 1)
        self.assertEqual(assist.read_state(self.home, self.session)["should_notify"], False)

    def test_unconfigured_provider_is_silent_and_does_not_count_failure(self):
        state = assist.record_result(self.home, self.session, {
            "status": "rejected", "error": "FUSION_PROVIDER_NOT_CONFIGURED"})
        self.assertEqual(state["failure_notices"], 0)
        self.assertFalse(state["should_notify"])

    def test_success_records_real_evidence(self):
        state = assist.record_result(self.home, self.session, {"status": "success", "answer": "independent review"})
        self.assertEqual(state["result"]["answer"], "independent review")
        self.assertIn("独立验收", state["notice"])

    def test_same_failure_is_only_notified_once_and_has_actionable_state(self):
        result = {"status": "failed", "error": "PROVIDER_TIMEOUT", "message": "read timeout"}
        first = assist.record_result(self.home, self.session, result)
        second = assist.record_result(self.home, self.session, result)
        self.assertTrue(first["should_notify"])
        self.assertFalse(second["should_notify"])
        self.assertEqual(second["display_state"], "retryable")
        self.assertIn("可重试", second["notice"])
        self.assertEqual(second["failure_notices"], 1)

    def test_provider_timeout_is_classified_without_dumping_subprocess_command(self):
        script = self.home / "skills" / "agent-fusion" / "scripts" / "agent_fusion.py"
        script.parent.mkdir(parents=True)
        script.write_text("pass\n", encoding="utf-8")
        request_id = assist.request_identity(
            self.session,
            "复核测试",
            str(self.home),
        )
        trace_path = self.home / "trace" / "fusion.jsonl"
        trace_environ = {
            fusion_trace.TRACE_PATH_ENV: str(trace_path),
            fusion_trace.TRACE_ID_ENV: request_id,
            fusion_trace.TRACE_SESSION_ENV: self.session,
        }
        fusion_trace.emit(
            "provider.lease.wait",
            environ=trace_environ,
            stage="provider_queue_wait",
        )
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["agent_fusion.py", "delegate"], 120),
        ):
            result = assist.delegate(
                self.home,
                self.session,
                "复核测试",
                workspace=str(self.home),
                environ=trace_environ,
            )

        self.assertEqual(result["error"], "FUSION_PROVIDER_TIMEOUT")
        self.assertIn("120 秒", result["message"])
        self.assertIn("provider_queue_wait", result["message"])
        self.assertNotIn("agent_fusion.py", result["message"])
        self.assertEqual(result["trace_path"], str(trace_path))
        self.assertEqual(result["last_stage"], "provider_queue_wait")
        self.assertTrue(any(
            item["stage"] == "provider_queue_wait"
            for item in result["trace_tail"]
        ))
        self.assertTrue(any(
            item["event"] == "assist.invoke.timeout"
            for item in result["trace_tail"]
        ))
        stored = trace_path.read_text(encoding="utf-8")
        self.assertNotIn("复核测试", stored)
        state = assist.read_state(self.home, self.session)
        self.assertTrue(state["provider_circuit_open"])
        self.assertEqual(state["provider_circuit_reason"], "FUSION_PROVIDER_TIMEOUT")

    def test_provider_failure_returns_last_stage_and_trace_tail(self):
        script = self.home / "skills" / "agent-fusion" / "scripts" / "agent_fusion.py"
        script.parent.mkdir(parents=True)
        script.write_text("pass\n", encoding="utf-8")
        request_id = assist.request_identity(
            self.session,
            "复核测试",
            str(self.home),
        )
        trace_path = self.home / "trace" / "fusion.jsonl"
        trace_environ = {
            fusion_trace.TRACE_PATH_ENV: str(trace_path),
            fusion_trace.TRACE_ID_ENV: request_id,
            fusion_trace.TRACE_SESSION_ENV: self.session,
        }
        fusion_trace.emit(
            "provider.request.error",
            environ=trace_environ,
            stage="provider_error",
            error_type="HTTPError",
            error="HTTP Error 502: Bad Gateway",
        )
        fusion_trace.emit(
            "provider.request.finished",
            environ=trace_environ,
            stage="provider_call_finished",
        )
        fusion_trace.emit(
            "runner.model.error",
            environ=trace_environ,
            stage="model_error",
            error="HTTP Error 502: Bad Gateway",
        )
        fusion_trace.emit(
            "runner.error",
            environ=trace_environ,
            stage="runner_error",
        )
        fusion_trace.emit(
            "runner.finished",
            environ=trace_environ,
            stage="runner_finished",
        )
        fusion_trace.emit(
            "fusion.delegate.error",
            environ=trace_environ,
            stage="unhandled_error",
        )
        completed = subprocess.CompletedProcess(
            args=["agent_fusion.py", "delegate"],
            returncode=2,
            stdout=(
                'AGENT_FUSION_RESULT={"status":"failed",'
                '"error":"FUSION_PROVIDER_CALL_FAILED",'
                '"message":"HTTP Error 502: Bad Gateway"}\n'
            ),
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=completed):
            result = assist.delegate(
                self.home,
                self.session,
                "复核测试",
                workspace=str(self.home),
                environ=trace_environ,
            )

        self.assertEqual(result["error"], "FUSION_PROVIDER_CALL_FAILED")
        self.assertEqual(result["last_stage"], "provider_error")
        self.assertTrue(any(
            item.get("error") == "HTTP Error 502: Bad Gateway"
            for item in result["trace_tail"]
        ))

    def test_circuit_breaker_blocks_new_automatic_calls_until_explicit_retry(self):
        failed = {"status": "failed", "error": "FUSION_PROVIDER_TIMEOUT", "message": "timed out"}
        with mock.patch.object(assist, "_invoke", return_value=failed):
            assist.delegate(self.home, self.session, "复核第一项", workspace=str(self.home))

        with mock.patch.object(assist, "_invoke") as invoke:
            skipped = assist.delegate(
                self.home,
                self.session,
                "复核另一项",
                workspace=str(self.home),
            )
        self.assertEqual(skipped["error"], "FUSION_RECOVERY_REQUIRED")
        invoke.assert_not_called()

        with mock.patch.object(
            assist,
            "_invoke",
            return_value={"status": "success", "answer": "recovered"},
        ) as invoke:
            recovered = assist.delegate(
                self.home,
                self.session,
                "复核另一项",
                workspace=str(self.home),
                retry=True,
            )
        self.assertEqual(recovered["status"], "success")
        invoke.assert_called_once()
        state = assist.read_state(self.home, self.session)
        self.assertFalse(state.get("provider_circuit_open", False))

    def test_delegate_reuses_success_for_same_request_without_second_provider_call(self):
        result = {"status": "success", "answer": "review", "session_id": "fusion-real"}
        with mock.patch.object(assist, "_invoke", return_value=result) as invoke:
            first = assist.delegate(self.home, self.session, "复核测试", workspace=str(self.home))
            second = assist.delegate(self.home, self.session, "复核测试", workspace=str(self.home))
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second["deduplicated"])
        invoke.assert_called_once()
        self.assertEqual(assist.read_state(self.home, self.session)["delivery_state"], "completed")

    def test_interrupted_request_does_not_automatically_call_provider_again(self):
        state = assist.begin_request(self.home, self.session, "复核测试", workspace=str(self.home))
        with mock.patch.object(assist, "_invoke") as invoke:
            recovered = assist.delegate(self.home, self.session, "复核测试", workspace=str(self.home))
        self.assertEqual(recovered["error"], "FUSION_RETRY_REQUIRED")
        self.assertEqual(recovered["request_id"], state["request_id"])
        invoke.assert_not_called()
        self.assertEqual(assist.read_state(self.home, self.session)["delivery_state"], "interrupted")

    def test_explicit_retry_uses_same_request_and_replaces_failure(self):
        interrupted = assist.begin_request(self.home, self.session, "复核测试", workspace=str(self.home))
        with mock.patch.object(assist, "_invoke", return_value={"status": "success", "answer": "done"}) as invoke:
            result = assist.delegate(self.home, self.session, "复核测试", workspace=str(self.home), retry=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["request_id"], interrupted["request_id"])
        invoke.assert_called_once()

    def test_actual_delegate_uses_existing_cli_and_records_result(self):
        script = self.home / "skills" / "agent-fusion" / "scripts" / "agent_fusion.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('AGENT_FUSION_RESULT={\"status\":\"success\",\"answer\":\"review\"}')\n", encoding="utf-8")
        fake_python = self.home / "agentrelay-env" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text("#!/bin/sh\nexec /usr/bin/env python3 \"$@\"\n", encoding="utf-8")
        fake_python.chmod(0o755)
        with mock.patch.dict(os.environ, {"AGENTRELAY_PYTHON": str(fake_python)}, clear=False):
            result = assist.delegate(self.home, self.session, "复核测试", workspace=str(self.home))
        self.assertEqual(result["status"], "success")
        self.assertEqual(assist.read_state(self.home, self.session)["result"]["answer"], "review")

    def test_missing_fusion_script_fails_without_blocking(self):
        result = assist.delegate(self.home, self.session, "复核测试", workspace=str(self.home))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(assist.read_state(self.home, self.session)["failure_notices"], 1)


if __name__ == "__main__":
    unittest.main()
