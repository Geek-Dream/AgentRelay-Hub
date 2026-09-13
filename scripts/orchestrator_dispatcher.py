#!/usr/bin/env python3
"""Dispatch protocol and the default DeepSeek Expert adapter."""

from __future__ import annotations

from dataclasses import dataclass
import time
import os
from pathlib import Path
from typing import Protocol

try:
    from .orchestrator_store import DispatchRequest, DispatchResult
except ImportError:
    from orchestrator_store import DispatchRequest, DispatchResult


class Provider(Protocol):
    provider_id: str

    def check_available(self) -> bool: ...
    def dispatch(self, request: DispatchRequest) -> DispatchResult: ...


class DeepSeekWebProvider:
    """Adapter over the existing Playwright implementation; Expert is read-only."""

    provider_id = "deepseek-web"

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
                                  error="DeepSeek Expert 只读咨询，不允许修改工作区")
        try:
            try:
                from .agent_relay import run_provider
            except ImportError:
                from agent_relay import run_provider
            result = run_provider(request.prompt, "expert", provider_name=self.provider_id)
            return DispatchResult(status="success", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=self.provider_id,
                                  output=result.get("answer", ""),
                                  duration_seconds=time.monotonic() - started)
        except TimeoutError as exc:
            return DispatchResult(status="timeout", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=self.provider_id, error=str(exc),
                                  duration_seconds=time.monotonic() - started)
        except Exception as exc:
            return DispatchResult(status="failed", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=self.provider_id, error=str(exc),
                                  duration_seconds=time.monotonic() - started)


class Dispatcher:
    def __init__(self, providers: dict[str, Provider] | None = None):
        self.providers = providers or {"deepseek-web": DeepSeekWebProvider()}

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        if request.level not in {"worker", "expert"}:
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model,
                                  error="当前 Dispatcher 只支持 worker 和 expert")
        if request.depth < 0 or request.depth > 2 or request.max_calls < 1:
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model, error="请求超出编排预算")
        if request.level == "expert" and (not request.read_only or request.allow_file_write):
            return DispatchResult(status="rejected", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model,
                                  error="Expert 请求必须是只读咨询")
        if not request.model or request.model not in self.providers:
            return DispatchResult(status="failed", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model, error="Provider 未配置")
        provider = self.providers[request.model]
        if not provider.check_available():
            return DispatchResult(status="failed", task_id=request.task_id,
                                  request_id=request.request_id, mode=request.level,
                                  provider_id=request.model, error="Provider 当前不可用")
        return provider.dispatch(request)
