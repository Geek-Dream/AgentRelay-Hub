import os
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import desktop_notify  # noqa: E402


class DesktopNotifyTests(unittest.TestCase):
    def test_disabled_config_never_touches_the_system(self):
        with mock.patch.object(desktop_notify.subprocess, "run") as run:
            sent = desktop_notify.notify(
                "标题", "正文", enabled=False,
            )
        self.assertFalse(sent)
        run.assert_not_called()

    def test_empty_title_and_body_are_not_sent(self):
        with mock.patch.object(desktop_notify.subprocess, "run") as run:
            self.assertFalse(desktop_notify.notify("", "正文", enabled=True))
            self.assertFalse(desktop_notify.notify("标题", "   ", enabled=True))
        run.assert_not_called()

    def test_macos_uses_a_single_osascript_notification(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(desktop_notify.sys, "platform", "darwin"), \
             mock.patch.object(desktop_notify.subprocess, "run",
                               return_value=completed) as run:
            sent = desktop_notify.notify(
                "AgentRelay", "即将打开 Slack", enabled=True,
            )
        self.assertTrue(sent)
        command = run.call_args[0][0]
        self.assertEqual(command[0], "osascript")
        script = command[command.index("-e") + 1]
        self.assertIn("display notification", script)
        self.assertIn("Slack", script)

    def test_linux_uses_notify_send(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(desktop_notify.sys, "platform", "linux"), \
             mock.patch.object(desktop_notify.shutil, "which",
                               return_value="/usr/bin/notify-send"), \
             mock.patch.object(desktop_notify.subprocess, "run",
                               return_value=completed) as run:
            sent = desktop_notify.notify(
                "AgentRelay", "后台启动进程", enabled=True,
            )
        self.assertTrue(sent)
        self.assertEqual(run.call_args[0][0][0], "notify-send")

    def test_unsupported_platform_reports_gracefully(self):
        with mock.patch.object(desktop_notify.sys, "platform", "sunos5"), \
             mock.patch.object(desktop_notify.subprocess, "run") as run:
            sent = desktop_notify.notify("t", "b", enabled=True)
            reason = desktop_notify.unsupported_reason()
        self.assertFalse(sent)
        run.assert_not_called()
        self.assertIn("sunos5", reason)

    def test_quotes_in_text_cannot_break_out_of_applescript(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(desktop_notify.sys, "platform", "darwin"), \
             mock.patch.object(desktop_notify.subprocess, "run",
                               return_value=completed) as run:
            desktop_notify.notify(
                'Agent"Relay', 'x" & do shell script "rm -rf ~',
                enabled=True,
            )
        command = run.call_args[0][0]
        script = command[command.index("-e") + 1]
        # Only the four structural quotes may remain unescaped.
        self.assertEqual(script.count('"') - script.count('\\"'), 4)
        self.assertIn('\\" & do shell script \\"', script)

    def test_failure_is_swallowed_so_automation_still_runs(self):
        with mock.patch.object(desktop_notify.sys, "platform", "darwin"), \
             mock.patch.object(desktop_notify.subprocess, "run",
                               side_effect=OSError("no osascript")):
            self.assertFalse(desktop_notify.notify("t", "b", enabled=True))


if __name__ == "__main__":
    unittest.main()
