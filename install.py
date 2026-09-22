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
TARGET_IMAGE_SKILL = CODEX_HOME / "skills" / "agent-image"
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
IMAGE_SKILL_FILES = ("SKILL.md",)
IMAGE_SCRIPT_FILES = ("agent_image.py",)
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
    "agent_relay_archive.py",
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

    for filename in IMAGE_SKILL_FILES:
        install_managed_file(
            PROJECT_ROOT / "skills" / "agent-image" / filename,
            TARGET_IMAGE_SKILL / filename,
        )

    for filename in IMAGE_SCRIPT_FILES:
        install_managed_file(
            PROJECT_ROOT / "scripts" / filename,
            TARGET_IMAGE_SKILL / "scripts" / filename,
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


def _guess_vision_support(model: str) -> bool:
    """按模型名保守猜测是否支持图片输入；不确定时返回 False。"""
    name = (model or "").lower()
    markers = (
        "-vl", "vl-", "vision", "llava", "bakllava", "moondream",
        "minicpm-v", "pixtral", "internvl", "glm-4v", "qwen2-vl",
        "qwen2.5-vl", "qwen3-vl", "gemma-3-vision", "aio-vision",
    )
    return any(marker in name for marker in markers)


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
    vision_guess = _guess_vision_support(model)
    vision_hint = "模型名看起来像多模态" if vision_guess else "模型名看不出多模态能力"
    vision = _yes_no(f"该模型是否支持图片识别（{vision_hint}，agent-image 只会用支持识图的本地模型）", vision_guess)
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
        "model": model, "enabled": True, "queue_timeout": int(queue_timeout or 30),
        "vision": bool(vision)}]
    print("已保存本地模型配置。首次调用时会再次检查服务是否启动。")
    if not vision:
        print("该模型未标记图片识别能力：普通文字任务仍可使用，agent-image 不会把图片发给它。")


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


def web_provider_readiness(item: dict) -> str:
    """Check saved web configuration without sending a synthetic chat."""
    provider_id = str(item.get("id", "")).strip()
    state_file = Path(str(item.get("state_file") or "")).expanduser()
    if not state_file.is_file():
        return "尚未保存登录状态，请点击“打开网页登录”"
    if provider_id == "deepseek-web":
        return "就绪检查通过：加密登录状态已保存，DeepSeek 专属适配器已接通"
    return (
        "就绪检查通过：配置和加密登录状态已保存。"
        "本检查不会打开网页或发送测试题；真实任务会由通用网页适配器调用"
    )


def test_web_provider(item: dict, timeout: int = 600) -> str:
    """网页 Provider 实测：DeepSeek 直接就绪检查；其他网站走人工代问。

    人工代问：打开真实浏览器并跳转到已绑定会话，用户自己输入问题发送，
    工具监听按钮状态并提取回答；出现人机验证时用户手动完成并刷新页面，
    监听会继续。适合强风控网站（如千问）。
    """
    provider_id = str(item.get("id", "")).strip()
    state_file = Path(str(item.get("state_file") or "")).expanduser()
    if not state_file.is_file():
        return "尚未保存登录状态，请点击“打开网页登录”"
    if provider_id == "deepseek-web":
        return "就绪检查通过：加密登录状态已保存，DeepSeek 专属适配器已接通"
    try:
        from scripts.agent_relay import run_provider
    except ImportError:
        from agent_relay import run_provider
    provider_name = provider_id.removesuffix("-web")
    print(
        f"\n人工代问测试（{provider_id}）：请在弹出的浏览器窗口里自行输入问题并发送；"
        f"出现人机验证时手动完成并刷新页面，监听会继续。"
    )
    try:
        result = run_provider(
            "", "flash", provider_name=provider_name,
            timeout=timeout, human_input=True,
        )
    except Exception as exc:
        return f"人工代问失败：{exc}"
    answer = str((result or {}).get("answer", "")).strip()
    question = str((result or {}).get("question", "")).strip()
    if answer and answer not in {"未获取到有效AI回复", "提取失败"}:
        return f"对话完成（你问：{question[:40] or '未捕获到问题文本'}），收到回复：{answer[:120]}"
    return "已监听完一轮问答，但没有取到有效回复"


def test_api_provider(item: dict) -> str:
    """API 模型实测：chat 格式直连探测 + 真实短对话；responses/anthropic 走内置路由转换后实测。"""
    fmt = str(item.get("format") or "chat")
    if fmt == "chat":
        ok, models, msg = _probe_models(str(item.get("endpoint") or ""),
                                        str(item.get("api_key") or ""))
        if not ok:
            return msg
        # 再发一条 1 token 的真实消息，验证对话接口本身可用
        try:
            import urllib.request
            endpoint = str(item.get("endpoint") or "").rstrip("/")
            if not endpoint.endswith("/chat/completions"):
                endpoint += "/chat/completions"
            payload = {"model": str(item.get("model") or ""),
                       "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization":
                             f"Bearer {str(item.get('api_key') or '')}"},
                method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = str((data.get("choices") or [{}])[0]
                          .get("message", {}).get("content") or "")
            return f"接口可用：{msg}；实测对话返回 {len(content)} 字符"
        except Exception as exc:
            return f"模型列表可用，但对话接口调用失败：{exc}"
    try:
        from scripts.api_format_router import ensure_router
    except ImportError:
        from api_format_router import ensure_router
    pid = str(item.get("id") or "test-upstream")
    url = ensure_router([{
        "id": pid,
        "endpoint": str(item.get("endpoint") or ""),
        "api_key": str(item.get("api_key") or ""),
        "model": str(item.get("model") or pid),
        "format": fmt,
        "timeout": 120,
    }])
    payload = {"model": pid,
               "messages": [{"role": "user", "content": "用一句话回答：1+1等于几？"}]}
    import urllib.request
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=150) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    answer = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
    if not answer:
        return f"路由转换失败：{data}"
    return f"路由转换成功（{fmt} 上游），收到回复：{answer[:100]}"


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
            fmt = str(value.get("format") or "chat")
            if fmt not in ("chat", "responses", "anthropic"):
                fmt = "chat"
            value = {"id": provider_id, "name": str(value.get("name") or provider_id), "endpoint": endpoint,
                     "model": str(value["model"]), "api_key": api_key, "request_type": str(value.get("request_type", "openai-compatible")),
                     "format": fmt,
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
                    router_incoming=data.get("api_router",{})
                    if isinstance(router_incoming,dict) and router_incoming.get("port"):
                        config["api_router"]={"port":max(1024,min(65535,int(router_incoming["port"])))}
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
                        self._send({"message":test_web_provider(item)});return
                    if kind == "api":
                        self._send({"message":test_api_provider(item)});return
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
