#!/usr/bin/env python3
"""将 AgentRelay 安装到 Codex 目录，并保留用户已有的 Hook。"""

from __future__ import annotations

import json
import os
import shlex
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
VENV_DIR = CODEX_HOME / "agentrelay-env"
VENV_PYTHON = (
    VENV_DIR / "Scripts" / "python.exe"
    if os.name == "nt"
    else VENV_DIR / "bin" / "python"
)
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

HOOK_FILES = (
    "agent_relay_hook.py",
    "agent_relay_hook.sh",
    "agent_relay_tracker.py",
)
SKILL_FILES = ("SKILL.md",)
SCRIPT_FILES = (
    "agent_relay.py",
    "agent_relay_login.py",
    "agent_relay_runtime.py",
    "orchestrator_runtime.py",
    "orchestrator_store.py",
)
CONFIG_FILES = ("model_registry.json",)
REFERENCE_FILES = ("orchestrator-v1.md",)
TRACKED_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
)


class InstallError(RuntimeError):
    """安装过程无法安全继续时抛出。"""


def install_managed_file(source: Path, destination: Path) -> bool:
    """Atomically install managed code and back up a differing old copy."""

    if not source.is_file():
        raise InstallError(f"发布包缺少必要文件：{source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if source.read_bytes() == destination.read_bytes():
            print(f"[保留] 已是最新版本：{destination}")
            return False
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = destination.with_name(
            f"{destination.name}.before-agentrelay-update.{timestamp}"
        )
        shutil.copy2(destination, backup)
        print(f"[备份] {backup}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"[安装] {destination}")
    return True


def install_files() -> None:
    for filename in HOOK_FILES:
        source = PROJECT_ROOT / "hooks" / filename
        destination = TARGET_HOOKS / filename
        install_managed_file(source, destination)
        if os.name != "nt" and filename.endswith(".sh"):
            destination.chmod(destination.stat().st_mode | 0o111)

    for filename in SKILL_FILES:
        install_managed_file(
            PROJECT_ROOT / "skills" / "agent-relay" / filename,
            TARGET_SKILL / filename,
        )

    for filename in SCRIPT_FILES:
        install_managed_file(
            PROJECT_ROOT / "scripts" / filename,
            TARGET_SKILL / "scripts" / filename,
        )

    for filename in CONFIG_FILES:
        install_managed_file(
            PROJECT_ROOT / "scripts" / filename,
            TARGET_SKILL / "scripts" / filename,
        )

    for filename in REFERENCE_FILES:
        install_managed_file(
            PROJECT_ROOT / "skills" / "agent-relay" / "references" / filename,
            TARGET_SKILL / "references" / filename,
        )


def build_hook_command(
    python_path: Path,
    hook_path: Path,
    platform_name: str,
) -> str:
    command = [str(python_path), str(hook_path)]
    if platform_name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def hook_command() -> str:
    return build_hook_command(
        VENV_PYTHON,
        TARGET_HOOKS / "agent_relay_hook.py",
        os.name,
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


def find_agent_relay_hook(entries: list):
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if not isinstance(nested, list):
            continue
        for hook in nested:
            if (
                isinstance(hook, dict)
                and "agent_relay_hook" in str(hook.get("command", ""))
            ):
                return hook
    return None


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
        existing_hook = find_agent_relay_hook(entries)
        if existing_hook is not None:
            if (
                existing_hook.get("command") != command
                or existing_hook.get("timeout") != 10
                or existing_hook.get("type") != "command"
            ):
                existing_hook.update(
                    {
                        "type": "command",
                        "command": command,
                        "timeout": 10,
                    }
                )
                changed = True
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


def run_checked(command: list[str], description: str) -> None:
    """运行安装步骤，并把失败转换为清晰的安装错误。"""

    print(f"[执行] {description}")
    sys.stdout.flush()
    try:
        result = subprocess.run(
            command,
            check=False,
        )
    except OSError as exc:
        raise InstallError(f"无法执行 {description}：{exc}") from exc
    if result.returncode != 0:
        raise InstallError(
            f"{description} 失败，退出码：{result.returncode}"
        )


def check_base_environment() -> None:
    print("\n环境检查：")
    print(f"[{'正常' if sys.version_info >= (3, 10) else '缺失'}] "
          f"Python {sys.version.split()[0]}（要求 >= 3.10）")
    if shutil.which("codex"):
        print("[正常] Codex CLI")
    else:
        print("[警告] PATH 中未找到 Codex CLI；文件仍会安装。")


def ensure_virtualenv() -> None:
    """创建 AgentRelay 独立环境，避免触发 PEP 668。"""

    if VENV_PYTHON.is_file():
        print(f"[保留] 已有 AgentRelay 虚拟环境：{VENV_DIR}")
    elif VENV_DIR.exists():
        raise InstallError(
            f"虚拟环境目录存在但不完整：{VENV_DIR}。"
            "请先移动或删除该目录后重试。"
        )
    else:
        run_checked(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            f"创建虚拟环境 {VENV_DIR}",
        )

    pip_check = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if pip_check.returncode != 0:
        raise InstallError(
            "虚拟环境中没有可用的 pip。Ubuntu/Debian 用户请先安装 "
            "python3-venv 后重试。"
        )
    print(f"[正常] 虚拟环境 Python：{VENV_PYTHON}")


def install_dependencies() -> None:
    if not REQUIREMENTS.is_file():
        raise InstallError(f"缺少依赖文件：{REQUIREMENTS}")
    run_checked(
        [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(REQUIREMENTS),
        ],
        "在 AgentRelay 虚拟环境中安装 Python 依赖",
    )


def install_and_verify_chromium() -> None:
    """安装并确认 headed 登录所需的完整 Chromium 可执行文件。"""

    run_checked(
        [
            str(VENV_PYTHON),
            "-m",
            "playwright",
            "install",
            "chromium",
        ],
        "安装 Playwright Chromium",
    )

    probe = (
        "from pathlib import Path; "
        "from playwright.sync_api import sync_playwright; "
        "manager=sync_playwright().start(); "
        "path=manager.chromium.executable_path; "
        "manager.stop(); print(path); "
        "raise SystemExit(0 if Path(path).is_file() else 1)"
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", probe],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    browser_path = result.stdout.strip().splitlines()
    if result.returncode != 0 or not browser_path:
        detail = result.stderr.strip() or "未找到 Chromium 可执行文件"
        raise InstallError(f"Chromium 完整浏览器验证失败：{detail}")
    print(f"[正常] 完整 Chromium：{browser_path[-1]}")


def print_next_steps() -> None:
    login = TARGET_SKILL / "scripts" / "agent_relay_login.py"
    print("\nAgentRelay 一键安装完成。")
    print(f"独立 Python 环境：{VENV_DIR}")
    print("接下来请执行：")
    print(f"  1. {VENV_PYTHON} {login}")
    print("     请在浏览器中手动登录，并完成人机验证/CAPTCHA。")
    print("  2. 重启 Codex，使其重新加载 hooks.json。")


def main() -> int:
    print(f"AgentRelay 安装程序：{PROJECT_ROOT}")
    print(f"Codex 目录：{CODEX_HOME}")
    if sys.version_info < (3, 10):
        print("[错误] 需要 Python 3.10 或更高版本。", file=sys.stderr)
        return 1
    try:
        check_base_environment()
        ensure_virtualenv()
        install_dependencies()
        install_and_verify_chromium()
        install_files()
        merge_hooks_json()
        print_next_steps()
    except (InstallError, OSError) as exc:
        print(f"[错误] 安装已停止：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
