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


def legacy_deepseek_state_path(home: Path | None = None) -> Path:
    """旧版本直接放在 Skill 根目录的 DeepSeek 浏览器状态。"""
    return (home or codex_home()) / "skills" / "agent-relay" / "agent_relay_login_state.json"


def unified_deepseek_state_path(home: Path | None = None) -> Path:
    """统一 Provider 配置目录中的加密 DeepSeek 浏览器状态。"""
    return config_directory(home) / "deepseek-web.storage-state.json.enc"


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
            "terminal_path": "codex",
            "max_agents": 5,
            "manual_disabled": False,
        },
        "web_providers": [],
        "local_providers": [],
        "api_providers": [],
        "default_provider": "deepseek-web",
        "fallback_providers": [],
    }


def _normalize_web_provider(item: object) -> dict[str, Any] | None:
    """补齐网页 Provider 的统一 schema，并兼容旧版 url 字段。"""
    if not isinstance(item, dict):
        return None
    provider_id = str(item.get("id", "")).strip().lower()
    if not provider_id:
        return None
    name = str(item.get("name") or provider_id)
    base_url = str(item.get("base_url") or item.get("url") or "").strip()
    conversation = item.get("conversation")
    if not isinstance(conversation, dict):
        conversation = {}
    default_titles = {
        "flash": "AgentRelay-DeepSeek-Flash",
        "expert": "AgentRelay-DeepSeek-Expert",
        "hybrid": "AgentRelay-DeepSeek",
    }
    titles = conversation.get("titles")
    if not isinstance(titles, dict):
        titles = {}
    normalized = {
        **item,
        "id": provider_id,
        "name": name,
        "url": base_url,
        "base_url": base_url,
        "enabled": bool(item.get("enabled", True)),
        "adapter": str(item.get("adapter") or ("deepseek" if provider_id == "deepseek-web" else "")),
        "conversation": {
            "supports_flash": bool(conversation.get("supports_flash", provider_id == "deepseek-web")),
            "supports_expert": bool(conversation.get("supports_expert", provider_id == "deepseek-web")),
            "supports_hybrid": bool(conversation.get("supports_hybrid", provider_id == "deepseek-web")),
            "supports_images": bool(conversation.get("supports_images", provider_id == "deepseek-web")),
            "create_if_missing": bool(conversation.get("create_if_missing", True)),
            "titles": {key: str(titles.get(key) or default_titles[key]) for key in default_titles},
        },
    }
    return normalized


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
    merged["web_providers"] = [
        normalized for item in merged.get("web_providers", [])
        if (normalized := _normalize_web_provider(item)) is not None
    ]
    commander = merged["commander"]
    if not commander.get("terminal_path"):
        commander["terminal_path"] = commander.get("terminal", "codex")
    if "manual_disabled" not in commander:
        commander["manual_disabled"] = False
    # 新配置中只要存在网页或本地 Provider，Commander 默认可用；用户手动关闭后保持关闭。
    if (merged["web_providers"] or merged["local_providers"]) and not commander.get("manual_disabled"):
        commander["enabled"] = True
    if not (merged["web_providers"] or merged["local_providers"]):
        commander["enabled"] = False
    return merged


def save_config(value: Mapping[str, Any], home: Path | None = None) -> Path:
    cipher = _fernet(home, create=True)
    normalized = dict(value)
    normalized["web_providers"] = [
        item for raw in normalized.get("web_providers", [])
        if (item := _normalize_web_provider(raw)) is not None
    ]
    commander = normalized.get("commander")
    if isinstance(commander, dict):
        commander["terminal_path"] = str(commander.get("terminal_path") or commander.get("terminal") or "codex")
        commander["terminal"] = commander["terminal_path"]
        commander["max_agents"] = max(1, min(5, int(commander.get("max_agents", 5) or 5)))
        commander["manual_disabled"] = bool(commander.get("manual_disabled", False))
        if normalized.get("web_providers") or normalized.get("local_providers"):
            if not commander["manual_disabled"]:
                commander["enabled"] = True
        else:
            commander["enabled"] = False
    payload = json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8")
    path = config_path(home)
    _secure_write(path, cipher.encrypt(payload))
    return path


def migrate_legacy_deepseek_config(home: Path | None = None) -> tuple[dict[str, Any], bool]:
    """将旧版 DeepSeek 登录状态迁移进统一 Provider 配置。

    旧版只保存根目录下的浏览器状态，因此网页配置中心无法识别它。迁移成功后
    旧明文状态会删除；任何解密、加密或写入失败都会保留原文件。
    """
    root = home or codex_home()
    config = load_config(root)
    legacy_raw = legacy_deepseek_state_path(root)
    legacy_encrypted = legacy_raw.with_suffix(legacy_raw.suffix + ".enc")
    target = unified_deepseek_state_path(root)
    changed = False

    if not target.exists() and legacy_raw.is_file():
        _secure_write(target, encrypt_secret_bytes(legacy_raw.read_bytes(), root))
        # Only remove the plaintext after the encrypted replacement is durable.
        legacy_raw.unlink()
        changed = True
    elif not target.exists() and legacy_encrypted.is_file():
        # Re-encrypt instead of copying opaque ciphertext so the new location is
        # always tied to the current unified configuration key.
        decrypted = decrypt_secret_bytes(legacy_encrypted.read_bytes(), root)
        _secure_write(target, encrypt_secret_bytes(decrypted, root))
        legacy_encrypted.unlink()
        changed = True

    providers = config.setdefault("web_providers", [])
    has_deepseek = any(
        isinstance(item, dict) and item.get("id") == "deepseek-web"
        for item in providers
    )
    if target.is_file() and not has_deepseek:
        providers.append({
            "id": "deepseek-web",
            "name": "DeepSeek",
            "url": "https://chat.deepseek.com/",
            "base_url": "https://chat.deepseek.com/",
            "enabled": True,
            "adapter": "deepseek",
            "state_file": str(target),
            "conversation": {
                "supports_flash": True,
                "supports_expert": True,
                "supports_hybrid": True,
                "supports_images": True,
                "create_if_missing": True,
                "titles": {
                    "flash": "AgentRelay-DeepSeek-Flash",
                    "expert": "AgentRelay-DeepSeek-Expert",
                    "hybrid": "AgentRelay-DeepSeek",
                },
            },
        })
        config["default_provider"] = "deepseek-web"
        changed = True

    if changed:
        save_config(config, root)
        config = load_config(root)
    return config, changed


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
    config, _ = migrate_legacy_deepseek_config(home)
    commander = config.get("commander", {})
    if commander.get("enabled"):
        os.environ.setdefault("AGENTRELAY_COMMANDER_ENABLED", "1")
        terminal_path = str(commander.get("terminal_path") or commander.get("terminal", "codex"))
        os.environ.setdefault("AGENTRELAY_COMMANDER_TERMINAL", terminal_path)
        os.environ.setdefault("AGENTRELAY_COMMANDER_TERMINAL_PATH", terminal_path)
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
