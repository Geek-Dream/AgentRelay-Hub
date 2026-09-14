"""Offline agent capability and execution abstractions."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from dataclasses import dataclass
import json
try:
    from .agent_adapter import AgentAdapter, DeepSeekAdapter, PromptTemplate
except ImportError:
    from agent_adapter import AgentAdapter, DeepSeekAdapter, PromptTemplate

@dataclass(frozen=True)
class AgentResponse:
    success: bool
    content: str = ""
    structured_data: Any = None
    error: str | None = None


def _parse_structured_content(content: str) -> Any:
    value = str(content).strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.lower().startswith("json"):
            value = value[4:].lstrip()
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


class AgentProvider(ABC):
    @abstractmethod
    def analyze(self, task_context: Any) -> Any: ...

    @abstractmethod
    def plan(self, task_context: Any) -> Any: ...

    @abstractmethod
    def explain(self, task_context: Any) -> AgentResponse: ...


class LocalAgentProvider(AgentProvider):
    """Rule-based provider; deliberately independent of external models."""
    def analyze(self, task_context):
        try: from .task_scheduler import TaskAnalyzer
        except ImportError: from task_scheduler import TaskAnalyzer
        plan = TaskAnalyzer().analyze(task_context.user_request)
        hits = getattr(task_context, "memory_hits", ()) or ()
        if hits:
            return AgentResponse(True, f"{plan.reason}；发现 {len(hits)} 条历史经验，优先参考已验证方案", {
                "task_plan": plan, "memory_hits": list(hits),
            })
        return AgentResponse(True, plan.reason, plan)

    def plan(self, task_context):
        hits = getattr(task_context, "memory_hits", ()) or ()
        plan = {
            "task_id": task_context.task_id,
            "request": task_context.user_request,
            "allowed_files": list(task_context.requirement_card.get("affected_files", []))
            if isinstance(task_context.requirement_card, dict) else [],
            "memory_hits": list(hits),
        }
        return AgentResponse(True, "本地规则生成执行方案", plan)

    def explain(self, task_context) -> str:
        card = task_context.requirement_card or {}
        return AgentResponse(True, str(card.get("plain_explanation") or task_context.user_request))


class LocalLLMProvider(AgentProvider):
    """Optional local model provider; rules remain the fallback."""
    def __init__(self, adapter=None, fallback=None):
        try: from .agent_adapter import OpenAICompatibleAdapter, PromptTemplate
        except ImportError: from agent_adapter import OpenAICompatibleAdapter, PromptTemplate
        self.adapter = adapter or OpenAICompatibleAdapter(); self.fallback = fallback or LocalAgentProvider(); self.templates = PromptTemplate
    def _call(self, template, context, fallback_method):
        try:
            prompt_request = context.user_request
            hits = getattr(context, "memory_hits", ()) or ()
            if hits:
                hints = "；".join(str(item.get("problem", "")) + ": " + ";".join(item.get("solution", ()))
                              for item in hits[:3] if isinstance(item, dict))
                if hints:
                    prompt_request += f"\n历史经验（仅供审查，不能扩大修改范围）：{hints}"
            prompt = self.templates.render(template, request=prompt_request, result=prompt_request)
            raw = self.adapter.send({"messages": [{"role": "user", "content": prompt}]})
            content = str(raw["choices"][0]["message"]["content"])
            return AgentResponse(True, content, _parse_structured_content(content) or raw)
        except Exception as exc:
            local = getattr(self.fallback, fallback_method)(context)
            return AgentResponse(local.success, local.content, local.structured_data, f"Local LLM fallback: {exc}")
    def analyze(self, context): return self._call("analyze_requirement", context, "analyze")
    def plan(self, context): return self._call("plan_change", context, "plan")
    def explain(self, context): return self._call("explain_result", context, "explain")


class OpenAIAPIProvider(LocalLLMProvider):
    """Optional GPT/other OpenAI-compatible API provider; never default-on."""
    pass


class DeepSeekProvider(AgentProvider):
    def __init__(self, adapter=None, fallback=None):
        try: from .agent_adapter import DeepSeekAdapter, PromptTemplate
        except ImportError: from agent_adapter import DeepSeekAdapter, PromptTemplate
        self.adapter = adapter or DeepSeekAdapter()
        self.fallback = fallback or LocalAgentProvider()
        self.templates = PromptTemplate

    def _call(self, operation, context, fallback_method):
        try:
            prompt_request = context.user_request
            hits = getattr(context, "memory_hits", ()) or ()
            if hits:
                hints = "；".join(str(item.get("problem", "")) + ": " + ";".join(item.get("solution", ()))
                              for item in hits[:3] if isinstance(item, dict))
                if hints:
                    prompt_request += f"\n历史经验（仅供审查，不能扩大修改范围）：{hints}"
            prompt = self.templates.render(operation, request=prompt_request,
                                          result=prompt_request)
            raw = self.adapter.send({"model": getattr(self.adapter, "model", "deepseek"), "prompt": prompt})
            content = raw.get("answer", raw.get("content", raw if isinstance(raw, str) else ""))
            return AgentResponse(True, str(content), _parse_structured_content(str(content)) or raw)
        except Exception as exc:
            local = getattr(self.fallback, fallback_method)(context)
            return AgentResponse(local.success, local.content, local.structured_data,
                                 f"DeepSeek fallback: {exc}")

    def analyze(self, task_context): return self._call("analyze_requirement", task_context, "analyze")
    def plan(self, task_context): return self._call("plan_change", task_context, "plan")
    def explain(self, task_context): return self._call("explain_result", task_context, "explain")


class AgentProviderRegistry:
    def __init__(self):
        self._providers: dict[str, AgentProvider] = {"local": LocalAgentProvider(), "deepseek": DeepSeekProvider()}

    def register(self, name: str, provider: AgentProvider) -> None:
        if not name or not isinstance(provider, AgentProvider):
            raise TypeError("Provider 名称和实例必须有效")
        self._providers[name] = provider

    def get(self, name: str = "local") -> AgentProvider:
        if name not in self._providers:
            raise KeyError(f"Provider 未注册: {name}")
        return self._providers[name]


class TaskExecutor(ABC):
    @abstractmethod
    def execute(self, task_context): ...

    def rollback(self, task_context):
        return ExecutionResult(False, error="rollback not implemented")


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    changed_files: tuple[str, ...] = ()
    output: str = ""
    error: str | None = None
    rollback_available: bool = False
    raw: Any = None


class RetryPolicy:
    def should_retry(self, task_context) -> bool:
        if task_context.retry_count >= 3:
            task_context.need_escalation = True
            return False
        return not task_context.need_escalation


class EscalationManager:
    """Consult an explicitly configured provider after a retry/time trigger."""
    def __init__(self, provider: AgentProvider | None = None, fallback: AgentProvider | None = None):
        self.provider = provider
        self.fallback = fallback or LocalAgentProvider()

    def consult(self, task_context) -> AgentResponse:
        if not task_context.need_escalation:
            return AgentResponse(False, error="ESCALATION_NOT_TRIGGERED")
        if self.provider is None:
            return AgentResponse(False, error="ESCALATION_PROVIDER_NOT_CONFIGURED")
        try:
            response = self.provider.analyze(task_context)
            if isinstance(response, AgentResponse) and response.success:
                return response
            raise RuntimeError(getattr(response, "error", "provider failed"))
        except Exception as exc:
            local = self.fallback.analyze(task_context)
            return AgentResponse(local.success, local.content, local.structured_data,
                                 f"Escalation fallback: {exc}")


class LocalTaskExecutor(TaskExecutor):
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def execute(self, task_context):
        result = self.orchestrator.execute_confirmed_task(
            task_context.task_id, workspace=task_context.workspace,
            edits=(task_context.confirmation_card or {}).get("edits", {}),
            verification_commands=task_context.verification_commands)
        agent = result["result"]
        return ExecutionResult(result["status"] == "completed", agent.changed_files,
                               agent.summary, agent.failure_reason,
                               bool(agent.changed_files), raw=result)

    def rollback(self, task_context):
        return ExecutionResult(False, error="rollback由 Orchestrator checkpoint 流程处理")


class ProviderPlanExecutor(TaskExecutor):
    """Apply only a provider's structured edit plan through a safe executor.

    Free-form model text is never interpreted as code.  When the provider does
    not return ``{"edits": {relative_path: content}}`` within the confirmed
    file scope, the configured fallback executor handles the task unchanged.
    """
    def __init__(self, provider: AgentProvider, fallback: TaskExecutor):
        self.provider = provider
        self.fallback = fallback

    def execute(self, task_context):
        try:
            response = self.provider.plan(task_context)
            data = response.structured_data if isinstance(response, AgentResponse) else None
            edits = data.get("edits") if isinstance(data, dict) else None
            card = task_context.confirmation_card or {}
            allowed = set(card.get("allowed_files", ()))
            if not isinstance(edits, dict) or set(edits) - allowed or any(not isinstance(value, str) for value in edits.values()):
                return self.fallback.execute(task_context)
            task_context.confirmation_card = {**card, "edits": edits}
        except Exception:
            return self.fallback.execute(task_context)
        return self.fallback.execute(task_context)

    def rollback(self, task_context):
        return self.fallback.rollback(task_context)
