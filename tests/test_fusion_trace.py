import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import fusion_trace  # noqa: E402


class FusionTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentrelay-fusion-trace-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "logs" / "fusion-trace.jsonl"
        self.environ = {
            fusion_trace.TRACE_PATH_ENV: str(self.path),
            fusion_trace.TRACE_ID_ENV: "trace-1",
            fusion_trace.TRACE_SESSION_ENV: "session-1",
        }

    def test_emit_writes_bounded_jsonl_and_filters_by_trace_id(self):
        record = fusion_trace.emit(
            "runner.model.start",
            environ=self.environ,
            stage="model_request",
            detail="x" * (fusion_trace.MAX_FIELD_LENGTH + 100),
        )
        fusion_trace.emit(
            "other.start",
            environ={**self.environ, fusion_trace.TRACE_ID_ENV: "trace-2"},
            stage="provider_request",
        )

        self.assertIsNotNone(record)
        self.assertEqual(record["trace_id"], "trace-1")
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["stage"], "model_request")
        self.assertEqual(
            len(record["detail"]),
            fusion_trace.MAX_FIELD_LENGTH,
        )
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

        events = fusion_trace.recent_events(self.path, "trace-1", limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "runner.model.start")
        self.assertEqual(fusion_trace.last_stage(events), "model_request")

    def test_trace_does_not_write_without_explicit_path(self):
        self.assertIsNone(
            fusion_trace.emit(
                "runner.model.start",
                environ={},
                stage="model_request",
            )
        )
        self.assertFalse(self.path.exists())

    def test_tail_reader_is_bounded_and_ignores_partial_records(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            '{"event":"old","trace_id":"trace-1","stage":"provider_request"}\n'
            '{"event":"new","trace_id":"trace-1","stage":"tool_read_file"}\n'
            '{"broken":',
            encoding="utf-8",
        )
        events = fusion_trace.recent_events(
            self.path,
            "trace-1",
            limit=1,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "tool_read_file")
        self.assertEqual(fusion_trace.last_stage(events), "tool_read_file")

    def test_last_stage_falls_back_to_event_name(self):
        self.assertEqual(
            fusion_trace.last_stage([{"event": "assist.invoke.timeout"}]),
            "assist.invoke.timeout",
        )
        self.assertEqual(
            fusion_trace.last_stage(
                [
                    {"event": "provider.lease.wait", "stage": "provider_queue_wait"},
                    {"event": "assist.invoke.start", "stage": "subprocess_start"},
                ],
                exclude_events={"assist.invoke.start"},
            ),
            "provider_queue_wait",
        )
        self.assertEqual(fusion_trace.recent_events(self.path), [])

    def test_diagnostic_stage_prefers_underlying_provider_error(self):
        events = [
            {"event": "runner.model.start", "stage": "model_request"},
            {"event": "provider.request.start", "stage": "provider_request"},
            {
                "event": "provider.request.error",
                "stage": "provider_error",
                "error": "HTTP Error 502: Bad Gateway",
            },
            {"event": "provider.request.finished", "stage": "provider_call_finished"},
            {"event": "runner.model.error", "stage": "model_error"},
            {"event": "runner.error", "stage": "runner_error"},
            {"event": "runner.finished", "stage": "runner_finished"},
            {"event": "fusion.delegate.error", "stage": "unhandled_error"},
        ]

        self.assertEqual(
            fusion_trace.diagnostic_stage(
                events,
                exclude_events={"runner.error", "fusion.delegate.error"},
            ),
            "provider_error",
        )

    def test_diagnostic_stage_keeps_hanging_tool_stage_during_outer_timeout(self):
        events = [
            {"event": "runner.model.start", "stage": "model_request"},
            {"event": "runner.tool.start", "stage": "tool_run_command"},
        ]

        self.assertEqual(
            fusion_trace.diagnostic_stage(events),
            "tool_run_command",
        )


if __name__ == "__main__":
    unittest.main()
