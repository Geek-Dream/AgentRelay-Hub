"""Offline task analysis and executor scheduling primitives."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True)
class TaskPlan:
    task_type: str
    difficulty: int
    risk: str
    need_confirmation: bool
    recommended_executor: str
    workflow: str
    reason: str

@dataclass(frozen=True)
class DecisionResult:
    task_type: str
    risk_level: str
    need_requirement_card: bool
    need_confirmation_card: bool
    recommended_worker: str
    recommended_model: str
    reason: str

class TaskDecisionEngine:
    def __init__(self): self.analyzer = TaskAnalyzer()
    def decide(self, request: str, *, retry_count: int = 0, elapsed_seconds: int = 0, modules: Iterable[str] = ()) -> DecisionResult:
        plan = self.analyzer.analyze(request, retry_count=retry_count, elapsed_seconds=elapsed_seconds, modules=modules)
        strict = plan.task_type in {"COMPLEX_TASK", "PROJECT_COMMANDER_TASK", "BLOCKED_TASK"}
        worker = "commander" if plan.task_type == "PROJECT_COMMANDER_TASK" else ("self" if plan.task_type == "COMPLEX_TASK" else "codex-subagent")
        model = "codex-self" if plan.task_type == "COMPLEX_TASK" else ("external-provider" if plan.task_type == "BLOCKED_TASK" else "codex-subagent")
        return DecisionResult(plan.task_type, plan.risk, strict,
                              strict or plan.task_type == "CONFIRMATION_TASK", worker, model, plan.reason)

class TaskAnalyzer:
    def analyze(self, request: str, *, retry_count: int = 0, elapsed_seconds: int = 0, modules: Iterable[str] = ()) -> TaskPlan:
        text = request.lower(); module_count = len(tuple(modules))
        if retry_count >= 3 or elapsed_seconds >= 900:
            return TaskPlan("BLOCKED_TASK", 4, "high", True, "codex-subagent", "weight-trigger", "重试或持续处理时间达到兜底条件")
        # A read-only technical question remains research even when it names a
        # high-risk component such as Redis or Kafka.  Modification verbs
        # below still route the same component to the confirmed workflow.
        research_intent = any(k in text for k in ("分析", "查询", "报错", "为什么", "兼容", "文档", "怎么安装"))
        change_intent = any(k in text for k in ("修改", "重构", "迁移", "替换", "增加", "删除", "修复", "改成"))
        if research_intent and not change_intent and module_count < 2:
            return TaskPlan("TECHNICAL_RESEARCH", 2, "low", False, "codex-subagent", "research", "需要分析或研究")
        if any(k in text for k in ("bug", "异常", "错误", "失败")) and not any(
                k in text for k in ("颜色", "样式", "变量", "参数")):
            return TaskPlan("TECHNICAL_RESEARCH", 2, "medium", False, "codex-subagent", "research", "普通技术问题需要外部建议")
        if module_count >= 2 or any(k in text for k in ("架构", "架构调整", "数据库", "数据库迁移", "mq", "kafka", "redis", "重构", "迁移", "基础设施", "项目级")):
            return TaskPlan("PROJECT_COMMANDER_TASK" if module_count >= 2 else "COMPLEX_TASK", 5, "high", True, "commander" if module_count >= 2 else "codex-subagent", "strict", "跨模块或架构调整")
        # Several ordinary changes are reviewed together in one confirmation
        # card. They do not need a requirement card or Commander by default.
        if any(marker in text for marker in ("并", "以及", "同时", "、")) and change_intent:
            return TaskPlan("CONFIRMATION_TASK", 2, "medium", True, "codex-subagent", "confirmation", "多个普通修改点需要一次汇总确认")
        if any(k in text for k in ("分析", "查询", "报错", "为什么")):
            return TaskPlan("TECHNICAL_RESEARCH", 2, "low", False, "codex-subagent", "research", "需要分析或研究")
        return TaskPlan("SIMPLE_TASK", 1, "low", False, "codex-subagent", "simple", "单文件或简单参数修改")

class ModelRegistry:
    def __init__(self, models=None):
        base = {"coding": 5, "research": 4, "simple_task": 5, "architecture": 4, "speed": 4, "cost": 1}
        self.models = {"codex-self": {"type": "self", "available": True, **base}, "codex-subagent": {"type": "subagent", "available": True, **base}}
        self.models["local-model"] = {"type": "local", "available": False, "coding": 6, "simple_task": 7, "speed": 6, "cost": 1}
        self.models["external-provider"] = {"type": "external", "available": False, "research": 7, "architecture": 6, "cost": 4}
        self.models["commander"] = {"type": "orchestrator", "available": False, "architecture": 8, "cost": 5}
        self.models.update(models or {})
    def available(self, name: str) -> bool:
        return bool(self.models.get(name, {}).get("available", False))

class TaskScheduler:
    def __init__(self, registry: ModelRegistry | None = None): self.registry = registry or ModelRegistry()
    def select(self, plan: TaskPlan) -> str:
        chosen = plan.recommended_executor
        if plan.task_type == "SIMPLE_TASK": chosen = "local-model"
        elif plan.task_type == "CONFIRMATION_TASK": chosen = "codex-subagent"
        elif plan.task_type == "COMPLEX_TASK": chosen = "codex-self"
        elif plan.task_type == "BLOCKED_TASK": chosen = "external-provider"
        if chosen == "commander": return "codex-subagent"
        if chosen in {"local-model", "external-provider"} and not self.registry.available(chosen): return "codex-subagent"
        return chosen if self.registry.available(chosen) else "codex-self"

    def blocked_advice(self, plan: TaskPlan) -> str | None:
        return "codex-subagent" if plan.task_type == "BLOCKED_TASK" else None
