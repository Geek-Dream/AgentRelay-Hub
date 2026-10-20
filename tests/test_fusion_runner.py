import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agent_fusion  # noqa: E402
import config_manager  # noqa: E402
import fusion_mode  # noqa: E402
import fusion_runner  # noqa: E402


def chat_response(content="", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": message}]}


def tool_call(name, arguments, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


class FusionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentrelay-fusion-runner-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.lock_dir = self.home / "locks"
        self.old_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)
        config_manager.save_config({
            "fusion": {
                "enabled": True,
                "provider_id": "local:test",
                "timeout": 5,
                "tool_mode": "readonly",
                "max_steps": 8,
            },
            "local_providers": [{
                "id": "test",
                "endpoint": "http://127.0.0.1:9/v1",
                "model": "fusion-test",
            }],
        }, self.home)

    def tearDown(self):
        if self.old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.old_codex_home
        self.temp.cleanup()

    def run_sidekick(self, model, **overrides):
        options = {
            "prompt": "delegated task",
            "context": "",
            "workspace": self.workspace,
            "session_id": "session-a",
            "provider_ref": "local:test",
            "provider_kind": "local",
            "tool_mode": "readonly",
            "max_steps": 8,
            "timeout": 5,
            "home": self.home,
            "lock_dir": self.lock_dir,
            "environ": {},
            "call_model": model,
        }
        options.update(overrides)
        return fusion_runner.run_sidekick(**options)

    def test_readonly_tools_allow_inspection_only(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            "first line\nneedle here\n",
            encoding="utf-8",
        )

        read = fusion_runner.execute_tool(
            "read_file",
            {"path": "src/app.py", "start_line": 2, "end_line": 2},
            workspace=self.workspace,
            tool_mode="readonly",
        )
        listed = fusion_runner.execute_tool(
            "list_files",
            {"path": ".", "pattern": "**/*.py"},
            workspace=self.workspace,
            tool_mode="readonly",
        )
        searched = fusion_runner.execute_tool(
            "grep",
            {"pattern": "needle", "path": "src", "file_glob": "*.py"},
            workspace=self.workspace,
            tool_mode="readonly",
        )
        write = fusion_runner.execute_tool(
            "write_file",
            {"path": "blocked.txt", "content": "no"},
            workspace=self.workspace,
            tool_mode="readonly",
        )
        command = fusion_runner.execute_tool(
            "run_command",
            {"command": "echo blocked"},
            workspace=self.workspace,
            tool_mode="readonly",
        )

        self.assertTrue(read["ok"])
        self.assertIn("2: needle here", read["content"])
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["matches"], ["src/app.py"])
        self.assertTrue(searched["ok"])
        self.assertEqual(searched["matches"][0]["line"], 2)
        self.assertEqual(write["error"], "tool_not_allowed")
        self.assertEqual(command["error"], "tool_not_allowed")

    def test_file_tools_reject_workspace_escape(self):
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")

        read = fusion_runner.execute_tool(
            "read_file",
            {"path": "../outside.txt"},
            workspace=self.workspace,
            tool_mode="readonly",
        )
        write = fusion_runner.execute_tool(
            "write_file",
            {"path": "../escaped.txt", "content": "blocked"},
            workspace=self.workspace,
            tool_mode="coding",
        )

        self.assertEqual(read["error"], "FUSION_PATH_OUTSIDE_WORKSPACE")
        self.assertEqual(write["error"], "FUSION_PATH_OUTSIDE_WORKSPACE")
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_coding_tools_write_and_run_with_fusion_child_environment(self):
        written = fusion_runner.execute_tool(
            "write_file",
            {"path": "notes/result.txt", "content": "hello\n"},
            workspace=self.workspace,
            tool_mode="coding",
        )
        command = (
            f"{json.dumps(sys.executable)} -c "
            "\"import os; print(os.environ.get('AGENTRELAY_FUSION_CHILD', ''))\""
        )
        executed = fusion_runner.execute_tool(
            "run_command",
            {"command": command, "timeout_seconds": 10},
            workspace=self.workspace,
            tool_mode="coding",
        )

        self.assertTrue(written["ok"])
        self.assertTrue(written["created"])
        self.assertEqual(
            (self.workspace / "notes" / "result.txt").read_text(encoding="utf-8"),
            "hello\n",
        )
        self.assertTrue(executed["ok"])
        self.assertEqual(executed["stdout"].strip(), "1")

    def test_chat_completion_tool_loop_uses_tools_and_returns_final_answer(self):
        (self.workspace / "README.md").write_text("fusion evidence\n", encoding="utf-8")
        requests = []
        responses = [
            chat_response(tool_calls=[tool_call("read_file", {"path": "README.md"})]),
            chat_response("finished after reading"),
        ]

        def model(messages, tools, timeout):
            requests.append({
                "messages": copy.deepcopy(messages),
                "tools": [item["function"]["name"] for item in tools],
            })
            return responses.pop(0)

        result = self.run_sidekick(model)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer"], "finished after reading")
        self.assertEqual(result["steps"], 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            requests[0]["tools"],
            ["read_file", "list_files", "grep"],
        )
        self.assertEqual(result["tools_used"][0]["tool"], "read_file")
        self.assertTrue(result["tools_used"][0]["ok"])
        self.assertIn("fusion evidence", requests[1]["messages"][-1]["content"])

    def test_step_limit_stops_runaway_tool_loop(self):
        def model(_messages, _tools, _timeout):
            return chat_response(
                tool_calls=[tool_call("list_files", {"path": ".", "limit": 1})]
            )

        with self.assertRaises(fusion_runner.FusionRunnerError) as context:
            self.run_sidekick(model, max_steps=2)

        self.assertEqual(context.exception.code, "FUSION_STEP_LIMIT")
        session_file = fusion_runner.session_path(
            self.workspace,
            session_id="session-a",
            home=self.home,
        )
        session = json.loads(session_file.read_text(encoding="utf-8"))
        self.assertEqual(session["state"], "idle")

    def test_persistent_session_reuses_context_and_reset_clears_it(self):
        seen = []

        def model(label):
            def call(messages, _tools, _timeout):
                seen.append(copy.deepcopy(messages))
                return chat_response(label)
            return call

        first = self.run_sidekick(model("first answer"))
        second = self.run_sidekick(
            model("second answer"),
            prompt="follow-up task",
        )
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertIn("first answer", [
            message.get("content") for message in seen[1]
        ])

        self.assertTrue(fusion_runner.reset_session(
            self.workspace,
            session_id="session-a",
            home=self.home,
        ))
        third = self.run_sidekick(
            model("fresh answer"),
            prompt="fresh task",
        )
        self.assertNotIn("first answer", [
            message.get("content") for message in seen[2]
        ])
        self.assertNotIn("follow-up task", [
            message.get("content") for message in seen[2]
        ])
        self.assertEqual(third["answer"], "fresh answer")

    def test_provider_or_tool_mode_change_resets_context(self):
        seen = []

        def model(answer):
            def call(messages, _tools, _timeout):
                seen.append(copy.deepcopy(messages))
                return chat_response(answer)
            return call

        self.run_sidekick(model("provider one answer"))
        changed_provider = self.run_sidekick(
            model("provider two answer"),
            provider_ref="local:other",
        )
        self.assertNotIn("provider one answer", [
            message.get("content") for message in seen[1]
        ])
        self.assertEqual(changed_provider["answer"], "provider two answer")

        self.run_sidekick(
            model("coding answer"),
            provider_ref="local:other",
            tool_mode="coding",
        )
        self.assertNotIn("provider two answer", [
            message.get("content") for message in seen[2]
        ])

    def test_commander_rejects_before_model_call(self):
        model = mock.Mock()
        with fusion_mode.fusion_commander_lock(
            "active-commander",
            home=self.home,
            lock_dir=self.lock_dir,
        ):
            result = self.run_sidekick(model)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "COMMANDER_ACTIVE")
        model.assert_not_called()
        self.assertTrue(Path(result["handoff_path"]).is_file())

    def test_commander_preempts_between_model_and_tool_and_does_not_auto_resume(self):
        commander_context = None
        entered = False

        def preempting_model(_messages, _tools, _timeout):
            nonlocal commander_context, entered
            commander_context = fusion_mode.fusion_commander_lock(
                "preempt-task",
                home=self.home,
                lock_dir=self.lock_dir,
            )
            commander_context.__enter__()
            entered = True
            return chat_response(
                tool_calls=[tool_call("list_files", {"path": "."})]
            )

        try:
            with mock.patch.object(
                fusion_runner,
                "execute_tool",
                wraps=fusion_runner.execute_tool,
            ) as execute_tool:
                result = self.run_sidekick(preempting_model, max_steps=2)
        finally:
            if entered and commander_context is not None:
                commander_context.__exit__(None, None, None)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "COMMANDER_ACTIVE")
        execute_tool.assert_not_called()
        self.assertTrue(Path(result["handoff_path"]).is_file())
        receipts = fusion_mode.commander_results(
            home=self.home,
            session_id=result["session_id"],
        )
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["task_id"], "preempt-task")

        seen = []

        def resumed_model(messages, _tools, _timeout):
            seen.append(copy.deepcopy(messages))
            return chat_response("new task handled explicitly")

        next_result = self.run_sidekick(
            resumed_model,
            prompt="new task after commander",
        )
        self.assertEqual(next_result["status"], "success")
        self.assertEqual(len(next_result["commander_results_applied"]), 1)
        self.assertTrue(any(
            "Commander run completed" in str(message.get("content") or "")
            for message in seen[0]
        ))
        self.assertEqual(sum(
            message.get("content") == "delegated task"
            for message in seen[0]
        ), 1)

    def test_commander_started_during_model_call_preempts_final_response(self):
        commander_context = None
        entered = False

        def preempting_model(_messages, _tools, _timeout):
            nonlocal commander_context, entered
            commander_context = fusion_mode.fusion_commander_lock(
                "late-commander",
                home=self.home,
                lock_dir=self.lock_dir,
            )
            commander_context.__enter__()
            entered = True
            return chat_response("must not escape the Commander lock")

        try:
            result = self.run_sidekick(preempting_model)
        finally:
            if entered and commander_context is not None:
                commander_context.__exit__(None, None, None)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "COMMANDER_ACTIVE")
        self.assertNotIn("answer", result)
        self.assertTrue(Path(result["handoff_path"]).is_file())

    def test_explicit_session_ids_keep_fusion_histories_isolated(self):
        seen = []

        def model(answer):
            def call(messages, _tools, _timeout):
                seen.append(copy.deepcopy(messages))
                return chat_response(answer)
            return call

        session_a = self.run_sidekick(model("session A answer"), session_id="session-a")
        session_b = self.run_sidekick(model("session B answer"), session_id="session-b")

        self.assertEqual(session_a["answer"], "session A answer")
        self.assertEqual(session_b["answer"], "session B answer")
        self.assertNotIn("session A answer", [
            message.get("content") for message in seen[1]
        ])

    def test_same_session_id_in_different_workspaces_uses_different_sessions(self):
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()

        first = self.run_sidekick(
            lambda _messages, _tools, _timeout: chat_response("first"),
            session_id="shared-session",
        )
        second = self.run_sidekick(
            lambda _messages, _tools, _timeout: chat_response("second"),
            workspace=other_workspace,
            session_id="shared-session",
        )

        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(
            fusion_runner.session_path(
                self.workspace,
                session_id="shared-session",
                home=self.home,
            ),
            fusion_runner.session_path(
                other_workspace,
                session_id="shared-session",
                home=self.home,
            ),
        )

    def test_fusion_and_commander_children_are_recursive_mode_guards(self):
        fusion_state = fusion_mode.fusion_state(
            home=self.home,
            lock_dir=self.lock_dir,
            environ={"AGENTRELAY_FUSION_CHILD": "1"},
        )
        self.assertFalse(fusion_state["delegable"])
        self.assertEqual(fusion_state["reason"], "FUSION_CHILD")

        for variable in (
            "AGENTRELAY_FUSION_CHILD",
            "AGENTRELAY_COMMANDER_CHILD",
        ):
            with self.subTest(variable=variable):
                with mock.patch.dict(os.environ, {variable: "1"}):
                    with self.assertRaises(PermissionError):
                        with fusion_mode.fusion_commander_lock(
                            "forbidden",
                            home=self.home,
                            lock_dir=self.lock_dir,
                        ):
                            pass

    def test_coding_mode_command_requires_confirmation(self):
        rejected, rejected_code = agent_fusion._mode_command(
            "coding",
            self.home,
        )
        self.assertEqual(rejected_code, 2)
        self.assertEqual(
            rejected["error"],
            "FUSION_CODING_CONFIRMATION_REQUIRED",
        )

        accepted, accepted_code = agent_fusion._mode_command(
            "coding",
            self.home,
            confirmed=True,
        )
        self.assertEqual(accepted_code, 0)
        self.assertEqual(accepted["state"]["tool_mode"], "coding")

    def test_cli_mode_and_reset_emit_structured_results(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = agent_fusion.main(["mode", "coding", "--confirm-coding"])
        self.assertEqual(code, 0)
        self.assertIn("AGENT_FUSION_RESULT=", output.getvalue())
        self.assertEqual(
            config_manager.load_config(self.home)["fusion"]["tool_mode"],
            "coding",
        )

        session_file = fusion_runner.session_path(
            self.workspace,
            session_id="session-a",
            home=self.home,
        )
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("{}\n", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = agent_fusion.main([
                "reset",
                "--workspace",
                str(self.workspace),
                "--session-id",
                "session-a",
            ])
        self.assertEqual(code, 0)
        self.assertFalse(session_file.exists())
        self.assertIn('"reset": true', output.getvalue())


if __name__ == "__main__":
    unittest.main()
