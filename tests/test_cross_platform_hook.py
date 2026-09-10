import importlib.util
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_module("agent_relay_installer", PROJECT_ROOT / "install.py")
hook = load_module(
    "agent_relay_hook",
    PROJECT_ROOT / "hooks" / "agent_relay_hook.py",
)


class CrossPlatformHookTests(unittest.TestCase):
    def test_windows_command_quotes_paths_with_spaces(self):
        command = installer.build_hook_command(
            Path("C:/Users/Test User/.codex/agentrelay-env/Scripts/python.exe"),
            Path("C:/Users/Test User/.codex/hooks/agent_relay_hook.py"),
            "nt",
        )
        self.assertIn('"C:\\Users\\Test User', command.replace("/", "\\"))
        self.assertIn("python.exe", command)
        self.assertIn("agent_relay_hook.py", command)

    def test_posix_command_quotes_paths_with_spaces(self):
        command = installer.build_hook_command(
            Path("/Users/test user/.codex/agentrelay-env/bin/python"),
            Path("/Users/test user/.codex/hooks/agent_relay_hook.py"),
            "posix",
        )
        self.assertIn("'/Users/test user/", command)

    def test_hook_only_forwards_valid_post_tool_output(self):
        valid = (
            '{"hookSpecificOutput":{"hookEventName":"PostToolUse",'
            '"additionalContext":"ready"}}'
        )
        self.assertTrue(hook.valid_hook_output(valid))
        self.assertFalse(hook.valid_hook_output('{"value": 1}'))
        self.assertFalse(hook.valid_hook_output("not-json"))


if __name__ == "__main__":
    unittest.main()
