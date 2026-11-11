"""Cross-platform process command construction."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def _resolve_windows_executable(
    executable: str,
    *,
    environ: Mapping[str, str],
) -> str:
    path = Path(executable)
    if path.suffix.casefold() in {".bat", ".cmd", ".com", ".exe", ".ps1"}:
        return executable
    resolved = shutil.which(executable, path=environ.get("PATH"))
    return resolved or executable


def process_command(
    command: Sequence[object],
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return an argv that can launch Windows command wrappers safely.

    ``CreateProcess`` cannot directly execute npm-generated ``.cmd``/``.bat``
    shims. PowerShell scripts likewise require a PowerShell host. Other
    executables and all POSIX commands remain unchanged.
    """

    parts = [str(part) for part in command]
    if not parts:
        raise ValueError("command 不能为空")
    env = dict(os.environ if environ is None else environ)
    if (platform_name or os.name) != "nt":
        return parts

    executable = _resolve_windows_executable(parts[0], environ=env)
    suffix = Path(executable).suffix.casefold()
    if suffix in {".bat", ".cmd"}:
        comspec = env.get("COMSPEC") or shutil.which("cmd", path=env.get("PATH"))
        if not comspec:
            comspec = "cmd.exe"
        command_line = subprocess.list2cmdline([executable, *parts[1:]])
        return [comspec, "/d", "/s", "/c", f'"{command_line}"']
    if suffix == ".ps1":
        powershell = (
            shutil.which("pwsh", path=env.get("PATH"))
            or shutil.which("powershell", path=env.get("PATH"))
            or "powershell.exe"
        )
        return [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            executable,
            *parts[1:],
        ]
    return [executable, *parts[1:]]
