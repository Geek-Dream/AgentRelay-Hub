import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import deletion_policy as policy


class DeletionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentrelay-delete-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.home.mkdir()

    def execute(self, target, mode):
        return subprocess.run(
            [sys.executable, str(policy.__file__), "execute", "--home", str(self.home),
             "--cwd", str(self.workspace), "--mode", mode, "--", str(target)],
            text=True, capture_output=True, check=False,
        )

    def test_default_named_temp_evidence_is_quietly_recoverable(self):
        source = self.root / "agentrelay-review-before.png"
        source.write_bytes(b"test image")
        completed = self.execute(source, "standard")
        result = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["status"], "moved_to_trash")
        self.assertFalse(source.exists())
        destination = Path(result["items"][0]["destination"])
        self.assertTrue(destination.is_file())
        manifest = Path(result["manifest"])
        self.assertEqual(json.loads(manifest.read_text())["operation"], "move_to_trash")
        destination.rename(source)
        self.assertEqual(source.read_bytes(), b"test image")

    def test_default_persistent_file_still_requires_normal_gate(self):
        source = self.workspace / "important.py"
        source.write_text("keep me")
        self.assertIsNone(policy.safe_delete_command(
            f"rm {source}", home=self.home, cwd=self.workspace, mode="standard",
        ))
        self.assertTrue(source.exists())

    def test_temp_path_alone_is_not_a_quiet_delete_permission(self):
        source = self.root / "important-user-document.txt"
        source.write_text("keep me")
        self.assertIsNone(policy.safe_delete_command(
            f"rm {source}", home=self.home, cwd=self.workspace, mode="standard",
        ))
        self.assertTrue(source.exists())

    def test_strict_can_move_explicit_target_to_trash(self):
        source = self.workspace / "old-output.txt"
        source.write_text("generated")
        completed = self.execute(source, "strict")
        result = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["status"], "moved_to_trash")
        self.assertFalse(source.exists())
        self.assertTrue(Path(result["items"][0]["destination"]).is_file())

    def test_duplicate_basenames_receive_unique_trash_destinations(self):
        first = self.workspace / "hooks" / "__pycache__"
        second = self.workspace / "scripts" / "__pycache__"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / "first.pyc").write_bytes(b"first")
        (second / "second.pyc").write_bytes(b"second")

        result = policy.move_to_trash(
            [str(first), str(second)],
            home=self.home,
            cwd=self.workspace,
        )

        self.assertEqual(result["status"], "moved_to_trash")
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        destinations = [Path(item["destination"]) for item in result["items"]]
        self.assertEqual(len(set(destinations)), 2)
        self.assertTrue(all(path.is_dir() for path in destinations))
        self.assertEqual(sum(path.name == "__pycache__" for path in destinations), 1)
        self.assertTrue(any(path.name.startswith("__pycache__.") for path in destinations))
        self.assertTrue(Path(result["manifest"]).is_file())

    def test_safe_delete_command_round_trips_for_standard_and_strict(self):
        evidence = self.root / "agentrelay-review-before.png"
        evidence.write_bytes(b"image")
        standard = policy.safe_delete_command(
            f"rm {evidence}", home=self.home, cwd=self.workspace, mode="standard",
        )
        self.assertIsNotNone(standard)
        self.assertTrue(policy.is_safe_delete_command(
            standard, home=self.home, mode="standard",
        ))

        project_file = self.workspace / "old-output.txt"
        project_file.write_text("generated")
        strict = policy.safe_delete_command(
            f"rm {project_file}", home=self.home, cwd=self.workspace, mode="strict",
        )
        self.assertIsNotNone(strict)
        self.assertTrue(policy.is_safe_delete_command(
            strict, home=self.home, mode="strict",
        ))

    def test_safe_delete_command_rejects_tampered_mode_home_or_target(self):
        evidence = self.root / "agentrelay-review-before.png"
        evidence.write_bytes(b"image")
        replacement = policy.safe_delete_command(
            f"rm {evidence}", home=self.home, cwd=self.workspace, mode="standard",
        )
        self.assertIsNotNone(replacement)

        mode_changed = shlex.split(replacement)
        mode_changed[8] = "relaxed"
        self.assertFalse(policy.is_safe_delete_command(
            shlex.join(mode_changed), home=self.home, mode="relaxed",
        ))

        home_changed = shlex.split(replacement)
        home_changed[4] = str(self.root / "other-home")
        self.assertFalse(policy.is_safe_delete_command(
            shlex.join(home_changed), home=self.home, mode="standard",
        ))

        persistent = self.workspace / "important.txt"
        persistent.write_text("keep")
        target_changed = shlex.split(replacement)
        target_changed[10] = str(persistent)
        self.assertFalse(policy.is_safe_delete_command(
            shlex.join(target_changed), home=self.home, mode="standard",
        ))

    def test_relaxed_preserves_explicit_direct_delete_behavior(self):
        source = self.workspace / "disposable.txt"
        source.write_text("delete")
        self.assertIsNone(policy.safe_delete_command(
            f"rm {source}", home=self.home, cwd=self.workspace, mode="relaxed",
        ))
        self.assertTrue(source.exists())

    def test_chained_delete_is_never_rewritten(self):
        command = "rm -rf /tmp/example && git clone https://example.invalid/repo /tmp/example"
        self.assertIsNone(policy.parse_simple_delete(command))

    def test_windows_delete_commands_use_the_managed_recycler(self):
        evidence = (
            self.root
            / "Agent Relay Evidence"
            / "agentrelay-review-before.png"
        )
        evidence.parent.mkdir()
        evidence.write_bytes(b"image")
        command = f'Remove-Item -LiteralPath "{evidence}" -Force'

        self.assertEqual(
            policy.parse_simple_delete(command, platform_name="nt"),
            [{"command": "remove-item", "target": str(evidence)}],
        )
        replacement = policy.safe_delete_command(
            command,
            home=self.home,
            cwd=self.workspace,
            mode="standard",
            platform_name="nt",
        )
        self.assertIsNotNone(replacement)
        self.assertIn('"', replacement)
        self.assertNotIn("Remove-Item", replacement)
        self.assertTrue(policy.is_safe_delete_command(
            replacement,
            home=self.home,
            mode="standard",
            platform_name="nt",
        ))

    def test_windows_cmd_delete_parsing_rejects_chains(self):
        self.assertEqual(
            policy.parse_simple_delete(
                r'del /q "C:\Temp\agentrelay-review-before.png"',
                platform_name="nt",
            ),
            [{
                "command": "del",
                "target": r"C:\Temp\agentrelay-review-before.png",
            }],
        )
        self.assertIsNone(policy.parse_simple_delete(
            r'Remove-Item "C:\Temp\agentrelay-review-before.png"; '
            r'Remove-Item "C:\Temp\other.png"',
            platform_name="nt",
        ))
        self.assertIsNone(policy.parse_simple_delete(
            r'Remove-Item -Path "C:\Temp\agentrelay-review-before.png",'
            r'"C:\Temp\agentrelay-review-after.png"',
            platform_name="nt",
        ))

    def test_symlink_is_not_followed(self):
        source = self.workspace / "actual.txt"
        source.write_text("keep")
        link = self.workspace / "agentrelay-review-link.png"
        link.symlink_to(source)
        self.assertIsNone(policy.safe_delete_command(
            f"rm {link}", home=self.home, cwd=self.workspace, mode="standard",
        ))
        self.assertTrue(source.exists())
        self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
