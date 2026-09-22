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
        "api_router": {"port": 15731},
        "default_provider": "deepseek-web",
        "fallback_providers": [],
    }


def provider_title_stem(provider_id: str) -> str:
    """Return the human-readable CamelCase part of an automatic session title."""
    aliases = {"deepseek": "DeepSeek", "qianwen": "Qianwen", "kimi": "Kimi"}
    raw = provider_id.strip().lower().removesuffix("-web")
    parts = [part for part in raw.replace("_", "-").split("-") if part]
    return "".join(aliases.get(part, part[:1].upper() + part[1:]) for part in parts) or "Model"


def default_conversation_titles(provider_id: str) -> dict[str, str]:
    root = f"AgentRelay-{provider_title_stem(provider_id)}"
    return {"flash": f"{root}-Flash", "expert": f"{root}-Expert", "hybrid": root}


CONVERSATION_MODES = ("flash", "expert", "hybrid")

# 这些站点的网页版识图限时限量（例如 GPT 免费识图按次限量），默认不当作
# 支持图片；用户后续在配置中心显式勾选后才会启用。
KNOWN_NO_IMAGE_DEFAULTS = ("gpt", "openai", "chatgpt")


def _known_image_disabled_by_default(provider_id: str) -> bool:
    raw = provider_id.strip().lower().removesuffix("-web")
    parts = raw.replace("_", "-").split("-")
    return any(token in parts or token in raw for token in KNOWN_NO_IMAGE_DEFAULTS)


def normalize_conversation(provider_id: str, conversation: object) -> dict[str, Any]:
    """统一会话能力 schema。

    `modes` 决定启用了哪些对话模式；`images` 是**每个模式独立**的识图能力。
    混合模式表示把极速和专家合并成一套会话，因此它会取代单独勾选的极速/专家。
    旧版只有 `supports_*` 布尔字段，这里负责迁移，不改动用户的自定义会话名。
    """
    raw = conversation if isinstance(conversation, dict) else {}
    default_titles = default_conversation_titles(provider_id)
    titles = raw.get("titles")
    if not isinstance(titles, dict):
        titles = {}
    # 早期版本把 DeepSeek 的默认命名写进了所有网页 Provider，这里修掉。
    old_deepseek_titles = default_conversation_titles("deepseek-web")
    if provider_id != "deepseek-web" and all(
        str(titles.get(key) or "") == value for key, value in old_deepseek_titles.items()
    ):
        titles = {}

    is_builtin_deepseek = provider_id == "deepseek-web"
    legacy_hybrid = bool(raw.get("supports_hybrid", is_builtin_deepseek))
    legacy_flash = bool(raw.get("supports_flash", is_builtin_deepseek))
    legacy_expert = bool(raw.get("supports_expert", is_builtin_deepseek))
    legacy_images = bool(raw.get("supports_images", is_builtin_deepseek))

    raw_modes = raw.get("modes")
    if isinstance(raw_modes, (list, tuple)) and raw_modes:
        wanted = {str(item).strip().lower() for item in raw_modes}
        modes = [mode for mode in CONVERSATION_MODES if mode in wanted]
    elif legacy_hybrid:
        # 混合模式本身就等于极速 + 专家，不再重复列出单独模式。
        modes = ["hybrid"]
    else:
        modes = [mode for mode, enabled in (("flash", legacy_flash), ("expert", legacy_expert)) if enabled]
    if not modes:
        modes = ["hybrid"]

    raw_images = raw.get("images")
    images: dict[str, bool] = {}
    for mode in modes:
        if isinstance(raw_images, dict):
            images[mode] = bool(raw_images.get(mode, False))
        else:
            images[mode] = legacy_images

    # GPT 网页版的识图能力限时限量，默认按不支持处理：只有用户在配置里
    # 显式按模式勾选过图片能力（raw 里带 images 字典）时才尊重该设置，
    # 旧的 supports_images 迁移标记对这类站点不生效。
    if not isinstance(raw_images, dict) and _known_image_disabled_by_default(provider_id):
        images = {mode: False for mode in modes}

    return {
        "modes": modes,
        "images": images,
        "create_if_missing": bool(raw.get("create_if_missing", True)),
        "titles": {key: str(titles.get(key) or default_titles[key]) for key in default_titles},
        # 派生字段：旧读取方仍然能工作。
        "supports_flash": "flash" in modes,
        "supports_expert": "expert" in modes,
        "supports_hybrid": "hybrid" in modes,
        "supports_images": any(images.values()),
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
    normalized = {
        **item,
        "id": provider_id,
        "name": name,
        "url": base_url,
        "base_url": base_url,
        "enabled": bool(item.get("enabled", True)),
        "adapter": str(item.get("adapter") or ("deepseek" if provider_id == "deepseek-web" else "")),
        "conversation": normalize_conversation(provider_id, item.get("conversation")),
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


MODE_ALIASES = {"1": "flash", "2": "expert", "flash": "flash", "expert": "expert",
                "hybrid": "hybrid", "default": None, "": None}


def find_web_provider(config: Mapping[str, Any], provider_id: str) -> dict[str, Any] | None:
    """按 id 找网页 Provider，兼容 `deepseek` 与 `deepseek-web` 两种写法。"""
    wanted = {str(provider_id or "").strip().lower()}
    wanted |= {f"{item}-web" for item in list(wanted) if item and not item.endswith("-web")}
    wanted |= {item.removesuffix("-web") for item in list(wanted) if item.endswith("-web")}
    for item in config.get("web_providers", []) or []:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("id") or "").strip().lower()
        if candidate in wanted:
            return item
    return None


def resolve_conversation_mode(
    *,
    provider_id: str = "deepseek-web",
    requested: str | None = None,
    has_images: bool = False,
    attempt: int = 1,
    home: Path | None = None,
) -> tuple[str, str]:
    """按配置决定本次该走极速、专家还是混合模式。

    默认规则（按用户定稿）：

    - 同时启用了极速和专家时，默认走极速；
    - 同一个问题第二次尝试、且这次不带图片时，升级到专家；
    - 用户明确要求“问专家”走专家，“快速问一下”走极速；
    - 混合模式把极速和专家合并成一套会话，只保留识图开关；
    - 识图能力按模式分别判断，指定模式不支持图片时自动换到支持图片的模式。

    返回 ``(mode, 说明)``；说明用于让 Agent 知道是否发生了回退。
    """
    config = load_config(home)
    entry = find_web_provider(config, provider_id) or {}
    conversation = normalize_conversation(provider_id, entry.get("conversation"))
    modes = conversation["modes"]
    images = conversation["images"]
    note = ""

    normalized_request = MODE_ALIASES.get(str(requested).strip().lower(), str(requested).strip().lower()) \
        if requested is not None else None
    candidate: str | None = None
    if normalized_request in modes:
        candidate = normalized_request
    elif normalized_request in CONVERSATION_MODES:
        note = f"配置里没有启用 {normalized_request} 模式，已改用可用模式。"

    if candidate is None:
        if "flash" in modes:
            candidate = "flash"
        elif "hybrid" in modes:
            candidate = "hybrid"
        else:
            candidate = modes[0]
        # 第二次尝试且不带图片：默认极速升级到专家。
        if (not has_images and int(attempt or 1) >= 2 and candidate == "flash"
                and "expert" in modes):
            candidate = "expert"
            note = "同一问题第二次尝试，已升级到专家模式。"

    if has_images and not images.get(candidate, False):
        fallback = next((mode for mode in ("flash", "hybrid", "expert")
                         if mode in modes and images.get(mode, False)), None)
        if fallback:
            note = (note + " " if note else "") + f"{candidate} 模式不支持图片，已改用 {fallback} 模式。"
            candidate = fallback
        else:
            note = (note + " " if note else "") + "当前没有任何已启用模式支持图片，本次不能转发图片。"
    return candidate, note.strip()


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
        os.environ.setdefault("AGENTRELAY_COMMANDER_DEFAULT_PROVIDER", provider_id)

    # 每个已保存登录状态的网页 Provider 都要导出对应环境变量，
    # 否则非默认 Provider（如千问）会回落到默认 Provider 的登录状态文件。
    web_entries = config.get("web_providers")
    for item in web_entries if isinstance(web_entries, list) else []:
        if not (isinstance(item, dict) and item.get("state_file")):
            continue
        provider_id = str(item.get("id", "")).strip()
        if not provider_id:
            continue
        state_file = str(item["state_file"])
        keys = {
            provider_id.upper().replace("-", "_"),
            provider_id.removesuffix("-web").upper().replace("-", "_"),
        }
        for key in keys:
            os.environ.setdefault(f"AGENT_RELAY_{key}_LOGIN_STATE", state_file)
        if provider_id == "deepseek-web":
            os.environ.setdefault("AGENT_RELAY_LOGIN_STATE", state_file)

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
            custom_api.append({key: item[key] for key in ("id", "endpoint", "api_key", "model", "timeout", "format") if key in item})
    if custom_api:
        os.environ.setdefault("AGENTRELAY_API_PROVIDERS_JSON", json.dumps(custom_api, ensure_ascii=False))
    api_router = config.get("api_router") if isinstance(config.get("api_router"), dict) else {}
    if api_router.get("port"):
        os.environ.setdefault("AGENTRELAY_API_ROUTER_PORT", str(int(api_router["port"])))
    fallbacks = config.get("fallback_providers")
    if isinstance(fallbacks, list):
        os.environ.setdefault("AGENTRELAY_COMMANDER_FALLBACK_PROVIDERS", ",".join(map(str, fallbacks)))
    return config
