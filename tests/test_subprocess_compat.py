import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compat = load_module(
    "subprocess_compat",
    PROJECT_ROOT / "scripts" / "subprocess_compat.py",
)


class WindowsCommandConstructionTests(unittest.TestCase):
    def test_cmd_wrapper_uses_comspec_and_preserves_argument_boundaries(self):
        command = [
            r"C:\Program Files\nodejs\codex.cmd",
            "exec",
            "--prompt",
            "review this file",
        ]
        result = compat.process_command(
            command,
            platform_name="nt",
            environ={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
        )

        self.assertEqual(result[:4], [
            r"C:\Windows\System32\cmd.exe",
            "/d",
            "/s",
            "/c",
        ])
        self.assertEqual(
            result[4],
            '"' + subprocess.list2cmdline(command) + '"',
        )

    def test_extensionless_command_resolves_to_npm_cmd_wrapper(self):
        resolved = r"C:\Users\Test User\AppData\Roaming\npm\codex.CMD"
        with mock.patch.object(compat.shutil, "which", return_value=resolved):
            result = compat.process_command(
                ["codex", "exec", "hello"],
                platform_name="nt",
                environ={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
            )

        self.assertEqual(result[0], r"C:\Windows\System32\cmd.exe")
        self.assertIn(resolved, result[-1])

    def test_powershell_script_uses_a_powershell_host(self):
        powershell = r"C:\Program Files\PowerShell\7\pwsh.exe"

        def which(name, path=None):
            return powershell if name == "pwsh" else None

        with mock.patch.object(compat.shutil, "which", side_effect=which):
            result = compat.process_command(
                [r"C:\Agent Relay\codex.ps1", "exec", "hello world"],
                platform_name="nt",
                environ={},
            )

        self.assertEqual(result[:7], [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ])
        self.assertEqual(result[7:], [
            r"C:\Agent Relay\codex.ps1",
            "exec",
            "hello world",
        ])

    def test_native_executable_is_not_wrapped(self):
        command = [r"C:\Program Files\nodejs\node.exe", "--version"]
        self.assertEqual(
            compat.process_command(command, platform_name="nt", environ={}),
            command,
        )

    @unittest.skipUnless(os.name == "nt", "requires real Windows cmd.exe")
    def test_real_windows_cmd_wrapper_runs_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory(prefix="agent relay cmd ") as directory:
            root = Path(directory)
            recorder = root / "record.py"
            recorder.write_text(
                "import json, sys\n"
                "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
                encoding="utf-8",
            )
            wrapper = root / "codex.cmd"
            wrapper.write_text(
                "@echo off\r\n"
                "\"%AGENTRELAY_TEST_PYTHON%\" \"%~dp0record.py\" %*\r\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "AGENTRELAY_TEST_PYTHON": sys.executable,
                "PYTHONUTF8": "1",
            }
            completed = subprocess.run(
                compat.process_command(
                    [str(wrapper), "first value", "中文 & value"],
                    environ=env,
                ),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            ["first value", "中文 & value"],
        )

    @unittest.skipUnless(os.name == "nt", "requires real Windows PowerShell")
    def test_real_powershell_wrapper_runs_with_spaces_and_unicode(self):
        if not (
            compat.shutil.which("pwsh")
            or compat.shutil.which("powershell")
        ):
            self.skipTest("PowerShell is not installed")
        with tempfile.TemporaryDirectory(prefix="agent relay ps1 ") as directory:
            wrapper = Path(directory) / "codex.ps1"
            wrapper.write_text(
                "param([string]$One, [string]$Two)\n"
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new()\n"
                "@{one=$One; two=$Two} | ConvertTo-Json -Compress\n",
                encoding="utf-8-sig",
            )
            completed = subprocess.run(
                compat.process_command(
                    [str(wrapper), "first value", "中文 value"],
                    environ=os.environ,
                ),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"one": "first value", "two": "中文 value"},
        )


class InstallerCompatibilityTests(unittest.TestCase):
    def test_installer_copies_helper_and_registers_utf8_hook(self):
        installer = load_module(
            "agent_relay_installer_subprocess_compat",
            PROJECT_ROOT / "install.py",
        )
        with tempfile.TemporaryDirectory(
            prefix="agentrelay-installer-smoke-"
        ) as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            hooks_json = codex_home / "hooks.json"
            targets = {
                "TARGET_HOOKS": codex_home / "hooks",
                "TARGET_SKILL": codex_home / "skills" / "agent-relay",
                "TARGET_IMAGE_SKILL": codex_home / "skills" / "agent-image",
                "TARGET_FUSION_SKILL": codex_home / "skills" / "agent-fusion",
                "HOOKS_JSON": hooks_json,
                "VENV_PYTHON": (
                    codex_home
                    / "agentrelay-env"
                    / ("Scripts" if os.name == "nt" else "bin")
                    / ("python.exe" if os.name == "nt" else "python")
                ),
            }
            with mock.patch.multiple(installer, **targets), \
                    mock.patch.object(
                        installer,
                        "hook_files_for_platform",
                        return_value=installer.hook_files_for_platform("nt"),
                    ):
                installer.install_files()
                installer.merge_hooks_json()

            target_skill = targets["TARGET_SKILL"]
            self.assertTrue(
                (target_skill / "scripts" / "subprocess_compat.py").is_file()
            )
            self.assertFalse(
                (targets["TARGET_HOOKS"] / "agent_relay_hook.sh").exists()
            )
            config = json.loads(hooks_json.read_text(encoding="utf-8"))
            command = next(
                hook["command"]
                for entry in config["hooks"]["SessionStart"]
                for hook in entry["hooks"]
                if "agent_relay_hook.py" in hook["command"]
            )
            self.assertIn("-X utf8", command)


if __name__ == "__main__":
    unittest.main()
