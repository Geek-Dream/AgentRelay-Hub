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
import time
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
                value.setdefault("comparisons", [])
                value.setdefault("recoveries", {})
                value.setdefault("queues", {})
                return value
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {"providers": {}, "bindings": {}, "notifications": [], "comparisons": [], "recoveries": {}, "queues": {}}

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

    @staticmethod
    def _role_binding_key(role: str) -> str:
        return f"role:{role}"

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

    @staticmethod
    def _queue_key(task_id: str, role: str) -> str:
        return f"{task_id}:{role}"

    def _register_waiter(self, state: dict, provider_id: str, waiter: str) -> bool:
        """Reserve at most one waiting slot per Provider."""
        queues = state.setdefault("queues", {})
        entries = queues.setdefault(provider_id, [])
        if waiter in entries:
            return True
        if entries:
            return False
        entries.append(waiter)
        return True

    def _remove_waiter(self, state: dict, provider_id: str, waiter: str) -> None:
        entries = state.setdefault("queues", {}).get(provider_id, [])
        if waiter in entries:
            entries.remove(waiter)
        if not entries:
            state.setdefault("queues", {}).pop(provider_id, None)

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
                simple_task: bool = False, wait_timeout: float = 0,
                poll_interval: float = 0.1, preferred_provider: str | None = None) -> tuple[ProviderLease | None, str | None]:
        """Lease one provider, remembering the best provider for this role.

        A busy local model is treated as a queueable resource.  Callers may
        wait for it briefly; after that they can retry with their web fallback.
        The default remains non-blocking for existing integrations.
        """
        if depth > 1:
            return None, "COMMANDER_RECURSION_DENIED"
        candidate_ids = [item for item in candidates if item in self.profiles]
        if simple_task:
            candidate_ids.sort(key=lambda item: self.profiles[item].kind != LOCAL)
        key = self._binding_key(task_id, role)
        waiter = self._queue_key(task_id, role)
        deadline = time.monotonic() + max(0.0, float(wait_timeout))
        waiting_provider: str | None = None
        while True:
            state = self._read()
            sticky = (preferred_provider
                      or state.get("bindings", {}).get(key)
                      or state.get("bindings", {}).get(self._role_binding_key(role)))
            ordered = list(candidate_ids)
            if sticky in ordered:
                ordered.remove(sticky)
                ordered.insert(0, sticky)
            local_wait_expired = wait_timeout > 0 and time.monotonic() >= deadline
            busy_candidates = []
            for provider_id in ordered:
                profile = self.profiles[provider_id]
                if not profile.enabled or profile.kind == API or not self._healthy(state, provider_id):
                    continue
                if profile.kind == LOCAL and local_wait_expired:
                    continue
                lease = self._try_lock(provider_id, task_id, role)
                if lease is None:
                    busy_candidates.append(provider_id)
                    continue
                for queued_provider in list(state.get("queues", {})):
                    self._remove_waiter(state, queued_provider, waiter)
                state["bindings"][key] = provider_id
                state["bindings"][self._role_binding_key(role)] = provider_id
                details = state["providers"].setdefault(provider_id, {})
                details["last_role"] = role
                details["last_task_id"] = task_id
                details["last_leased_at"] = _now()
                self._save(state)
                return lease, None
            # Never wait on a busy local model while another candidate is free:
            # the loop above already tried every candidate.  If all are busy,
            # reserve one shared queue slot and wait briefly for that resource.
            if busy_candidates and time.monotonic() < deadline:
                target = waiting_provider
                if target not in busy_candidates:
                    target = next((item for item in busy_candidates
                                   if self._register_waiter(state, item, waiter)), None)
                    if target:
                        waiting_provider = target
                        self._save(state)
                if target:
                    time.sleep(max(0.01, float(poll_interval)))
                    continue
            if waiting_provider:
                self._remove_waiter(state, waiting_provider, waiter)
                self._save(state)
                waiting_provider = None
            # A simple local task may fall through to an online candidate; a
            # caller with no candidates receives the same stable error below.
            break
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
                              current_fallback: str, *, prompt: str = "",
                              probe=None, judge=None, context_summary: str = "") -> str:
        """Choose a recovered provider, optionally by testing both models.

        ``probe(provider_id, prompt)`` performs the real provider request and
        ``judge(outputs)`` returns numeric quality scores.  The callbacks stay
        outside this control plane so the pool never spends API tokens or
        opens browser sessions by itself.  A small persistent handoff summary
        lets the selected provider continue with the useful context.
        """
        state = self._read()
        providers = state.get("providers", {})
        outputs = {}
        scores = {}
        if probe is not None and prompt:
            for provider_id in (restored, current_fallback):
                try:
                    outputs[provider_id] = str(probe(provider_id, prompt))[:4000]
                except Exception as exc:
                    outputs[provider_id] = f"[probe failed] {str(exc)[:300]}"
            if judge is not None:
                try:
                    judged = judge(dict(outputs))
                    if isinstance(judged, dict):
                        scores = {str(key): max(0.0, min(1.0, float(value)))
                                  for key, value in judged.items()}
                except (TypeError, ValueError):
                    scores = {}
            if not scores:
                # Only a last-resort signal; production callers should provide
                # the main Agent's semantic judge rather than trust length.
                scores = {provider_id: min(1.0, len(value) / 1200.0)
                          for provider_id, value in outputs.items()}
            for provider_id, score in scores.items():
                details = providers.setdefault(provider_id, {})
                samples = int(details.get("quality_samples", 0))
                previous = float(details.get("quality", 0.0))
                details["quality"] = round((previous * samples + score) / (samples + 1), 3)
                details["quality_samples"] = samples + 1
        restored_quality = scores.get(restored, float(providers.get(restored, {}).get("quality", 0.0)))
        fallback_quality = scores.get(current_fallback, float(providers.get(current_fallback, {}).get("quality", 0.0)))
        chosen = restored if restored_quality >= fallback_quality else current_fallback
        state["bindings"][self._binding_key(task_id, role)] = chosen
        state["bindings"][self._role_binding_key(role)] = chosen
        if outputs or context_summary:
            state.setdefault("comparisons", []).append({
                "at": _now(), "task_id": task_id, "role": role,
                "restored": restored, "fallback": current_fallback,
                "selected": chosen, "scores": scores,
                "outputs": outputs,
                "context_handoff": str(context_summary)[:4000],
            })
        self._save(state)
        return chosen

    @staticmethod
    def _recovery_key(task_id: str, role: str) -> str:
        return f"{task_id}:{role}"

    def record_fallback(self, task_id: str, role: str, restored: str,
                        fallback: str, prompt: str, output: str,
                        *, context_summary: str = "") -> None:
        """Remember the useful context produced while a preferred Provider was down.

        The summary is deliberately supplied by the caller (or generated from
        the captured answer), so recovery never needs a throwaway model call.
        """
        state = self._read()
        summary = str(context_summary).strip()
        if not summary:
            summary = f"{fallback} 在 {restored} 熔断期间处理了原任务，回答摘要：{str(output).strip()[:800]}"
        state.setdefault("recoveries", {})[self._recovery_key(task_id, role)] = {
            "restored": restored, "fallback": fallback,
            "prompt": str(prompt)[:2000], "fallback_output": str(output)[:4000],
            "context_summary": summary[:4000],
            "recorded_at": _now(), "status": "pending",
        }
        self._save(state)

    def pending_recovery(self, task_id: str, role: str, restored: str) -> dict | None:
        value = self._read().get("recoveries", {}).get(self._recovery_key(task_id, role))
        if not isinstance(value, dict) or value.get("status") != "pending":
            return None
        return value if value.get("restored") == restored else None

    @staticmethod
    def _default_quality_scores(outputs: dict[str, str], prompt: str) -> dict[str, float]:
        """Conservative offline rubric for obvious useful-vs-nonsense replies."""
        words = {item.casefold() for item in re.findall(r"[A-Za-z0-9_+#.-]+|[\u4e00-\u9fff]{2,}", prompt)}
        scores = {}
        for provider_id, value in outputs.items():
            text = str(value).strip()
            lowered = text.casefold()
            score = 0.15
            if 80 <= len(text) <= 5000:
                score += 0.25
            if any(token in lowered for token in ("步骤", "配置", "原因", "验证", "example", "command", "redis", "代码")):
                score += 0.25
            if words:
                score += min(0.25, len(words & set(re.findall(r"[A-Za-z0-9_+#.-]+|[\u4e00-\u9fff]{2,}", lowered))) / max(1, len(words)) * 0.25)
            if any(token in lowered for token in ("无法回答", "不知道", "作为 ai", "抱歉")):
                score -= 0.2
            scores[provider_id] = max(0.0, min(1.0, round(score, 3)))
        return scores

    def compare_outputs(self, task_id: str, role: str, restored: str,
                        fallback: str, prompt: str, outputs: dict[str, str],
                        *, judge=None, context_summary: str = "") -> dict:
        """Score two Provider answers and persist the handoff decision."""
        scores = {}
        if judge is not None:
            try:
                value = judge(dict(outputs))
                if isinstance(value, dict):
                    scores = {str(key): max(0.0, min(1.0, float(item)))
                              for key, item in value.items()}
            except (TypeError, ValueError):
                scores = {}
        if not scores:
            scores = self._default_quality_scores(outputs, prompt)
        state = self._read()
        providers = state.setdefault("providers", {})
        for provider_id, score in scores.items():
            details = providers.setdefault(provider_id, {})
            samples = int(details.get("quality_samples", 0))
            previous = float(details.get("quality", 0.0))
            details["quality"] = round((previous * samples + score) / (samples + 1), 3)
            details["quality_samples"] = samples + 1
        chosen = restored if scores.get(restored, 0.0) >= scores.get(fallback, 0.0) else fallback
        key = self._recovery_key(task_id, role)
        state.setdefault("recoveries", {}).setdefault(key, {})["status"] = "compared"
        state["bindings"][self._binding_key(task_id, role)] = chosen
        state["bindings"][self._role_binding_key(role)] = chosen
        comparison = {"at": _now(), "task_id": task_id, "role": role,
                      "restored": restored, "fallback": fallback,
                      "selected": chosen, "scores": scores,
                      "outputs": {key: str(value)[:4000] for key, value in outputs.items()},
                      "context_handoff": str(context_summary)[:4000]}
        state.setdefault("comparisons", []).append(comparison)
        self._save(state)
        return comparison

    def status(self) -> dict:
        state = self._read()
        return {"profiles": [asdict(profile) for profile in self.profiles.values()],
                "providers": state.get("providers", {}), "bindings": state.get("bindings", {}),
                "notifications": state.get("notifications", []),
                "comparisons": state.get("comparisons", []),
                "recoveries": state.get("recoveries", {}),
                "queues": state.get("queues", {})}
