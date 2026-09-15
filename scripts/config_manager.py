"""AgentRelay 本地配置与加密存储。

配置只保存在用户的 CODEX_HOME，不写入项目目录。加密密钥也只保存在同一
用户目录并限制为当前用户可读；运行时仅把需要的值放入当前 Python 进程环境。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - installer reports a clear dependency error
    Fernet = None
    InvalidToken = Exception


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def config_directory(home: Path | None = None) -> Path:
    directory = (home or codex_home()) / "skills" / "agent-relay" / "config"
    return directory


def config_path(home: Path | None = None) -> Path:
    return config_directory(home) / "agentrelay-config.json.enc"


def key_path(home: Path | None = None) -> Path:
    return config_directory(home) / ".agentrelay-config.key"


def _secure_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _fernet(home: Path | None = None, *, create: bool = False):
    if Fernet is None:
        raise RuntimeError("缺少 cryptography 依赖，请重新运行安装脚本")
    path = key_path(home)
    if not path.exists():
        if not create:
            return None
        _secure_write(path, Fernet.generate_key())
    key = path.read_bytes().strip()
    return Fernet(key)


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "offline",
        "commander": {
            "enabled": False,
            "terminal": "codex",
            "command": "codex exec",
            "max_agents": 5,
        },
        "web_providers": [],
        "local_providers": [],
        "api_providers": [],
        "default_provider": "deepseek-web",
        "fallback_providers": [],
    }


def load_config(home: Path | None = None) -> dict[str, Any]:
    path = config_path(home)
    if not path.exists():
        return default_config()
    try:
        cipher = _fernet(home)
        if cipher is None:
            return default_config()
        value = json.loads(cipher.decrypt(path.read_bytes()).decode("utf-8"))
        if not isinstance(value, dict):
            return default_config()
    except (OSError, ValueError, TypeError, InvalidToken, json.JSONDecodeError):
        return default_config()
    merged = default_config()
    merged.update(value)
    for section in ("commander", "web_providers", "local_providers", "api_providers"):
        if section == "commander":
            value_section = value.get(section)
            if isinstance(value_section, dict):
                merged[section] = {**default_config()[section], **value_section}
        elif not isinstance(merged.get(section), list):
            merged[section] = []
    return merged


def save_config(value: Mapping[str, Any], home: Path | None = None) -> Path:
    cipher = _fernet(home, create=True)
    payload = json.dumps(dict(value), ensure_ascii=False, indent=2).encode("utf-8")
    path = config_path(home)
    _secure_write(path, cipher.encrypt(payload))
    return path


def encrypt_secret_bytes(value: bytes, home: Path | None = None) -> bytes:
    return _fernet(home, create=True).encrypt(value)


def decrypt_secret_bytes(value: bytes, home: Path | None = None) -> bytes:
    return _fernet(home).decrypt(value)


def update_config(changes: Mapping[str, Any], home: Path | None = None) -> dict[str, Any]:
    current = load_config(home)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    save_config(current, home)
    return current


def _first(items: object) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("enabled", True):
            return item
    return None


def _selected(items: object, preferred: str = "") -> dict[str, Any] | None:
    if isinstance(items, list) and preferred:
        for item in items:
            if isinstance(item, dict) and item.get("enabled", True) and str(item.get("id", "")) == preferred:
                return item
    return _first(items)


def apply_config_to_environment(home: Path | None = None) -> dict[str, Any]:
    """把加密配置映射到当前进程，显式环境变量优先。"""
    config = load_config(home)
    commander = config.get("commander", {})
    if commander.get("enabled"):
        os.environ.setdefault("AGENTRELAY_COMMANDER_ENABLED", "1")
        os.environ.setdefault("AGENTRELAY_COMMANDER_TERMINAL", str(commander.get("terminal", "codex")))
        os.environ.setdefault("AGENTRELAY_COMMANDER_COMMAND", str(commander.get("command", "codex exec")))
        os.environ.setdefault("AGENTRELAY_COMMANDER_MAX_AGENTS", str(commander.get("max_agents", 5)))

    web = _selected(config.get("web_providers"), str(config.get("default_provider", "")))
    if web:
        provider_id = str(web.get("id", "deepseek-web"))
        os.environ.setdefault("AGENT_RELAY_PROVIDER", provider_id.removesuffix("-web"))
        if provider_id == "deepseek-web":
            os.environ.setdefault("AGENTRELAY_DEEPSEEK_ENABLED", "1")
            os.environ.setdefault("AGENTRELAY_DEEPSEEK_MODE", "playwright")
            if web.get("state_file"):
                os.environ.setdefault("AGENT_RELAY_LOGIN_STATE", str(web["state_file"]))
        elif web.get("state_file"):
            key = provider_id.upper().replace("-", "_")
            os.environ.setdefault(f"AGENT_RELAY_{key}_LOGIN_STATE", str(web["state_file"]))
        os.environ.setdefault("AGENTRELAY_COMMANDER_DEFAULT_PROVIDER", provider_id)

    local = _first(config.get("local_providers"))
    if local and local.get("endpoint"):
        os.environ.setdefault("AGENTRELAY_LOCAL_LLM_ENDPOINT", str(local["endpoint"]))
        os.environ.setdefault("AGENTRELAY_LOCAL_LLM_MODEL", str(local.get("model", "")))
        os.environ.setdefault("AGENTRELAY_LOCAL_QUEUE_TIMEOUT", str(local.get("queue_timeout", 30)))

    api = _first(config.get("api_providers"))
    if api and api.get("endpoint") and api.get("api_key"):
        os.environ.setdefault("AGENTRELAY_API_ENDPOINT", str(api["endpoint"]))
        os.environ.setdefault("AGENTRELAY_API_KEY", str(api["api_key"]))
        os.environ.setdefault("AGENTRELAY_API_MODEL", str(api.get("model", "")))

    custom_api = []
    for item in config.get("api_providers", []) if isinstance(config.get("api_providers"), list) else []:
        if isinstance(item, dict) and item.get("id") and item.get("endpoint") and item.get("api_key"):
            custom_api.append({key: item[key] for key in ("id", "endpoint", "api_key", "model", "timeout") if key in item})
    if custom_api:
        os.environ.setdefault("AGENTRELAY_API_PROVIDERS_JSON", json.dumps(custom_api, ensure_ascii=False))
    fallbacks = config.get("fallback_providers")
    if isinstance(fallbacks, list):
        os.environ.setdefault("AGENTRELAY_COMMANDER_FALLBACK_PROVIDERS", ",".join(map(str, fallbacks)))
    return config
