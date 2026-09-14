#!/usr/bin/env python3
"""Dispatch protocol and the default DeepSeek Expert adapter."""

from __future__ import annotations

from dataclasses import dataclass
import time
import os
import re
from pathlib import Path
from typing import Protocol

try:
    from .orchestrator_store import DispatchError, DispatchRequest, DispatchResult
except ImportError:
    from orchestrator_store import DispatchError, DispatchRequest, DispatchResult


class Provider(Protocol):
    provider_id: str

    def check_available(self) -> bool: ...
    def dispatch(self, request: DispatchRequest) -> DispatchResult: ...


class DeepSeekWebProvider:
    """Adapter over the existing Playwright implementation; Expert is read-only."""

    provider_id = "deepseek-web"

    @staticmethod
    def _safe_error(value: object) -> str:
        text = str(value)
        text = re.sub(r"(?i)(cookie|token|password|authorization|set-cookie)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
        return text[:500]

    def check_available(self) -> bool:
        try:
            from .agent_relay_runtime import provider_state_file, resolve_codex_home
        except ImportError:
            from agent_relay_runtime import provider_state_file, resolve_codex_home
        state_path = provider_state_file(self.provider_id, resolve_codex_home(), os.environ)
        return Path(state_path).is_file()

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        started = time.monotonic()
        if request.level != "expert" or not request.read_only or request.allow_file_write:
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=self.provider_id,
                                  error=DispatchError("EXPERT_READ_ONLY", "DeepSeek Expert 只读咨询，不允许修改工作区"))
        try:
            try:
                from .agent_relay import run_provider
            except ImportError:
                from agent_relay import run_provider
            # Legacy DeepSeek adapter is registered as "deepseek"; keep the
            # external orchestrator name "deepseek-web" stable.
            result = run_provider(request.prompt, "expert", provider_name="deepseek",
                                  timeout=request.timeout_seconds)
            return DispatchResult(status="success", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=self.provider_id,
                                  output=result.get("answer", ""),
                                  duration_seconds=time.monotonic() - started)
        except TimeoutError as exc:
            return DispatchResult(status="timeout", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=self.provider_id, error=DispatchError("PROVIDER_TIMEOUT", self._safe_error(exc), True),
                                  duration_seconds=time.monotonic() - started)
        except Exception as exc:
            return DispatchResult(status="failed", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=self.provider_id, error=DispatchError("PROVIDER_ERROR", self._safe_error(exc), True),
                                  duration_seconds=time.monotonic() - started)


class Dispatcher:
    def __init__(self, providers: dict[str, Provider] | None = None, *, enable_external: bool = False):
        provider = DeepSeekWebProvider()
        self.providers = dict(providers or {})
        if enable_external and not self.providers:
            self.providers.update({"deepseek-web": provider, "deepseek": provider})
        if providers and "deepseek" in providers and "deepseek-web" not in providers:
            self.providers["deepseek-web"] = providers["deepseek"]

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        if os.environ.get("AGENTRELAY_COMMANDER_CHILD") == "1" and str(request.model or "").lower() in {
            "gpt-api", "api", "openai", "openai-api"
        }:
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model,
                                  error=DispatchError("COMMANDER_CHILD_API_DENIED",
                                                       "Commander 子 Agent 不允许调用 API Provider"))
        if request.level not in {"worker", "expert"}:
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model,
                                  error=DispatchError("MODE_NOT_SUPPORTED", "当前 Dispatcher 只支持 worker 和 expert"))
        if request.depth < 0 or request.depth > 2 or request.max_calls < 1:
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model, error=DispatchError("BUDGET_EXCEEDED", "请求超出编排预算"))
        if request.level == "expert" and (not request.read_only or request.allow_file_write):
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model,
                                  error=DispatchError("EXPERT_READ_ONLY", "Expert 请求必须是只读咨询"))
        provider_id = "deepseek-web" if request.model == "deepseek" else request.model
        if not provider_id or provider_id not in self.providers:
            return DispatchResult(status="failed", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model, error=DispatchError("PROVIDER_NOT_CONFIGURED", "Provider 未配置", True))
        provider = self.providers[provider_id]
        if not provider.check_available():
            return DispatchResult(status="failed", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=provider_id, error=DispatchError("PROVIDER_UNAVAILABLE", "Provider 当前不可用", True))
        return provider.dispatch(request)
