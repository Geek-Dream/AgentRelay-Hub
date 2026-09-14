"""Small offline multi-agent runtime primitives."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable
import uuid


class AgentStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass
class AgentTask:
    task_id: str
    request: str
    parent_task: str | None = None
    child_tasks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentMessage:
    from_agent: str
    to_agent: str
    message_type: str
    content: str


@dataclass
class AgentInstance:
    agent_id: str
    role: str
    provider: Any
    status: AgentStatus = AgentStatus.CREATED
    task_id: str | None = None
    context: Any = None
    result: Any = None


class AgentManager:
    def __init__(self, max_workers: int = 4):
        self.agents: dict[str, AgentInstance] = {}
        self.tasks: dict[str, AgentTask] = {}
        self.messages: list[AgentMessage] = []
        self.max_workers = max_workers

    def create_agent(self, role: str, provider: Any, *, task_id: str | None = None,
                     context: Any = None, agent_id: str | None = None) -> AgentInstance:
        instance = AgentInstance(agent_id or str(uuid.uuid4()), role, provider,
                                 task_id=task_id, context=context)
        self.agents[instance.agent_id] = instance
        return instance

    def create_task(self, task_id: str, request: str, *, parent_task: str | None = None) -> AgentTask:
        task = AgentTask(task_id, request, parent_task)
        self.tasks[task_id] = task
        if parent_task in self.tasks:
            self.tasks[parent_task].child_tasks.append(task_id)
        return task

    def start_agent(self, agent_id: str, runner: Callable[[AgentInstance], Any] | None = None) -> AgentInstance:
        agent = self.agents[agent_id]
        if agent.status not in {AgentStatus.CREATED, AgentStatus.WAITING, AgentStatus.FAILED}:
            raise ValueError(f"Agent 状态 {agent.status.value} 不允许启动")
        agent.status = AgentStatus.RUNNING
        try:
            agent.result = runner(agent) if runner else agent.provider.execute(agent.context)
            # A parallel coordinator may have timed this agent out and moved
            # it to WAITING while a cooperative Python runner was still
            # returning.  Do not let that late return revive the task.
            if agent.status == AgentStatus.RUNNING:
                agent.status = AgentStatus.SUCCESS
        except Exception as exc:
            agent.result = exc
            if agent.status == AgentStatus.RUNNING:
                agent.status = AgentStatus.FAILED
        return agent

    def stop_agent(self, agent_id: str) -> AgentInstance:
        agent = self.agents[agent_id]
        if agent.status == AgentStatus.RUNNING:
            agent.status = AgentStatus.WAITING
        return agent

    def cancel_agent(self, agent_id: str) -> AgentInstance:
        return self.stop_agent(agent_id)

    def get_agent(self, agent_id: str) -> AgentInstance:
        return self.agents[agent_id]

    def list_agents(self) -> list[AgentInstance]:
        return list(self.agents.values())

    def send_message(self, message: AgentMessage) -> AgentMessage:
        if message.from_agent not in self.agents or message.to_agent not in self.agents:
            raise KeyError("消息 Agent 不存在")
        self.messages.append(message)
        return message

    def run_parallel(self, agent_ids: list[str], runner: Callable[[AgentInstance], Any] | None = None, timeout: float | None = None) -> list[AgentInstance]:
        if len(agent_ids) > self.max_workers:
            raise ValueError("Agent 数量超过并发预算")
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(agent_ids))))
        futures = [pool.submit(self.start_agent, agent_id, runner) for agent_id in agent_ids]
        done, pending = wait(futures, timeout=timeout)
        for future in done:
            # start_agent captures runner failures and stores FAILED on the instance.
            future.result()
        for agent_id, future in zip(agent_ids, futures):
            if future in pending:
                self.agents[agent_id].status = AgentStatus.WAITING
        # Do not hold the caller until a timed-out worker finishes. Running
        # threads cannot be force-killed, but their state is safely WAITING.
        pool.shutdown(wait=False, cancel_futures=True)
        return [self.agents[agent_id] for agent_id in agent_ids]

    def idle_agents(self, *, exclude=()) -> list[AgentInstance]:
        excluded=set(exclude)
        return [a for a in self.agents.values() if a.agent_id not in excluded and a.status in {AgentStatus.CREATED, AgentStatus.WAITING, AgentStatus.SUCCESS}]

    def assign_assist(self, task_id: str, *, exclude=()) -> AgentInstance | None:
        agents=self.idle_agents(exclude=exclude)
        if not agents: return None
        agent=agents[0]; agent.task_id=task_id; agent.status=AgentStatus.WAITING; return agent

    def recover(self, agent_id: str, runner: Callable[[AgentInstance], Any] | None = None) -> AgentInstance:
        agent = self.agents[agent_id]
        if agent.status not in {AgentStatus.FAILED, AgentStatus.WAITING}:
            raise ValueError(f"Agent 状态 {agent.status.value} 不支持恢复")
        return self.start_agent(agent_id, runner)

    def summarize(self, agent_ids: list[str] | None = None) -> dict:
        selected = [self.agents[x] for x in agent_ids] if agent_ids else self.list_agents()
        return {"total": len(selected), "success": sum(a.status == AgentStatus.SUCCESS for a in selected),
                "failed": sum(a.status == AgentStatus.FAILED for a in selected),
                "results": {a.agent_id: a.result for a in selected}}
