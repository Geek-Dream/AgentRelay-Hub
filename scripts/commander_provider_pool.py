"""Serialized provider leasing and recovery policy for Commander children.

The pool is deliberately separate from individual Provider adapters.  It is a
small, persistent control plane that prevents two roles from writing into the
same web conversation or exhausting one local model process at the same time.
It never calls a provider by itself.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to process-local use
    fcntl = None


WEB = "web"
LOCAL = "local"
API = "api"
_COOKIE = re.compile(r"cookie|session.?expired|unauthori[sz]ed|not.?logged|登录", re.I)
_PROXY = re.compile(r"proxy|代理|connection.?refused|tunnel", re.I)


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    kind: str
    enabled: bool = True
    login_command: str = ""
    proxy_hint: str = ""


@dataclass(frozen=True)
class ProviderNotice:
    provider_id: str
    code: str
    message: str
    login_command: str = ""
    proxy_hint: str = ""
    count: int = 0


@dataclass
class ProviderLease:
    provider_id: str
    task_id: str
    role: str
    _handle: object | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CommanderProviderCoordinator:
    """Own provider ownership, sticky role bindings, and circuit breakers.

    A lease maps to an OS file lock, so a local model or a single persistent
    web chat can only serve one Commander role at a time even if roles run in
    separate `codex exec` processes.  API providers are intentionally barred
    from Commander children to keep child fan-out from multiplying API cost.
    """

    def __init__(self, directory: str | Path, profiles: Iterable[ProviderProfile] = ()):
        self.directory = Path(directory)
        self.path = self.directory / "provider-pool.json"
        self.locks = self.directory / "locks"
        self.profiles = {item.provider_id: item for item in profiles}

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("providers", {})
                value.setdefault("bindings", {})
                value.setdefault("notifications", [])
                return value
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {"providers": {}, "bindings": {}, "notifications": []}

    def _save(self, value: dict) -> None:
        _write_json(self.path, value)

    @staticmethod
    def _append_notice(state: dict, notice: ProviderNotice) -> None:
        entries = state.setdefault("notifications", [])
        if isinstance(entries, list):
            entries.append({"at": _now(), **asdict(notice)})

    @staticmethod
    def _binding_key(task_id: str, role: str) -> str:
        return f"{task_id}:{role}"

    def _healthy(self, value: dict, provider_id: str) -> bool:
        state = value.get("providers", {}).get(provider_id, {})
        return not bool(state.get("circuit_open", False))

    def _try_lock(self, provider_id: str, task_id: str, role: str) -> ProviderLease | None:
        self.locks.mkdir(parents=True, exist_ok=True)
        handle = (self.locks / f"{provider_id}.lock").open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return ProviderLease(provider_id, task_id, role, handle)
        except (BlockingIOError, OSError):
            handle.close()
            return None

    def release(self, lease: ProviderLease | None) -> None:
        if lease is None or lease._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(lease._handle.fileno(), fcntl.LOCK_UN)
        finally:
            lease._handle.close()
            lease._handle = None

    def acquire(self, task_id: str, role: str, candidates: Iterable[str], *, depth: int = 1,
                simple_task: bool = False) -> tuple[ProviderLease | None, str | None]:
        """Lease one compatible provider, preferring the role's sticky choice."""
        if depth > 1:
            return None, "COMMANDER_RECURSION_DENIED"
        state = self._read()
        candidate_ids = [item for item in candidates if item in self.profiles]
        if simple_task:
            candidate_ids.sort(key=lambda item: self.profiles[item].kind != LOCAL)
        key = self._binding_key(task_id, role)
        sticky = state.get("bindings", {}).get(key)
        if sticky in candidate_ids:
            candidate_ids.remove(sticky)
            candidate_ids.insert(0, sticky)
        for provider_id in candidate_ids:
            profile = self.profiles[provider_id]
            if not profile.enabled or profile.kind == API or not self._healthy(state, provider_id):
                continue
            lease = self._try_lock(provider_id, task_id, role)
            if lease is None:
                continue
            state["bindings"][key] = provider_id
            details = state["providers"].setdefault(provider_id, {})
            details["last_role"] = role
            details["last_task_id"] = task_id
            details["last_leased_at"] = _now()
            self._save(state)
            return lease, None
        if any(self.profiles[item].kind == API for item in candidate_ids):
            return None, "COMMANDER_CHILD_API_DENIED"
        return None, "PROVIDER_BUSY_OR_UNAVAILABLE"

    def complete(self, lease: ProviderLease | None, *, success: bool, quality: float = 0.0,
                 error: str = "") -> ProviderNotice | None:
        if lease is None:
            return None
        try:
            state = self._read()
            provider = state["providers"].setdefault(lease.provider_id, {})
            samples = int(provider.get("quality_samples", 0))
            previous = float(provider.get("quality", 0.0))
            if success:
                provider["quality"] = round((previous * samples + max(0.0, quality)) / (samples + 1), 3)
                provider["quality_samples"] = samples + 1
                provider["success_count"] = int(provider.get("success_count", 0)) + 1
                provider["last_success_at"] = _now()
                return None
            provider["failure_count"] = int(provider.get("failure_count", 0)) + 1
            provider["last_error"] = str(error)[:500]
            provider["last_failure_at"] = _now()
            if _COOKIE.search(error) or _PROXY.search(error):
                provider["circuit_open"] = True
                provider["circuit_reason"] = "AUTH" if _COOKIE.search(error) else "PROXY"
                count = int(provider.get("notice_count", 0)) + 1
                provider["notice_count"] = count
                profile = self.profiles[lease.provider_id]
                if count <= 3:
                    code = "COOKIE_EXPIRED" if _COOKIE.search(error) else "PROXY_REQUIRED"
                    message = (f"{lease.provider_id} 登录状态失效，需要重新认证。"
                               if code == "COOKIE_EXPIRED" else
                               f"{lease.provider_id} 无法连接，可能需要代理。")
                    notice = ProviderNotice(lease.provider_id, code, message,
                                            profile.login_command, profile.proxy_hint, count)
                    self._append_notice(state, notice)
                    return notice
            return None
        finally:
            self._save(state if 'state' in locals() else self._read())
            self.release(lease)

    def mark_reauthenticated(self, provider_id: str) -> None:
        state = self._read()
        provider = state["providers"].setdefault(provider_id, {})
        provider["circuit_open"] = False
        provider["circuit_reason"] = ""
        provider["reauthenticated_at"] = _now()
        self._save(state)

    def unavailable_notice(self, provider_id: str, error: str) -> ProviderNotice | None:
        """Return a user-facing recovery hint, capped at three notices."""
        state = self._read()
        provider = state["providers"].setdefault(provider_id, {})
        count = int(provider.get("notice_count", 0))
        if count >= 3:
            return None
        count += 1
        provider["notice_count"] = count
        provider["circuit_open"] = True
        profile = self.profiles.get(provider_id, ProviderProfile(provider_id, "web"))
        code = "COOKIE_EXPIRED" if _COOKIE.search(error) else "PROVIDER_UNAVAILABLE"
        message = (f"{provider_id} 登录状态失效，需要重新认证。"
                   if code == "COOKIE_EXPIRED" else f"{provider_id} 暂时不可用。")
        notice = ProviderNotice(provider_id, code, message, profile.login_command,
                                profile.proxy_hint, count)
        self._append_notice(state, notice)
        self._save(state)
        return notice

    def choose_after_recovery(self, task_id: str, role: str, restored: str,
                              current_fallback: str) -> str:
        """Keep the better proven context after a recovered web session."""
        state = self._read()
        providers = state.get("providers", {})
        restored_quality = float(providers.get(restored, {}).get("quality", 0.0))
        fallback_quality = float(providers.get(current_fallback, {}).get("quality", 0.0))
        chosen = restored if restored_quality >= fallback_quality else current_fallback
        state["bindings"][self._binding_key(task_id, role)] = chosen
        self._save(state)
        return chosen

    def status(self) -> dict:
        state = self._read()
        return {"profiles": [asdict(profile) for profile in self.profiles.values()],
                "providers": state.get("providers", {}), "bindings": state.get("bindings", {}),
                "notifications": state.get("notifications", [])}
