"""桌面通知：把"即将执行的自动化"提前告诉用户。

只发系统级通知（macOS 通知中心、Linux 桌面、Windows Toast），
不弹窗口、不抢焦点、不阻塞调用方。发送失败一律静默降级——
通知本身坏了，不能顺带把用户要的自动化也废掉。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any


TIMEOUT_SECONDS = 8
MAX_LENGTH = 240


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:MAX_LENGTH]


def _applescript_string(value: str) -> str:
    """Escape a value so it cannot terminate the AppleScript literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def unsupported_reason() -> str:
    return (
        f"当前平台 {sys.platform} 没有可用的桌面通知命令；"
        "自动化仍会执行，只是没有系统弹窗提醒。"
    )


def _macos_command(title: str, body: str) -> list[str]:
    script = (
        f'display notification "{_applescript_string(body)}" '
        f'with title "{_applescript_string(title)}"'
    )
    return ["osascript", "-e", script]


def _linux_command(title: str, body: str) -> list[str] | None:
    if not shutil.which("notify-send"):
        return None
    return ["notify-send", "--app-name=AgentRelay", title, body]


def _windows_command(title: str, body: str) -> list[str] | None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    escaped_title = title.replace("'", "''")
    escaped_body = body.replace("'", "''")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n=$t.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($t.CreateTextNode('{escaped_title}')) > $null;"
        f"$n.Item(1).AppendChild($t.CreateTextNode('{escaped_body}')) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('AgentRelay').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    return [powershell, "-NoProfile", "-NonInteractive", "-Command", script]


def _command_for(title: str, body: str) -> list[str] | None:
    if sys.platform == "darwin":
        return _macos_command(title, body)
    if sys.platform.startswith("linux"):
        return _linux_command(title, body)
    if sys.platform == "win32" or os.name == "nt":
        return _windows_command(title, body)
    return None


def notify(title: Any, body: Any, *, enabled: bool = True) -> bool:
    """Send one desktop notification. Returns whether the system accepted it."""
    if not enabled:
        return False
    heading = _clean(title)
    text = _clean(body)
    if not heading or not text:
        return False
    command = _command_for(heading, text)
    if command is None:
        return False
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
