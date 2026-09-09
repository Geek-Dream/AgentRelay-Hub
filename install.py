#!/usr/bin/env python3
"""将 AgentRelay 安装到 Codex 目录，并保留用户已有的 Hook。"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
CODEX_HOME = Path(
    os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
).expanduser()
TARGET_HOOKS = CODEX_HOME / "hooks"
TARGET_SKILL = CODEX_HOME / "skills" / "agent-relay"
HOOKS_JSON = CODEX_HOME / "hooks.json"

HOOK_FILES = ("agent_relay_hook.sh", "agent_relay_tracker.py")
SKILL_FILES = ("SKILL.md",)
SCRIPT_FILES = ("agent_relay.py", "agent_relay_login.py")
TRACKED_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
)


class InstallError(RuntimeError):
    """安装过程无法安全继续时抛出。"""


def copy_if_missing(source: Path, destination: Path) -> bool:
    """复制安装文件，并保留用户已有文件。"""

    if destination.exists():
        print(f"[保留] 已有文件未覆盖：{destination}")
        return False
    if not source.is_file():
        raise InstallError(f"发布包缺少必要文件：{source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    print(f"[安装] {destination}")
    return True


def install_files() -> None:
    for filename in HOOK_FILES:
        source = PROJECT_ROOT / "hooks" / filename
        destination = TARGET_HOOKS / filename
        installed = copy_if_missing(source, destination)
        if installed and filename.endswith(".sh"):
            destination.chmod(destination.stat().st_mode | 0o111)

    for filename in SKILL_FILES:
        copy_if_missing(
            PROJECT_ROOT / "skills" / "agent-relay" / filename,
            TARGET_SKILL / filename,
        )

    for filename in SCRIPT_FILES:
        copy_if_missing(
            PROJECT_ROOT / "scripts" / filename,
            TARGET_SKILL / "scripts" / filename,
        )


def hook_command() -> str:
    return (
        'if [ -x "${CODEX_HOME:-$HOME/.codex}/hooks/'
        'agent_relay_hook.sh" ]; then /bin/sh '
        '"${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_hook.sh"; '
        'else mkdir -p "${CODEX_HOME:-$HOME/.codex}/agent_relay_tracker/'
        'logs" 2>/dev/null || :; command -p cat >/dev/null 2>>'
        '"${CODEX_HOME:-$HOME/.codex}/agent_relay_tracker/logs/'
        'error.log" || :; fi'
    )


def load_hooks_config() -> dict:
    if not HOOKS_JSON.exists():
        return {}
    try:
        with HOOKS_JSON.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(
            f"无法安全合并格式无效的 {HOOKS_JSON}：{exc}"
        ) from exc
    if not isinstance(config, dict):
        raise InstallError(f"{HOOKS_JSON} 的根节点必须是 JSON 对象")
    return config


def has_agent_relay_hook(entries: list) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if not isinstance(nested, list):
            continue
        for hook in nested:
            if (
                isinstance(hook, dict)
                and "agent_relay_hook.sh" in str(hook.get("command", ""))
            ):
                return True
    return False


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def merge_hooks_json() -> None:
    config = load_hooks_config()
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"{HOOKS_JSON} 中的 hooks 必须是 JSON 对象")

    changed = False
    command = hook_command()
    for event in TRACKED_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise InstallError(
                f"{HOOKS_JSON} 中的 hooks.{event} 必须是 JSON 数组"
            )
        if has_agent_relay_hook(entries):
            continue
        entries.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": 10,
                    }
                ]
            }
        )
        changed = True

    if not changed:
        print(f"[保留] {HOOKS_JSON} 已包含 AgentRelay Hook")
        return

    if HOOKS_JSON.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = HOOKS_JSON.with_name(
            f"{HOOKS_JSON.name}.before-agentrelay-install.{timestamp}"
        )
        shutil.copy2(HOOKS_JSON, backup)
        print(f"[备份] {backup}")

    atomic_write_json(HOOKS_JSON, config)
    print(f"[安装] 已合并 {HOOKS_JSON}")


def command_status(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def check_environment() -> None:
    print("\n环境检查：")
    print(f"[{'正常' if sys.version_info >= (3, 10) else '缺失'}] "
          f"Python {sys.version.split()[0]}（要求 >= 3.10）")
    print(f"[{'正常' if shutil.which('codex') else '缺失'}] Codex CLI")
    pip_ok = command_status([sys.executable, "-m", "pip", "--version"])
    print(f"[{'正常' if pip_ok else '缺失'}] pip")

    playwright_ok = importlib.util.find_spec("playwright") is not None
    print(f"[{'正常' if playwright_ok else '缺失'}] Playwright")

    chromium_ok = False
    if playwright_ok:
        try:
            browser_list = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "--list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=15,
            )
            chromium_ok = (
                browser_list.returncode == 0
                and "chromium" in browser_list.stdout.lower()
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[警告] Chromium 检查失败：{exc}")
    print(f"[{'正常' if chromium_ok else '缺失'}] Playwright Chromium")


def print_next_steps() -> None:
    login = TARGET_SKILL / "scripts" / "agent_relay_login.py"
    requirements = PROJECT_ROOT / "requirements.txt"
    print("\nAgentRelay 基础安装完成。")
    print("接下来请执行：")
    print(f"  1. {sys.executable} -m pip install -r {requirements}")
    print(f"  2. {sys.executable} -m playwright install chromium")
    print(f"  3. {sys.executable} {login}")
    print("     请在浏览器中手动登录，并完成人机验证/CAPTCHA。")
    print("  4. 重启 Codex，使其重新加载 hooks.json。")


def main() -> int:
    print(f"AgentRelay 安装程序：{PROJECT_ROOT}")
    print(f"Codex 目录：{CODEX_HOME}")
    if sys.version_info < (3, 10):
        print("[错误] 需要 Python 3.10 或更高版本。", file=sys.stderr)
        return 1
    try:
        install_files()
        merge_hooks_json()
        check_environment()
        print_next_steps()
    except (InstallError, OSError) as exc:
        print(f"[错误] 安装已停止：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
