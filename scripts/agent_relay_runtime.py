#!/usr/bin/env python3
"""Provider-neutral runtime models and state for AgentRelay."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Mapping, MutableMapping, Optional
from urllib.parse import urlparse


_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def normalize_provider_name(value: str) -> str:
    name = str(value or "").strip().lower().replace("_", "-")
    if not _PROVIDER_NAME.fullmatch(name):
        raise ValueError(f"无效的 Provider 名称：{value!r}")
    return name


def resolve_codex_home(
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def venv_python(codex_home: Path, os_name: Optional[str] = None) -> Path:
    platform_name = os.name if os_name is None else os_name
    if platform_name == "nt":
        return codex_home / "agentrelay-env" / "Scripts" / "python.exe"
    return codex_home / "agentrelay-env" / "bin" / "python"


def provider_state_file(
    provider: str,
    codex_home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return a Provider-specific storage-state path with legacy compatibility."""

    env = os.environ if environ is None else environ
    provider_name = normalize_provider_name(provider)
    provider_key = provider_name.upper().replace("-", "_")
    explicit = env.get(f"AGENT_RELAY_{provider_key}_LOGIN_STATE")
    if not explicit:
        explicit = env.get("AGENT_RELAY_LOGIN_STATE")
    if explicit:
        return Path(os.path.expanduser(explicit))

    skill_root = codex_home / "skills" / "agent-relay"
    if provider_name in {"deepseek", "deepseek-web"}:
        return skill_root / "agent_relay_login_state.json"
    return skill_root / f"agent_relay_{provider_name}_login_state.json"


def session_bindings_file(
    codex_home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("AGENT_RELAY_SESSION_BINDINGS")
    if explicit:
        return Path(os.path.expanduser(explicit))
    return (
        codex_home
        / "skills"
        / "agent-relay"
        / "agent_relay_session_bindings.json"
    )


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_provider_name(self.name))
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"无效的 Provider URL：{self.base_url!r}")


PROVIDER_SPECS = {
    "deepseek": ProviderSpec(
        name="deepseek",
        base_url="https://chat.deepseek.com/",
    ),
}


def provider_spec(provider: str) -> ProviderSpec:
    provider_name = normalize_provider_name(provider)
    spec = PROVIDER_SPECS.get(provider_name)
    if spec is None:
        supported = ", ".join(sorted(PROVIDER_SPECS))
        raise ValueError(
            f"尚未配置 Provider：{provider_name}；当前支持：{supported}"
        )
    return spec


@dataclass(frozen=True)
class SessionRef:
    provider: str
    session_id: str
    title: str
    href: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", normalize_provider_name(self.provider))
        if not str(self.session_id).strip():
            raise ValueError("session_id 不能为空")
        parsed = urlparse(self.href)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"会话 href 必须是绝对 HTTP(S) URL：{self.href!r}")

    def to_mapping(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "href": self.href,
        }

    @classmethod
    def from_mapping(
        cls,
        provider: str,
        value: object,
    ) -> Optional["SessionRef"]:
        if not isinstance(value, Mapping):
            return None
        try:
            return cls(
                provider=provider,
                session_id=str(value["session_id"]),
                title=str(value.get("title", "")),
                href=str(value["href"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class SessionBindingStore:
    """Persist mode-to-session bindings; malformed state degrades to empty."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> MutableMapping[str, object]:
        if not self.path.is_file():
            return {"version": self.SCHEMA_VERSION, "providers": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"version": self.SCHEMA_VERSION, "providers": {}}
        if not isinstance(value, dict) or not isinstance(
            value.get("providers"), dict
        ):
            return {"version": self.SCHEMA_VERSION, "providers": {}}
        return value

    def get(self, provider: str, mode: str) -> Optional[SessionRef]:
        provider_name = normalize_provider_name(provider)
        value = self._load()
        providers = value.get("providers", {})
        provider_value = providers.get(provider_name, {})
        if not isinstance(provider_value, Mapping):
            return None
        return SessionRef.from_mapping(provider_name, provider_value.get(mode))

    def put(self, mode: str, session: SessionRef) -> None:
        value = self._load()
        providers = value.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            value["providers"] = providers
        provider_value = providers.setdefault(session.provider, {})
        if not isinstance(provider_value, dict):
            provider_value = {}
            providers[session.provider] = provider_value
        provider_value[mode] = session.to_mapping()
        value["version"] = self.SCHEMA_VERSION
        _atomic_write_json(self.path, value)

    def remove(self, provider: str, mode: str) -> None:
        provider_name = normalize_provider_name(provider)
        value = self._load()
        providers = value.get("providers", {})
        provider_value = providers.get(provider_name, {})
        if not isinstance(provider_value, dict) or mode not in provider_value:
            return
        del provider_value[mode]
        _atomic_write_json(self.path, value)
