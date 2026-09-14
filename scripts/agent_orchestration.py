"""Rule-based orchestration helpers layered over the multi-agent runtime."""
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
import json


@dataclass(frozen=True)
class PlannedAgent:
    role: str
    goal: str


@dataclass(frozen=True)
class AgentExecutionPlan:
    plan_id: str
    task_id: str
    agents: tuple[PlannedAgent, ...]
    max_agents: int = 4
    timeout: int = 120
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AgentTaskPlanner:
    def plan(self, context, *, max_agents: int = 3, timeout: int = 120) -> AgentExecutionPlan:
        text = context.user_request.lower()
        roles = [("researcher", "查找相关代码和消费逻辑"), ("debugger", "分析问题原因")]
        if any(k in text for k in ("风险", "重构", "数据库", "kafka")):
            roles.append(("reviewer", "评估修改风险和验证方案"))
        roles = roles[:max(1, max_agents)]
        agents = tuple(PlannedAgent(*item) for item in roles)
        return AgentExecutionPlan(f"plan-{context.task_id}", context.task_id, agents, max_agents, timeout)


@dataclass(frozen=True)
class AgentSummary:
    conclusions: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"conclusions": list(self.conclusions), "conflicts": list(self.conflicts),
                "recommendations": list(self.recommendations)}


def merge_agent_results(results) -> AgentSummary:
    conclusions, recommendations, conflicts = [], [], []
    for result in results:
        text = str(result)
        if text:
            conclusions.append(text)
        if "建议" in text or "recommend" in text.lower():
            recommendations.append(text)
    lowered = [x.lower() for x in conclusions]
    if any("需要修改数据库" in x for x in conclusions) and any("无需修改数据库" in x for x in conclusions):
        conflicts.append("Agent 对数据库是否需要修改存在冲突")
    if len(set(lowered)) < len(lowered) and lowered:
        conflicts.append("多个 Agent 返回了重复结论")
    return AgentSummary(tuple(conclusions), tuple(conflicts), tuple(recommendations))


class AgentPersistence:
    def __init__(self, directory: str | Path = "runtime/agents"):
        self.directory = Path(directory)

    def save(self, agent) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"agent-{agent.agent_id}.json"
        data = asdict(agent); data["status"] = agent.status.value
        path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    def load(self, agent_id: str) -> dict:
        return json.loads((self.directory / f"agent-{agent_id}.json").read_text(encoding="utf-8"))
