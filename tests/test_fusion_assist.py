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
            state = assist.record_result(self.home, self.session, {"status": "failed", "error": "API_EXPIRED"})
            self.assertEqual(state["failure_notices"], min(index + 1, 3))
            self.assertEqual(state["should_notify"], index < 3)
        self.assertEqual(assist.read_state(self.home, self.session)["notice"], "")

    def test_unconfigured_provider_is_silent_and_does_not_count_failure(self):
        state = assist.record_result(self.home, self.session, {
            "status": "rejected", "error": "FUSION_PROVIDER_NOT_CONFIGURED"})
        self.assertEqual(state["failure_notices"], 0)
        self.assertFalse(state["should_notify"])

    def test_success_records_real_evidence(self):
        state = assist.record_result(self.home, self.session, {"status": "success", "answer": "independent review"})
        self.assertEqual(state["result"]["answer"], "independent review")
        self.assertIn("独立验收", state["notice"])

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
