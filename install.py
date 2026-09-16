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
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
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
    "config_web.py",
    "generic_web_adapter.py",
    "diagnose_web_provider.py",
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
        ("vLLM", "http://127.0.0.1:8000/v1"),
        ("llama.cpp", "http://127.0.0.1:8080/v1"),
        ("llama.cpp", "http://127.0.0.1:8081/v1"),
    ]
    # llama.cpp 常由用户自定义端口。macOS/Linux 上补查本机正在监听的端口，
    # 仍只访问 127.0.0.1 的 OpenAI-compatible /v1/models 接口。
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        ports = re.findall(r"(?:127\.0\.0\.1|\*|localhost):(\d+)\s*\(LISTEN\)", result.stdout)
        known = {endpoint.rsplit(":", 1)[-1].split("/")[0] for _, endpoint in services}
        for port in ports[:30]:
            if port not in known:
                services.append(("本机兼容服务", f"http://127.0.0.1:{port}/v1"))
                known.add(port)
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for name, endpoint in services:
        ok, models, message = _probe_models(endpoint)
        if ok:
            found.append({"name": name, "endpoint": endpoint, "models": models, "message": message})
    return found


def _load_local_config():
    try:
        from scripts.config_manager import migrate_legacy_deepseek_config, save_config
    except ImportError as exc:
        raise InstallError(f"无法加载本地配置模块：{exc}") from exc
    config, migrated = migrate_legacy_deepseek_config(CODEX_HOME)
    if migrated:
        print("[迁移] 已将旧版 DeepSeek 登录状态纳入统一加密 Provider 配置。")
    return config, save_config


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
            login_result = subprocess.run(command, check=False)
        except OSError as exc:
            print(f"登录脚本启动失败：{exc}")
            continue
        if login_result.returncode != 0:
            print("登录没有完成，本次不保存为可用 Provider。")
            continue
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
    local_entries = config.setdefault("local_providers", [])
    if not isinstance(local_entries, list):
        local_entries = []
        config["local_providers"] = local_entries
    local_entries[:] = [{"id": alias, "name": alias, "endpoint": endpoint,
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
    api_entries = config.setdefault("api_providers", [])
    if not isinstance(api_entries, list):
        api_entries = []
        config["api_providers"] = api_entries
    api_entries[:] = [{"id": alias, "name": alias, "endpoint": endpoint,
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
    terminal = _ask("Codex 终端可执行文件路径", "codex")
    try:
        max_agents = max(1, min(5, int(_ask("最多同时启动几个子 Agent", "5"))))
    except ValueError:
        max_agents = 5
    config["commander"].update({"terminal": terminal, "terminal_path": terminal,
                                 "manual_disabled": not enabled, "max_agents": max_agents})


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
        print("5. 打开本地网页配置")
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
        elif choice == "5":
            run_web_configurator()
        else:
            print("选项无效，请输入菜单中的数字。")


def test_generic_web_provider(provider_id: str, question: str = "用一句话回答：1+1等于几？",
                              timeout: int = 90) -> str:
    """用通用兜底适配器真实试发一次，返回用户可读的验证结果。"""
    try:
        from scripts.agent_relay import run_provider
    except ImportError:
        from agent_relay import run_provider
    provider_name = provider_id.removesuffix("-web")
    try:
        result = run_provider(question, "flash", provider_name=provider_name, timeout=timeout)
    except Exception as exc:
        return f"通用适配器尝试失败：{exc}"
    answer = str((result or {}).get("answer", "")).strip()
    if answer and answer not in {"未获取到有效AI回复", "提取失败"}:
        return f"通用适配器对话成功，收到回复：{answer[:120]}"
    return "网页已打开但没有取到有效回复；该网站可能不适合通用适配器，或登录状态已失效"


def run_web_configurator() -> None:
    """启动一次性本地配置中心；所有状态只在本机短暂监听。"""
    config, save_config = _load_local_config()
    stopped = threading.Event()

    def adapter_flag(provider_id: str) -> str:
        # deepseek-web 有专属适配器；其余已保存登录的网站走通用兜底。
        return "deepseek" if provider_id == "deepseek-web" else "generic"

    def public_config() -> dict:
        result = json.loads(json.dumps(config, ensure_ascii=False))
        result["config_path"] = str(TARGET_SKILL / "config" / "agentrelay-config.json.enc")
        for item in result.get("web_providers", []):
            if isinstance(item, dict) and item.get("id"):
                item["adapter"] = adapter_flag(str(item["id"]))
        for item in result.get("api_providers", []):
            if isinstance(item, dict):
                item["api_key_configured"] = bool(item.get("api_key"))
                item.pop("api_key", None)
        return result

    def provider_kind(kind: str) -> str:
        if kind not in {"web", "local", "api"}:
            raise ValueError("Provider 类型必须是 web、local 或 api")
        return f"{kind}_providers"

    def upsert_provider(kind: str, value: dict) -> dict:
        section = provider_kind(kind)
        provider_id = str(value.get("id", "")).strip().lower()
        if not _valid_provider_id(provider_id):
            raise ValueError("别名只能使用小写字母、数字和短横线")
        if kind == "web":
            url = str(value.get("base_url") or value.get("url") or "").strip()
            if not _valid_url(url):
                raise ValueError("网页地址必须是 HTTP(S) 地址")
            conversation = value.get("conversation") if isinstance(value.get("conversation"), dict) else {}
            old = next((x for x in config.get(section, []) if isinstance(x, dict) and x.get("id") == provider_id), {})
            value = {"id": provider_id, "name": str(value.get("name") or provider_id),
                     "url": url, "base_url": url, "enabled": bool(value.get("enabled", True)),
                     "adapter": "deepseek" if provider_id == "deepseek-web" else "generic",
                     # 表单保存不带 state_file；必须保留旧值，否则登录状态指针会被清空
                     "state_file": str(value.get("state_file") or old.get("state_file") or ""),
                     "conversation": conversation}
        elif kind == "local":
            endpoint = str(value.get("endpoint", "")).strip()
            if not _valid_url(endpoint) or not value.get("model"):
                raise ValueError("本地接口地址和模型名称不能为空")
            value = {"id": provider_id, "name": str(value.get("name") or provider_id), "endpoint": endpoint,
                     "model": str(value["model"]), "enabled": bool(value.get("enabled", True)),
                     "queue_timeout": max(1, min(3600, int(value.get("queue_timeout", 30))))}
        else:
            endpoint = str(value.get("endpoint", "")).strip()
            if not _valid_url(endpoint) or not value.get("model"):
                raise ValueError("API 地址和模型名称不能为空")
            old = next((x for x in config.get(section, []) if isinstance(x, dict) and x.get("id") == provider_id), {})
            api_key = str(value.get("api_key") or old.get("api_key") or "")
            value = {"id": provider_id, "name": str(value.get("name") or provider_id), "endpoint": endpoint,
                     "model": str(value["model"]), "api_key": api_key, "request_type": str(value.get("request_type", "openai-compatible")),
                     "enabled": bool(value.get("enabled", True)), "timeout": max(5, min(600, int(value.get("timeout", 60))))}
        entries = config.setdefault(section, [])
        entries[:] = [item for item in entries if not isinstance(item, dict) or item.get("id") != provider_id]
        entries.append(value)
        save_config(config, CODEX_HOME)
        return value

    def remove_provider(kind: str, provider_id: str) -> None:
        section = provider_kind(kind)
        removed = [item for item in config.get(section, []) if isinstance(item, dict) and item.get("id") == provider_id]
        config[section] = [item for item in config.get(section, []) if not isinstance(item, dict) or item.get("id") != provider_id]
        if config.get("default_provider") == provider_id:
            config["default_provider"] = next((item.get("id") for item in config.get("web_providers", []) if isinstance(item, dict) and item.get("enabled", True)), "")
        save_config(config, CODEX_HOME)
        # 手动删除条目时，它的登录状态文件和会话绑定也必须从磁盘消失
        if kind == "web":
            for item in removed:
                state_file = str(item.get("state_file") or "")
                if not state_file:
                    continue
                try:
                    path = Path(state_file).expanduser()
                    config_dir = (TARGET_SKILL / "config").resolve()
                    if path.resolve().is_relative_to(config_dir) and path.exists():
                        path.unlink()
                except OSError:
                    pass
            try:
                bindings_path = TARGET_SKILL / "agent_relay_session_bindings.json"
                if bindings_path.is_file():
                    data = json.loads(bindings_path.read_text(encoding="utf-8"))
                    providers_data = data.get("providers")
                    if isinstance(providers_data, dict):
                        dropped = providers_data.pop(provider_id, None)
                        dropped = providers_data.pop(provider_id.removesuffix("-web"), None) or dropped
                        if dropped is not None:
                            bindings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
                pass

    def legacy_html_page() -> str:
        state = json.dumps(public_config(), ensure_ascii=False).replace("</", "<\\/")
        return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>AgentRelay 配置中心</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#697386;--line:#e5e8ef;--blue:#1264d8;--bg:#f6f8fb;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif}
.shell{max-width:1180px;margin:auto;padding:30px 24px 70px}.top{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:28px;animation:fade .7s ease both}.brand{font-size:30px;letter-spacing:-.02em;font-weight:700}.sub{color:var(--muted);margin-top:5px}.pill{border:1px solid var(--line);background:#fff;border-radius:999px;padding:8px 13px;color:var(--muted)}
.layout{display:grid;grid-template-columns:210px 1fr;gap:24px}.nav{position:sticky;top:20px;height:max-content}.nav button{width:100%;text-align:left;border:0;background:transparent;border-radius:10px;padding:11px 13px;color:var(--muted);cursor:pointer;margin-bottom:3px}.nav button.active,.nav button:hover{background:#e9f1ff;color:var(--blue)}
.view{display:none}.view.active{display:block;animation:rise .45s ease both}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:0 8px 30px #24324b08;animation:rise .5s ease both}.stat{font-size:30px;font-weight:700;margin-top:8px}.label,.hint{color:var(--muted);font-size:13px}.section-title{font-size:22px;margin:0 0 5px}.section-head{display:flex;justify-content:space-between;align-items:end;margin:10px 0 16px}.provider-list{display:grid;gap:12px}.provider{display:flex;justify-content:space-between;gap:16px;align-items:center}.provider-main{min-width:0}.provider-name{font-weight:650}.provider-meta{color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.status{display:inline-flex;align-items:center;gap:6px;font-size:12px;margin-top:8px}.dot{width:7px;height:7px;border-radius:50%;background:#94a3b8}.dot.on{background:#18a65b}.dot.wait{background:#e7a51b}.actions{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.btn{border:1px solid var(--line);background:#fff;color:var(--ink);padding:8px 12px;border-radius:9px;cursor:pointer}.btn:hover{border-color:#a8bde4}.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}.btn.danger{color:#c43b48}.form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:6px}.field.full{grid-column:1/-1}.field input,.field select{border:1px solid var(--line);border-radius:9px;padding:10px;background:#fff;color:var(--ink);font:inherit}.check{display:flex;gap:8px;align-items:center}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.toast{position:fixed;right:22px;bottom:22px;background:#172033;color:#fff;padding:12px 16px;border-radius:10px;opacity:0;transform:translateY(10px);transition:.25s;pointer-events:none}.toast.show{opacity:1;transform:none}.empty{padding:25px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:12px}
@keyframes fade{from{opacity:0}to{opacity:1}}@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@media(max-width:760px){.shell{padding:20px 15px 50px}.layout{display:block}.nav{position:static;display:flex;overflow:auto;margin-bottom:20px}.nav button{white-space:nowrap;width:auto}.grid{grid-template-columns:1fr}.form{grid-template-columns:1fr}.field.full{grid-column:auto}.top{align-items:flex-start;flex-direction:column}.provider{align-items:flex-start;flex-direction:column}.actions{justify-content:flex-start}}
</style></head><body><main class='shell'><header class='top'><div><div class='brand'>AgentRelay</div><div class='sub'>本机配置中心 · 保存后即可关闭页面</div></div><span class='pill' id='mode-pill'>离线基础模式</span></header>
<div class='layout'><nav class='nav'><button class='active' data-view='overview'>总览</button><button data-view='commander'>Commander</button><button data-view='web'>线上模型</button><button data-view='local'>本地模型</button><button data-view='api'>API 模型</button><button data-view='security'>安全与配置</button></nav><section>
<div id='overview' class='view active'><div class='section-head'><div><h1 class='section-title'>现在的状态</h1><div class='hint'>把复杂配置拆成几个清楚的开关。</div></div><div class='toolbar'><button class='btn' onclick='checkEnvironment()'>检查环境</button><button class='btn' onclick='initializeFiles()'>初始化基础文件</button><button class='btn primary' onclick='closeApp()'>完成并关闭</button></div></div><div class='grid' id='stats'></div><div class='card' style='margin-top:16px'><div class='section-head'><div><h2 class='section-title' style='font-size:18px'>Provider 管理</h2><div class='hint'>已保存不等于已接入 Adapter；页面会明确标记可调用状态。</div></div></div><div id='overview-providers' class='provider-list'></div><div id='environment' class='hint' style='margin-top:16px'></div></div></div>
<div id='commander' class='view'><div class='section-head'><div><h1 class='section-title'>Commander</h1><div class='hint'>主 Agent 负责分工、审查和合并，子 Agent 最多 5 个。</div></div><button class='btn primary' onclick='saveCommander()'>保存</button></div><div class='card'><div class='form'><label class='field full check'><input id='commander-enabled' type='checkbox'> 开启审查官模式</label><label class='field full'><span>Codex 终端路径</span><input id='terminal-path' placeholder='/usr/local/bin/codex 或 codex'></label><label class='field'><span>最多同时运行几个 Agent</span><select id='max-agents'><option value='1'>1 个</option><option value='2'>2 个</option><option value='3'>3 个</option><option value='4'>4 个</option><option value='5'>5 个</option></select></label></div><p class='hint' id='commander-hint'>Commander 需要至少一个线上网页模型或本地模型。API 模型只能由主 Agent 使用，不会交给子 Agent。配置 Provider 后默认开启；你也可以手动关闭。</p></div></div>
<div id='web' class='view'><div class='section-head'><div><h1 class='section-title'>线上网页模型</h1><div class='hint'>网页登录后保存加密状态。DeepSeek 当前支持混合、极速、专家和图片。</div></div><button class='btn primary' onclick='showProviderForm("web")'>新增 Provider</button></div><div id='web-list' class='provider-list'></div><div id='web-form'></div></div>
<div id='local' class='view'><div class='section-head'><div><h1 class='section-title'>本地模型</h1><div class='hint'>Ollama、LM Studio、vLLM、llama.cpp；本地服务一次只给一个 Agent。</div></div><div class='toolbar'><button class='btn' onclick='scanLocal()'>扫描本机服务</button><button class='btn primary' onclick='showProviderForm("local")'>新增 Provider</button></div></div><div id='local-scan' class='hint'></div><div id='local-list' class='provider-list'></div><div id='local-form'></div></div>
<div id='api' class='view'><div class='section-head'><div><h1 class='section-title'>API 模型</h1><div class='hint'>OpenAI 兼容接口或中转站。密钥只保存到本机加密文件。</div></div><button class='btn primary' onclick='showProviderForm("api")'>新增 Provider</button></div><div id='api-list' class='provider-list'></div><div id='api-form'></div></div>
<div id='security' class='view'><div class='section-head'><div><h1 class='section-title'>安全与配置</h1><div class='hint'>这里不会显示 Cookie 或完整 API Key。</div></div></div><div class='card'><p>配置文件：<code id='config-path'></code></p><p>加密方式：Fernet，本机密钥权限为当前用户可读。</p><p>网页服务：只监听 127.0.0.1，点击完成后立即关闭。</p><button class='btn primary' onclick='closeApp()'>保存并关闭</button></div></div>
</section></div></main><div id='toast' class='toast'></div><script>const initial=__CONFIG__;let state=initial;
const $=id=>document.getElementById(id);function toast(msg){$('toast').textContent=msg;$('toast').classList.add('show');setTimeout(()=>$('toast').classList.remove('show'),2600)}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav button,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.view).classList.add('active')});
async function api(path,opts={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function allProviders(){return [['web',state.web_providers||[]],['local',state.local_providers||[]],['api',state.api_providers||[]]]}
function render(){const c=state.commander||{},available=(state.web_providers||[]).some(p=>p.enabled!==false)||(state.local_providers||[]).some(p=>p.enabled!==false);$('mode-pill').textContent=c.enabled?'Commander 高级模式':'离线基础模式';$('config-path').textContent=state.config_path||'本机加密目录';$('commander-enabled').checked=!!c.enabled;$('commander-enabled').disabled=!available;$('terminal-path').value=c.terminal_path||c.terminal||'codex';$('max-agents').value=c.max_agents||5;$('commander-hint').textContent=available?'已检测到线上或本地 Provider，Commander 默认开启；你可以手动关闭。最多 5 个 Agent，数量由业务自动规划。API 模型只能由主 Agent 使用。':'还没有线上或本地 Provider，Commander 暂时不可用。请先配置至少一个线上网页模型或本地模型；API 模型只能由主 Agent 使用。';const counts=allProviders().map(x=>x[1].length);$('stats').innerHTML=[['线上 Provider',counts[0]],['本地 Provider',counts[1]],['API Provider',counts[2]]].map(x=>`<div class='card'><div class='label'>${x[0]}</div><div class='stat'>${x[1]}</div></div>`).join('');$('overview-providers').innerHTML=allProviders().flatMap(([k,items])=>items.map(p=>providerCard(k,p,false))).join('')||`<div class='empty'>还没有 Provider，离线基础能力仍可使用。</div>`;['web','local','api'].forEach(k=>$(k+'-list').innerHTML=(state[k+'_providers']||[]).map(p=>providerCard(k,p,true)).join('')||`<div class='empty'>暂无配置，点击右上角新增。</div>`)}
function providerCard(kind,p,actions){const adapter=kind==='web'?(p.adapter?'可调用 Adapter':'已保存，等待 Adapter'):kind==='local'?'本地单并发':'API 仅主 Agent';const status=p.enabled!==false?'on':'wait';return `<article class='card provider'><div class='provider-main'><div class='provider-name'>${esc(p.name||p.id)} ${p.id===state.default_provider?'<span class="pill">默认</span>':''}</div><div class='provider-meta'>${esc(p.id)} · ${esc(p.base_url||p.endpoint||'')}</div><div class='status'><i class='dot ${status}'></i>${adapter} · ${p.enabled!==false?'已启用':'已停用'}</div></div>${actions?`<div class='actions'><button class='btn' onclick='testProvider("${esc(kind)}","${esc(p.id)}")'>测试</button><button class='btn' onclick='showProviderForm("${esc(kind)}","${esc(p.id)}")'>编辑</button><button class='btn' onclick='toggleProvider("${esc(kind)}","${esc(p.id)}")'>${p.enabled!==false?'停用':'启用'}</button>${kind==='web'?`<button class='btn' onclick='setDefault("${esc(p.id)}")'>设为默认</button>`:''}<button class='btn danger' onclick='deleteProvider("${esc(kind)}","${esc(p.id)}")'>删除</button></div>`:''}</article>`}
function updateModeFields(){const hybrid=$('f-hybrid')&&$('f-hybrid').checked;[['flash','f-flash-title'],['expert','f-expert-title']].forEach(([mode,id])=>{const el=$(id);if(el)el.closest('.field').style.display=(hybrid||$(('f-'+mode)).checked)?'flex':'none'})}
function showProviderForm(kind,id=''){const p=(state[kind+'_providers']||[]).find(x=>x.id===id)||{};const c=p.conversation||{};const titles=c.titles||{};const isDeepSeek=p.id==='deepseek-web';const flash=c.supports_flash??isDeepSeek,expert=c.supports_expert??isDeepSeek,hybrid=c.supports_hybrid??isDeepSeek;let body='';if(kind==='web')body=`<div class='card'><h2 class='section-title' style='font-size:18px'>${id?'编辑':'新增'}线上模型</h2><div class='form'><label class='field'><span>别名</span><input id='f-id' value='${esc(p.id)}' placeholder='deepseek-web'></label><label class='field'><span>显示名称</span><input id='f-name' value='${esc(p.name)}' placeholder='DeepSeek'></label><label class='field full'><span>对话页面网址</span><input id='f-url' value='${esc(p.base_url||p.url)}' placeholder='https://chat.deepseek.com/'></label><label class='field check'><input id='f-flash' onchange='updateModeFields()' type='checkbox' ${flash?'checked':''}> 极速模式</label><label class='field check'><input id='f-expert' onchange='updateModeFields()' type='checkbox' ${expert?'checked':''}> 专家模式</label><label class='field check'><input id='f-hybrid' onchange='updateModeFields()' type='checkbox' ${hybrid?'checked':''}> 混合模式</label><label class='field check'><input id='f-images' type='checkbox' ${c.supports_images?'checked':''}> 支持图片（公共能力）</label><label class='field'><span>极速会话名称</span><input id='f-flash-title' value='${esc(titles.flash||'AgentRelay-DeepSeek-Flash')}'></label><label class='field'><span>专家会话名称</span><input id='f-expert-title' value='${esc(titles.expert||'AgentRelay-DeepSeek-Expert')}'></label><label class='field'><span>混合会话名称</span><input id='f-hybrid-title' value='${esc(titles.hybrid||'AgentRelay-DeepSeek')}'></label></div><p class='hint'>没有目标会话时会自动创建，这是固定行为。选择混合模式时会同时配置极速和专家会话。</p><div class='toolbar'><button class='btn' onclick='loginWeb()'>打开网页登录</button><button class='btn primary' onclick='saveProvider("web")'>保存</button><button class='btn' onclick="$('web-form').innerHTML=''">取消</button></div><p class='hint'>自定义网页可以保存登录状态，但只有已实现 Adapter 的网站可以实际对话。</p></div>`;else if(kind==='local')body=`<div class='card'><h2 class='section-title' style='font-size:18px'>${id?'编辑':'新增'}本地模型</h2><div class='form'><label class='field'><span>别名</span><input id='f-id' value='${esc(p.id)}' placeholder='ollama-main'></label><label class='field'><span>显示名称</span><input id='f-name' value='${esc(p.name)}' placeholder='我的本地模型'></label><label class='field full'><span>接口地址</span><input id='f-endpoint' value='${esc(p.endpoint)}' placeholder='http://127.0.0.1:11434/v1'></label><label class='field'><span>模型名称</span><input id='f-model' value='${esc(p.model)}' placeholder='llama3.2'></label><label class='field'><span>最多等待秒数</span><input id='f-timeout' type='number' min='1' value='${p.queue_timeout||30}'></label></div><div class='toolbar'><button class='btn primary' onclick='saveProvider("local")'>保存</button><button class='btn' onclick="$('local-form').innerHTML=''">取消</button></div></div>`;else body=`<div class='card'><h2 class='section-title' style='font-size:18px'>${id?'编辑':'新增'} API 模型</h2><div class='form'><label class='field'><span>别名</span><input id='f-id' value='${esc(p.id)}' placeholder='openai-main'></label><label class='field'><span>显示名称</span><input id='f-name' value='${esc(p.name)}' placeholder='GPT API'></label><label class='field full'><span>API 地址</span><input id='f-endpoint' value='${esc(p.endpoint)}' placeholder='https://api.openai.com/v1'></label><label class='field'><span>模型名称</span><input id='f-model' value='${esc(p.model)}' placeholder='gpt-5.5'></label><label class='field'><span>请求类型</span><select id='f-type'><option value='openai-compatible'>OpenAI 兼容聊天</option><option value='custom-compatible'>自定义兼容接口</option></select></label><label class='field full'><span>API Key（留空保持原密钥）</span><input id='f-key' type='password' placeholder='不会回显旧密钥'></label></div><div class='toolbar'><button class='btn' onclick='testApiForm()'>测试接口</button><button class='btn primary' onclick='saveProvider("api")'>保存</button><button class='btn' onclick="$('api-form').innerHTML=''">取消</button></div></div>`;$(kind+'-form').innerHTML=body;if(kind==='api'&&p.request_type)$('f-type').value=p.request_type;if(kind==='web')updateModeFields()}
async function saveProvider(kind){try{const p={id:$('f-id').value,name:$('f-name').value,enabled:true};if(kind==='web'){const hybrid=$('f-hybrid').checked;Object.assign(p,{base_url:$('f-url').value,conversation:{supports_flash:$('f-flash').checked||hybrid,supports_expert:$('f-expert').checked||hybrid,supports_hybrid:hybrid,supports_images:$('f-images').checked,create_if_missing:true,titles:{flash:$('f-flash-title').value,expert:$('f-expert-title').value,hybrid:$('f-hybrid-title').value}}})}else Object.assign(p,{endpoint:$('f-endpoint').value,model:$('f-model').value});if(kind==='local')p.queue_timeout=$('f-timeout').value;if(kind==='api'){p.api_key=$('f-key').value;p.request_type=$('f-type').value}const d=await api('/api/providers',{method:'POST',body:JSON.stringify({kind,provider:p})});state=d.config;render();$(kind+'-form').innerHTML='';toast('配置已加密保存')}catch(e){toast(e.message)}}
async function saveCommander(){try{const enabled=$('commander-enabled').checked;const d=await api('/api/settings',{method:'POST',body:JSON.stringify({commander:{enabled,manual_disabled:!enabled,terminal_path:$('terminal-path').value.trim(),max_agents:Number($('max-agents').value)||5}})});state=d.config;render();toast('Commander 配置已保存')}catch(e){toast(e.message)}}
async function toggleProvider(kind,id){try{const d=await api('/api/providers/action',{method:'POST',body:JSON.stringify({kind,id,action:'toggle'})});state=d.config;render();toast('状态已更新')}catch(e){toast(e.message)}}async function setDefault(id){try{const d=await api('/api/providers/action',{method:'POST',body:JSON.stringify({kind:'web',id,action:'default'})});state=d.config;render();toast('默认 Provider 已更新')}catch(e){toast(e.message)}}async function deleteProvider(kind,id){if(!confirm('确定删除这个 Provider？'))return;try{const d=await api('/api/providers?kind='+kind+'&id='+encodeURIComponent(id),{method:'DELETE'});state=d.config;render();toast('已删除')}catch(e){toast(e.message)}}
async function testProvider(kind,id){try{const d=await api('/api/test',{method:'POST',body:JSON.stringify({kind,id})});toast(d.message)}catch(e){toast(e.message)}}async function testApiForm(){const endpoint=$('f-endpoint').value,model=$('f-model').value,key=$('f-key').value;try{const d=await api('/api/test',{method:'POST',body:JSON.stringify({kind:'api',endpoint,model,api_key:key})});toast(d.message)}catch(e){toast(e.message)}}
async function scanLocal(){try{const d=await api('/api/scan-local');$('local-scan').textContent=d.found.length?'检测到：'+d.found.map(x=>x.name+'（'+(x.models.join('、')||'未返回模型')+'）').join('，'):'没有检测到本机服务';toast('扫描完成')}catch(e){toast(e.message)}}async function checkEnvironment(){try{const d=await api('/api/environment');const failed=d.checks.filter(x=>!x.ok);$('environment').innerHTML='<strong>环境检查是确认安装器、Codex CLI、Skill 和 Hook 是否能正常工作，不是检查 Provider 登录状态。</strong><br>'+d.checks.map(x=>`${x.ok?'✓':'!'} ${esc(x.name)}：${esc(x.detail)}${x.reason?'（'+esc(x.reason)+'）':''}`).join('<br>');toast(failed.length?'未找到：'+failed.map(x=>x.name+'，'+x.reason).join('；'):'环境检查通过')}catch(e){toast(e.message)}}async function initializeFiles(){try{const d=await api('/api/initialize',{method:'POST'});toast(d.message)}catch(e){toast(e.message)}}async function loginWeb(){const hybrid=$('f-hybrid').checked;const p={id:$('f-id').value,name:$('f-name').value,base_url:$('f-url').value,conversation:{supports_flash:$('f-flash').checked||hybrid,supports_expert:$('f-expert').checked||hybrid,supports_hybrid:hybrid,supports_images:$('f-images').checked,create_if_missing:true,titles:{flash:$('f-flash-title').value,expert:$('f-expert-title').value,hybrid:$('f-hybrid-title').value}}};try{const d=await api('/api/login',{method:'POST',body:JSON.stringify({provider:p})});state=d.config;render();toast(d.message)}catch(e){toast(e.message)}}async function closeApp(){try{await api('/api/close',{method:'POST'});document.body.innerHTML='<main class="shell"><div class="card"><h1>配置已完成</h1><p>本地服务已关闭，可以安全关闭这个页面。</p></div></main>'}catch(e){toast(e.message)}}render();</script></body></html>""".replace("__CONFIG__", state)

    def html_page() -> str:
        try:
            from scripts.config_web import build_config_page
        except ImportError as exc:
            raise InstallError(f"无法加载网页配置中心：{exc}") from exc
        return build_config_page(public_config())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return
        def _body(self):
            length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
            raw = self.rfile.read(length).decode("utf-8")
            if self.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(raw or "{}")
            return {key: item[0] for key, item in parse_qs(raw).items()}
        def _send(self, payload, status=200, content_type="application/json; charset=utf-8"):
            body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status);self.send_header("Content-Type",content_type);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            from urllib.parse import parse_qs as parse_query, urlsplit
            parsed = urlsplit(self.path)
            if parsed.path == "/api/config": self._send({"config":public_config()});return
            if parsed.path == "/api/login-status":
                provider_id = parse_query(parsed.query).get("id", [""])[0]
                item = next((x for x in config.get("web_providers", []) if isinstance(x, dict) and x.get("id") == provider_id), {})
                self._send({"saved": bool(item.get("state_file") and Path(item["state_file"]).is_file())});return
            if parsed.path == "/": self._send(html_page(),content_type="text/html; charset=utf-8");return
            self._send({"error":"未找到"},404)
        def do_DELETE(self):
            try:
                from urllib.parse import parse_qs as parse_query, urlsplit
                query=parse_query(urlsplit(self.path).query);remove_provider(query.get("kind",[""])[0],query.get("id",[""])[0]);self._send({"config":public_config()})
            except Exception as exc:self._send({"error":str(exc)},400)
        def do_POST(self):
            try:
                if self.path == "/api/close": stopped.set();self._send({"ok":True});return
                if self.path == "/api/environment":
                    configured_path = str((config.get("commander") or {}).get("terminal_path") or "").strip()
                    terminal_ok = bool(shutil.which(configured_path) if configured_path and "/" not in configured_path else (Path(configured_path).is_file() if configured_path else shutil.which("codex")))
                    checks = [
                        {"name": "Python", "ok": sys.version_info >= (3, 10), "detail": sys.version.split()[0], "reason": "需要 Python 3.10 或更高版本"},
                        {"name": "Codex CLI", "ok": terminal_ok, "detail": configured_path or shutil.which("codex") or "PATH 中未找到", "reason": "Commander 需要它启动隔离子 Agent；路径不存在时无法开启 Commander"},
                        {"name": "AgentRelay 虚拟环境", "ok": VENV_PYTHON.is_file(), "detail": str(VENV_PYTHON), "reason": "网页配置和 Playwright 使用独立环境"},
                        {"name": "Skill 文件", "ok": (TARGET_SKILL / "SKILL.md").is_file(), "detail": str(TARGET_SKILL), "reason": "缺少它时 Agent 不会加载工作流"},
                        {"name": "Hook 配置", "ok": HOOKS_JSON.is_file(), "detail": str(HOOKS_JSON), "reason": "缺少它时重试、计时和提醒不会运行"},
                    ]
                    self._send({"checks": checks});return
                if self.path == "/api/initialize":
                    install_files();merge_hooks_json();self._send({"message":"基础 Skill、Hook 和脚本已重新安装"});return
                if self.path == "/api/scan-local": self._send({"found":_scan_local_services()});return
                data=self._body()
                if self.path == "/api/settings":
                    incoming=data.get("commander",{})
                    if isinstance(incoming,dict):
                        enabled=bool(incoming.get("enabled",False))
                        terminal_path=str(incoming.get("terminal_path") or incoming.get("terminal") or "codex").strip() or "codex"
                        config.setdefault("commander",{}).update({"enabled":enabled,"manual_disabled":not enabled,"terminal":terminal_path,"terminal_path":terminal_path,"max_agents":max(1,min(5,int(incoming.get("max_agents",5))))})
                    save_config(config,CODEX_HOME);self._send({"config":public_config()});return
                if self.path == "/api/providers":
                    item=upsert_provider(str(data.get("kind")),data.get("provider") or {});self._send({"provider":item,"config":public_config()});return
                if self.path == "/api/providers/action":
                    kind=str(data.get("kind"));section=provider_kind(kind);item=next(x for x in config.get(section,[]) if isinstance(x,dict) and x.get("id")==data.get("id"));action=data.get("action")
                    if action == "toggle": item["enabled"]=not bool(item.get("enabled",True))
                    elif action == "default" and kind == "web": config["default_provider"]=item["id"]
                    else: raise ValueError("不支持的操作")
                    save_config(config,CODEX_HOME);self._send({"config":public_config()});return
                if self.path == "/api/test":
                    kind=str(data.get("kind"));item=data
                    if data.get("id"): item=next(x for x in config.get(provider_kind(kind),[]) if isinstance(x,dict) and x.get("id")==data.get("id"))
                    if kind == "web":
                        state_ok=bool(item.get("state_file") and Path(item["state_file"]).exists())
                        if not state_ok:
                            self._send({"message":"尚未保存登录状态，请点击“打开网页登录”"});return
                        if str(item.get("id",""))=="deepseek-web":
                            self._send({"message":"已找到加密登录状态，DeepSeek 专属适配器可以自动对话"});return
                        self._send({"message":test_generic_web_provider(str(item.get("id","")))});return
                    ok,models,msg=_probe_models(item.get("endpoint",""),item.get("api_key","") if kind=="api" else "");self._send({"ok":ok,"models":models,"message":msg});return
                if self.path == "/api/login":
                    p=data.get("provider") or {};provider_id=str(p.get("id","")).strip().lower();url=str(p.get("base_url") or "").strip()
                    if not _valid_provider_id(provider_id) or not _valid_url(url): raise ValueError("Provider 别名或网址无效")
                    state_target=TARGET_SKILL/"config"/f"{provider_id}.storage-state.json";entry={**p,"id":provider_id,"base_url":url,"url":url,"state_file":str(state_target.with_suffix(state_target.suffix+".enc")),"enabled":True};upsert_provider("web",entry)
                    command=[str(VENV_PYTHON),str(TARGET_SKILL/"scripts"/"agent_relay_login.py"),"--provider",provider_id.removesuffix("-web"),"--url",url,"--state-file",str(state_target),"--save-on-close"]
                    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._send({"config":public_config(),"message":"网页登录窗口已打开；登录完成后关闭对话标签页即可自动保存"});return
                self._send({"error":"未找到"},404)
            except Exception as exc:self._send({"error":str(exc)},400)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    print(f"正在打开本地配置页：{url}")
    print("配置页运行中；在页面点击“完成并关闭”或按 Ctrl+C 结束。")
    webbrowser.open(url)
    interrupted = False
    try:
        while not stopped.wait(0.2):
            server.handle_request()
    except KeyboardInterrupt:
        interrupted = True
        print("\n已结束：本地服务已关闭，已保存的配置不受影响。")
    finally:
        server.server_close()
    if not interrupted:
        print("网页配置已完成，本地服务已关闭。")


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
    parser.add_argument("--no-menu", action="store_true", help="只安装文件，不进入交互配置页")
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
        if not args.no_menu and argv is None and sys.stdin.isatty():
            # 默认就是网页配置中心；终端菜单保留为明确的 --menu 备用入口。
            run_web_configurator() if not args.menu else run_chinese_menu()
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
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已结束：已保存的配置不受影响。")
        raise SystemExit(0)
