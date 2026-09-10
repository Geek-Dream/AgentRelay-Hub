import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from agent_relay_runtime import (  # noqa: E402
    SessionBindingStore,
    SessionRef,
    provider_state_file,
    session_bindings_file,
    venv_python,
)


class RuntimePathTests(unittest.TestCase):
    def test_virtualenv_python_is_cross_platform(self):
        root = Path("C:/Users/test/.codex")
        self.assertEqual(
            venv_python(root, os_name="nt"),
            root / "agentrelay-env" / "Scripts" / "python.exe",
        )
        self.assertEqual(
            venv_python(Path("/tmp/codex"), os_name="posix"),
            Path("/tmp/codex/agentrelay-env/bin/python"),
        )

    def test_deepseek_keeps_legacy_login_state_path(self):
        root = Path("/tmp/codex")
        self.assertEqual(
            provider_state_file("deepseek", root, {}),
            root / "skills/agent-relay/agent_relay_login_state.json",
        )
        self.assertEqual(
            provider_state_file("qwen", root, {}),
            root / "skills/agent-relay/agent_relay_qwen_login_state.json",
        )

    def test_runtime_paths_can_be_overridden(self):
        root = Path("/tmp/codex")
        env = {
            "AGENT_RELAY_QWEN_LOGIN_STATE": "/tmp/qwen-state.json",
            "AGENT_RELAY_SESSION_BINDINGS": "/tmp/bindings.json",
        }
        self.assertEqual(
            provider_state_file("qwen", root, env),
            Path("/tmp/qwen-state.json"),
        )
        self.assertEqual(
            session_bindings_file(root, env),
            Path("/tmp/bindings.json"),
        )


class SessionBindingStoreTests(unittest.TestCase):
    def test_round_trip_and_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            store = SessionBindingStore(path)
            session = SessionRef(
                provider="deepseek",
                session_id="session-1",
                title="AgentRelay-DeepSeek",
                href="https://chat.deepseek.com/a/chat/s/session-1",
            )

            store.put("unified", session)
            self.assertEqual(store.get("deepseek", "unified"), session)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["version"], 1)

            store.remove("deepseek", "unified")
            self.assertIsNone(store.get("deepseek", "unified"))

    def test_malformed_state_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            path.write_text("not-json", encoding="utf-8")
            store = SessionBindingStore(path)
            self.assertIsNone(store.get("deepseek", "unified"))


if __name__ == "__main__":
    unittest.main()
