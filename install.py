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
import argparse
import getpass
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


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
    "orchestrator_dispatcher.py",
    "orchestrator_dispatch.py",
    "orchestrator_core.py",
    "agentrelay_daemon.py",
    "agentrelay_console.py",
    "commander_provider_call.py",
    "commander_provider_pool.py",
    "task_monitor.py",
    "runtime_manager.py",
    "recovery_manager.py",
    "agent_provider.py",
    "agent_adapter.py",
    "model_router.py",
    "multi_agent.py",
    "research_commander.py",
    "agent_orchestration.py",
    "orchestrator_task.py",
    "task_scheduler.py",
    "config_manager.py",
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
PUBLISH_BLOCKLIST = {
    "agent_relay_login_state.json", "agent_relay_session_bindings.json",
    "raw_hook_payloads.jsonl", "cookies", "sessions",
}


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


def validate_publish_tree() -> None:
    """Refuse to package credentials, browser state, or runtime data."""
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = set(path.relative_to(PROJECT_ROOT).parts)
        if relative_parts & PUBLISH_BLOCKLIST or any(
            token in path.name.lower() for token in ("cookie", "session_bindings", "login_state")
        ):
            raise InstallError(f"发布目录包含禁止安装的敏感文件：{path}")


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


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}：").strip()
    return value or default


def _yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{prompt}（{hint}）：").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "是", "确认", "好的", "可以"}


def _valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _valid_provider_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", value or ""))


def _probe_models(endpoint: str, api_key: str = "") -> tuple[bool, list[str], str]:
    """测试 OpenAI-compatible 服务，不记录响应中的密钥或完整内容。"""
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    target = base + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with urlopen(Request(target, headers=headers), timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        models = [str(item.get("id")) for item in data.get("data", [])
                  if isinstance(item, dict) and item.get("id")]
        return True, models, "连接成功"
    except Exception as exc:
        return False, [], f"连接失败：{type(exc).__name__}"


def _probe_chat(endpoint: str, model: str, api_key: str = "") -> tuple[bool, str]:
    """用极短请求验证聊天接口，不输出响应内容或密钥。"""
    target = endpoint.rstrip("/")
    if not target.endswith("/chat/completions"):
        target += "/chat/completions"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "messages": [{"role": "user", "content": "请只回复：测试成功"}], "max_tokens": 8}
    try:
        with urlopen(Request(target, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"), timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        answer = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return bool(answer), "聊天接口可用" if answer else "聊天接口未返回内容"
    except Exception as exc:
        return False, f"聊天测试失败：{type(exc).__name__}"


def _scan_local_services() -> list[dict]:
    services = [
        ("Ollama", "http://127.0.0.1:11434/v1"),
        ("LM Studio", "http://127.0.0.1:1234/v1"),
        ("vLLM/llama.cpp", "http://127.0.0.1:8000/v1"),
    ]
    found = []
    for name, endpoint in services:
        ok, models, message = _probe_models(endpoint)
        if ok:
            found.append({"name": name, "endpoint": endpoint, "models": models, "message": message})
    return found


def _load_local_config():
    try:
        from scripts.config_manager import load_config, save_config
    except ImportError as exc:
        raise InstallError(f"无法加载本地配置模块：{exc}") from exc
    return load_config(CODEX_HOME), save_config


def _configure_web(config: dict) -> None:
    print("\n线上模型配置（网页登录会话）")
    print("可配置 DeepSeek、千问、Kimi 或其他网页模型。Cookie 只保存在本机，不会写入项目。")
    providers = config.setdefault("web_providers", [])
    while True:
        print("\n1. DeepSeek  2. 千问  3. Kimi  4. 自定义网页模型  0. 返回")
        choice = input("请选择：").strip()
        if choice == "0":
            return
        defaults = {"1": ("deepseek-web", "DeepSeek", "https://chat.deepseek.com/"),
                    "2": ("qianwen-web", "千问", "https://www.qianwen.com/"),
                    "3": ("kimi-web", "Kimi", "https://www.kimi.com/")}
        if choice in defaults:
            provider_id, label, url = defaults[choice]
        elif choice == "4":
            label = _ask("模型显示名称")
            provider_id = _ask("模型别名（只用英文、数字和短横线）", label.lower().replace(" ", "-"))
            url = _ask("登录网址")
            if not label or not _valid_provider_id(provider_id) or not _valid_url(url):
                print("网址或名称无效，请重试。")
                continue
        else:
            print("选项无效，请重试。")
            continue
        if provider_id != "deepseek-web":
            print(f"{label} 的网页适配器尚未内置。可以先保存配置，后续接入适配器后使用。")
        if not _yes_no(f"现在打开 {label} 登录页面", True):
            continue
        login_script = TARGET_SKILL / "scripts" / "agent_relay_login.py"
        command = [str(VENV_PYTHON), str(login_script), "--provider", provider_id.removesuffix("-web")]
        if provider_id != "deepseek-web":
            command.extend(["--url", url, "--state-file",
                            str(TARGET_SKILL / "config" / f"{provider_id}.storage-state.json")])
        try:
            subprocess.run(command, check=False)
        except OSError as exc:
            print(f"登录脚本启动失败：{exc}")
        entry = {"id": provider_id, "name": label, "url": url, "enabled": True,
                 "state_file": str(TARGET_SKILL / "agent_relay_login_state.json.enc") if provider_id == "deepseek-web" else
                 str(TARGET_SKILL / "config" / f"{provider_id}.storage-state.json.enc")}
        providers[:] = [item for item in providers if not isinstance(item, dict) or item.get("id") != provider_id]
        providers.append(entry)
        config["default_provider"] = provider_id
        print(f"已保存 {label} 配置。")


def _configure_local(config: dict) -> None:
    print("\n本地模型配置")
    print("支持 Ollama、llama.cpp、vLLM、LM Studio 等 OpenAI 兼容接口；本地模型始终单并发。")
    if not _yes_no("配置本地模型", True):
        return
    found = _scan_local_services()
    if found:
        print("检测到以下本地服务：")
        for index, item in enumerate(found, 1):
            models = "、".join(item["models"]) or "未返回模型名"
            print(f"{index}. {item['name']}：{item['endpoint']}（{models}）")
        selected = _ask("选择服务编号，或输入 0 手动填写", "1")
        try:
            selected_item = found[int(selected) - 1] if int(selected) > 0 else None
        except (ValueError, IndexError):
            selected_item = None
        endpoint = selected_item["endpoint"] if selected_item else _ask("本地接口地址", "http://127.0.0.1:11434/v1")
        default_model = (selected_item["models"] or ["llama3.2"])[0] if selected_item else "llama3.2"
    else:
        print("未检测到 Ollama、LM Studio 或 vLLM/llama.cpp。可以重新扫描或手动配置。")
        print("1. 重新扫描  2. 手动填写  3. 上一步")
        scan_choice = input("请选择：").strip()
        if scan_choice == "1":
            return _configure_local(config)
        if scan_choice == "3":
            return
        endpoint = _ask("本地接口地址", "http://127.0.0.1:11434/v1")
        default_model = "llama3.2"
    model = _ask("模型名称", default_model)
    alias = _ask("本地模型别名", model)
    queue_timeout = _ask("最多等待多少秒（超过后改走线上或离线规则）", "30")
    if not _valid_url(endpoint):
        print("接口地址无效，已跳过。")
        return
    ok, models, message = _probe_models(endpoint)
    print(f"本地服务测试：{message}。")
    if not ok and not _yes_no("服务当前不可用，仍然保存配置", False):
        return
    if models and model not in models:
        model = _ask("未发现该模型，请输入实际模型名", models[0])
    config.setdefault("local_providers", [])[:] = [{"id": alias, "name": alias, "endpoint": endpoint,
        "model": model, "enabled": True, "queue_timeout": int(queue_timeout or 30)}]
    print("已保存本地模型配置。首次调用时会再次检查服务是否启动。")


def _configure_api(config: dict) -> None:
    print("\nAPI 模型配置")
    print("API 密钥只加密保存在本机；Commander 子 Agent 不会调用 API Provider。")
    if not _yes_no("配置 API 模型", True):
        return
    endpoint = _ask("API 地址", "https://api.openai.com/v1")
    api_key = getpass.getpass("API Key（输入时不显示）：").strip()
    model = _ask("模型名称", "gpt-5.5")
    alias = _ask("模型别名", model)
    print("请求类型：1. OpenAI 兼容聊天接口  2. 自定义兼容接口")
    request_type = "openai-compatible" if input("请选择 [1]：").strip() != "2" else "custom-compatible"
    if not _valid_url(endpoint) or not api_key or not model:
        print("API 配置不完整，已跳过。")
        return
    ok, models, message = _probe_models(endpoint, api_key)
    print(f"API 测试：{message}。")
    if not ok and not _yes_no("API 当前不可用，仍然保存配置", False):
        return
    if models and model not in models:
        model = _ask("接口返回的模型名与输入不同，请输入模型名", models[0])
    config.setdefault("api_providers", [])[:] = [{"id": alias, "name": alias, "endpoint": endpoint,
        "api_key": api_key, "model": model, "request_type": request_type, "enabled": True, "timeout": 60}]
    if _yes_no("现在发送一次测试请求", False):
        chat_ok, chat_message = _probe_chat(endpoint, model, api_key)
        print(f"API 聊天测试：{chat_message}。")
        if not chat_ok and not _yes_no("聊天测试失败，仍然保存配置", False):
            return
    print("已保存 API 模型配置。")


def configure_models(config: dict) -> None:
    while True:
        print("\n模型配置：1. 线上模型  2. 本地模型  3. API 模型  0. 返回")
        choice = input("请选择：").strip()
        if choice == "0":
            return
        if choice == "1":
            _configure_web(config)
        elif choice == "2":
            _configure_local(config)
        elif choice == "3":
            _configure_api(config)
        else:
            print("选项无效，请重试。")


def configure_commander(config: dict) -> None:
    print("\nCommander 高级功能")
    print("开启后，主 Agent 可按任务动态启动 1-5 个隔离子 Agent，做前端、后端、数据库、审计等协同工作；")
    print("每个子 Agent 独立计时和重试，结果先审查，必须经过确认卡才合并；子 Agent 不能递归启动 Commander。")
    print("网页/本地模型是可选辅助；至少要有当前 Codex CLI 才能启动真正的子 Agent。")
    enabled = _yes_no("开启 Commander", False)
    config.setdefault("commander", {}).update({"enabled": enabled})
    if not enabled:
        return
    terminal = _ask("使用的终端命令", "codex")
    command = _ask("启动子 Agent 的命令", f"{terminal} exec")
    try:
        max_agents = max(1, min(5, int(_ask("最多同时启动几个子 Agent", "5"))))
    except ValueError:
        max_agents = 5
    config["commander"].update({"terminal": terminal, "command": command, "max_agents": max_agents})


def initialize_configuration() -> None:
    config, save_config = _load_local_config()
    print("\nAgentRelay 初始化向导")
    print("1. 安装离线基础能力：Hook、Skill、Tracker、确认卡和本地规则。")
    if _yes_no("现在配置默认 DeepSeek 网页模型", True):
        _configure_web(config)
    else:
        print("已跳过 DeepSeek，之后可从主菜单重新配置。")
    if _yes_no("现在配置本地模型", True):
        _configure_local(config)
    else:
        print("已跳过本地模型，系统将使用离线规则。")
    save_config(config, CODEX_HOME)
    print("基础配置已加密保存。")


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
    print("\n✅ AgentRelay 一键安装完成。")
    print(f"独立 Python 环境：{VENV_DIR}")
    print("接下来请执行：")
    print(f"  1. {VENV_PYTHON} {login}")
    print("     请在浏览器中手动登录，并完成人机验证/CAPTCHA。")
    print("  2. 重启 Codex，使其重新加载 hooks.json。")


def run_chinese_menu() -> None:
    """显示中文安装菜单；适合用户直接运行 install.py。"""
    while True:
        print("\n" + "=" * 56)
        print("AgentRelay 安装与配置中心")
        print("1. 初始化当前脚本（安装离线基础能力）")
        print("2. 开启/关闭 Commander 高级功能")
        print("3. 配置模型（线上 / 本地 / API）")
        print("4. 查看当前配置状态")
        print("0. 退出")
        choice = input("请选择：").strip()
        if choice == "0":
            print("已退出，配置仍保存在本机。")
            return
        if choice == "1":
            initialize_configuration()
        elif choice == "2":
            config, save_config = _load_local_config()
            configure_commander(config)
            save_config(config, CODEX_HOME)
            print("Commander 配置已加密保存。")
        elif choice == "3":
            config, save_config = _load_local_config()
            configure_models(config)
            save_config(config, CODEX_HOME)
            print("模型配置已加密保存。")
        elif choice == "4":
            config, _ = _load_local_config()
            print(json.dumps({
                "运行模式": "Commander 高级模式" if config.get("commander", {}).get("enabled") else "离线基础模式",
                "线上模型数量": len(config.get("web_providers", [])),
                "本地模型数量": len(config.get("local_providers", [])),
                "API 模型数量": len(config.get("api_providers", [])),
                "配置文件": str(TARGET_SKILL / "config" / "agentrelay-config.json.enc"),
            }, ensure_ascii=False, indent=2))
        else:
            print("选项无效，请输入菜单中的数字。")


def write_daemon_service_config() -> Path | None:
    """Install a service descriptor only when explicitly requested."""
    if os.environ.get("AGENTRELAY_INSTALL_DAEMON") != "1":
        return None
    daemon = TARGET_SKILL / "scripts" / "agentrelay_daemon.py"
    command = [str(VENV_PYTHON), str(daemon), "--runtime", str(TARGET_SKILL / "runtime" / "tasks")]
    if sys.platform == "darwin":
        from scripts.research_commander import service_config
        target = CODEX_HOME / "launchagents" / "com.agentrelay.daemon.plist"
        target.parent.mkdir(parents=True, exist_ok=True); target.write_text(service_config("launchd", TARGET_SKILL, command), encoding="utf-8")
    elif sys.platform.startswith("linux"):
        from scripts.research_commander import service_config
        target = CODEX_HOME / "systemd" / "agentrelay.service"
        target.parent.mkdir(parents=True, exist_ok=True); target.write_text(service_config("systemd", TARGET_SKILL, command), encoding="utf-8")
    print(f"[生成] daemon 服务配置：{target}")
    return target


def register_daemon_service(target: Path) -> None:
    """Explicitly register a generated service; never run during normal install."""
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
        print(f"[注册] launchd：{target}")
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "enable", "--now", str(target)], check=True)
        print(f"[注册] systemd：{target}")
    else:
        raise InstallError("当前平台不支持自动注册 daemon 服务")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AgentRelay 中文安装与配置脚本")
    parser.add_argument("--menu", action="store_true", help="进入中文安装与配置菜单")
    parser.add_argument("--no-menu", action="store_true", help="只安装文件，不进入交互菜单")
    parser.add_argument("--install-daemon", action="store_true", help="生成后台服务配置")
    parser.add_argument("--register-daemon", action="store_true", help="注册已生成的后台服务")
    args = parser.parse_args(argv)
    print(f"AgentRelay 安装程序：{PROJECT_ROOT}")
    print(f"Codex 目录：{CODEX_HOME}")
    if sys.version_info < (3, 10):
        print("[错误] 需要 Python 3.10 或更高版本。", file=sys.stderr)
        return 1
    try:
        validate_publish_tree()
        check_base_environment()
        ensure_virtualenv()
        install_dependencies()
        install_and_verify_chromium()
        install_files()
        merge_hooks_json()
        if not args.no_menu and (args.menu or (argv is None and sys.stdin.isatty())):
            run_chinese_menu()
        if args.install_daemon or args.register_daemon:
            os.environ["AGENTRELAY_INSTALL_DAEMON"] = "1"
        service = write_daemon_service_config()
        if args.register_daemon and service is not None:
            register_daemon_service(service)
        print_next_steps()
    except (InstallError, OSError) as exc:
        print(f"[错误] 安装已停止：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
