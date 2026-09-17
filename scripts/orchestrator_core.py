#!/usr/bin/env python3
"""Executable no-external-model orchestration core for Agent Orchestrator v1."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import difflib
from pathlib import Path
from typing import Callable, Iterable
import uuid
import hashlib
import subprocess
import time
import json
import os
import shlex

try:
    from .orchestrator_store import CheckpointStore, KnowledgeRecord, MemoryStore
except ImportError:
    from orchestrator_store import CheckpointStore, KnowledgeRecord, MemoryStore
try:
    from .task_scheduler import TaskAnalyzer, TaskScheduler, TaskDecisionEngine
except ImportError:
    from task_scheduler import TaskAnalyzer, TaskScheduler, TaskDecisionEngine


class TaskType(str, Enum):
    SIMPLE_TASK = "SIMPLE_TASK"
    CONFIRMATION_TASK = "CONFIRMATION_TASK"
    TECHNICAL_RESEARCH = "TECHNICAL_RESEARCH"
    COMPLEX_TASK = "COMPLEX_TASK"
    PROJECT_COMMANDER_TASK = "PROJECT_COMMANDER_TASK"


@dataclass(frozen=True)
class AgentResult:
    task_id: str
    status: str
    summary: str = ""
    changed_files: tuple[str, ...] = ()
    diff_summary: str = ""
    validation: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    failure_reason: str | None = None
    provider: str = "codex-subagent"
    fallback_used: bool = False
    before_snapshot: dict[str, str] = field(default_factory=dict)
    after_snapshot: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewResult:
    approved: bool
    score: int
    findings: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    required_changes: tuple[str, ...] = ()
    validation_result: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentReport:
    agent_id: str
    role: str
    task_id: str
    status: str
    completed_work: str
    changed_files: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    validation: tuple[str, ...] = ()
    next_action: str = ""


@dataclass
class RequirementCard:
    task_id: str
    title: str
    original_request: str
    task_type: str
    complexity: int
    business_goal: str
    acceptance_criteria: list[str] = field(default_factory=list)
    affected_modules: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    estimated_steps: list[str] = field(default_factory=list)
    status: str = "draft"
    user_requirement: str = ""
    requirement_assessment: str = ""
    plain_explanation: str = ""
    current_state: str = "待分析"
    target_state: str = "按验收标准完成"
    verification_plan: list[str] = field(default_factory=list)
    needs_confirmation: bool = True
    # Structured aliases used by the public workflow API and renderers.
    affected_files: list[str] = field(default_factory=list)
    risk_analysis: list[str] = field(default_factory=list)
    rollback_plan: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RequirementAssessment:
    strict_workflow: bool
    reason: str
    complexity: int
    affected_modules: tuple[str, ...]
    risks: tuple[str, ...]
    needs_commander: bool
    needs_confirmation: bool


@dataclass(frozen=True)
class ConfirmationCard:
    task_id: str
    action: str
    scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    status: str = "pending"
    request: str = ""
    workspace: str | None = None
    allowed_files: tuple[str, ...] = ()
    edits: dict[str, str] = field(default_factory=dict)
    verification_commands: tuple[str, ...] = ()
    attempts: int = 0
    last_error: str = ""
    recovery_reason: str = ""
    attempt_history: list[dict] = field(default_factory=list)
    # Complex work confirms its requirement scope before this second action.
    next_action: str = "execute"


class ConfirmationStore:
    EVENT_TYPES = {"started", "checkpoint_created", "worker_started", "verification_started", "verification_failed", "rollback_started", "rollback_completed", "completed", "failed", "recovery_required"}
    TRANSITIONS = {"pending": {"approved", "rejected", "cancelled", "expired"}, "approved": {"executing"}, "executing": {"completed", "failed", "recovery_required"}, "recovery_required": set(), "rejected": set(), "cancelled": set(), "expired": set(), "completed": set(), "failed": set()}

    def __init__(self, directory: str | Path | None = None):
        self.cards: dict[str, ConfirmationCard] = {}
        self.directory = Path(directory) if directory else None
        if self.directory:
            self._load()

    def _path(self, task_id: str) -> Path:
        assert self.directory is not None
        return self.directory / f"{task_id}.json"

    def _persist(self, card: ConfirmationCard) -> None:
        if not self.directory:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(card.task_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(card), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _load(self) -> None:
        for path in self.directory.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                history = [x for x in value.get("attempt_history", []) if isinstance(x, dict)]
                card = ConfirmationCard(str(value["task_id"]), str(value["action"]), tuple(value.get("scope", ())), tuple(value.get("acceptance_criteria", ())), str(value.get("status", "pending")), str(value.get("request", "")), value.get("workspace"), tuple(value.get("allowed_files", ())), dict(value.get("edits", {})), tuple(value.get("verification_commands", ())), int(value.get("attempts", 0)), str(value.get("last_error", "")), str(value.get("recovery_reason", "")), history, str(value.get("next_action", "execute")))
                if card.status not in self.TRANSITIONS:
                    continue
                self.cards[card.task_id] = card
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue

    def create(self, card: ConfirmationCard) -> ConfirmationCard:
        self.cards[card.task_id] = card
        self._persist(card)
        return card

    def transition(self, task_id: str, status: str) -> ConfirmationCard:
        card = self.cards[task_id]
        if status not in self.TRANSITIONS.get(card.status, set()):
            raise ValueError(f"非法确认卡状态迁移: {card.status} -> {status}")
        updated = ConfirmationCard(card.task_id, card.action, card.scope, card.acceptance_criteria, status,
                                   card.request, card.workspace, card.allowed_files, card.edits, card.verification_commands,
                                   card.attempts, card.last_error, card.recovery_reason, card.attempt_history, card.next_action)
        self.cards[task_id] = updated
        self._persist(updated)
        return updated

    def can_execute(self, task_id: str) -> bool:
        return (task_id in self.cards and self.cards[task_id].status == "approved"
                and self.cards[task_id].action == "execute")

    def advance_requirement(self, task_id: str) -> ConfirmationCard:
        """Record requirement approval and create the separate action card."""
        card = self.cards[task_id]
        if card.action != "confirm_requirement" or card.status != "pending":
            raise ValueError("当前确认卡不是待确认的需求卡")
        approved = self.transition(task_id, "approved")
        history = list(approved.attempt_history) + [{
            "stage": "requirement", "status": "approved",
            "at": datetime.now(timezone.utc).isoformat(),
        }]
        execution = ConfirmationCard(
            approved.task_id, approved.next_action, approved.scope,
            approved.acceptance_criteria, "pending", approved.request,
            approved.workspace, approved.allowed_files, approved.edits,
            approved.verification_commands, approved.attempts,
            approved.last_error, approved.recovery_reason, history,
            approved.next_action,
        )
        return self.create(execution)

    def add_event(self, task_id: str, event_type: str, message: str = "") -> ConfirmationCard:
        if event_type not in self.EVENT_TYPES:
            raise ValueError("非法执行事件类型")
        card = self.cards.get(task_id)
        if card is None:
            return card
        history = list(card.attempt_history)
        if history:
            event = {"type": event_type, "at": datetime.now(timezone.utc).isoformat(), "message": str(message)[:300]}
            history[-1] = {**history[-1], "events": list(history[-1].get("events", [])) + [event]}
            updated = ConfirmationCard(card.task_id,card.action,card.scope,card.acceptance_criteria,card.status,card.request,card.workspace,card.allowed_files,card.edits,card.verification_commands,card.attempts,card.last_error,card.recovery_reason,history)
            self.cards[task_id] = updated; self._persist(updated); return updated
        return card

    def recover(self, task_id: str, action: str, reason: str = "") -> ConfirmationCard:
        card = self.cards[task_id]
        if card.status not in {"executing", "recovery_required", "failed"}:
            raise ValueError(f"状态 {card.status} 不允许恢复")
        if action not in {"fail", "retry"}:
            raise ValueError("恢复操作必须是 fail 或 retry")
        status = "failed" if action == "fail" else "approved"
        updated = ConfirmationCard(card.task_id, card.action, card.scope, card.acceptance_criteria, status,
                                   card.request, card.workspace, card.allowed_files, card.edits, card.verification_commands,
                                   card.attempts, card.last_error, reason, card.attempt_history)
        self.cards[task_id] = updated; self._persist(updated)
        return updated

    def mark_recovery_required(self, task_id: str, reason: str) -> ConfirmationCard:
        card = self.cards[task_id]
        if card.status != "executing":
            raise ValueError(f"状态 {card.status} 不允许标记中断")
        updated = ConfirmationCard(card.task_id, card.action, card.scope, card.acceptance_criteria, "recovery_required",
                                   card.request, card.workspace, card.allowed_files, card.edits, card.verification_commands,
                                   card.attempts, card.last_error, reason, card.attempt_history)
        self.cards[task_id] = updated; self._persist(updated)
        return updated

    def retry_failed(self, task_id: str, max_retries: int = 1) -> ConfirmationCard:
        card = self.cards[task_id]
        if card.status != "failed":
            raise ValueError(f"状态 {card.status} 不允许重试")
        if card.attempts > max_retries:
            raise ValueError("超过最大重试次数")
        return self.recover(task_id, "retry", "explicit retry")


@dataclass(frozen=True)
class VerificationCard:
    task_id: str
    acceptance_criteria: tuple[str, ...]
    checks: tuple[str, ...]
    tests: tuple[str, ...]
    static_checks: tuple[str, ...]
    diff_checks: tuple[str, ...]
    unresolved: tuple[str, ...]
    passed: bool
    review: str = ""


@dataclass(frozen=True)
class VerificationResult:
    command: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    error: str | None = None


class TaskExecutionState(str, Enum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLBACK = "ROLLBACK"
    ROLLED_BACK = "ROLLED_BACK"


class TaskStateMachine:
    TRANSITIONS = {
        TaskExecutionState.CREATED: {TaskExecutionState.ANALYZING},
        TaskExecutionState.ANALYZING: {TaskExecutionState.WAITING_CONFIRMATION, TaskExecutionState.EXECUTING, TaskExecutionState.SUCCESS},
        TaskExecutionState.WAITING_CONFIRMATION: {TaskExecutionState.APPROVED, TaskExecutionState.FAILED},
        TaskExecutionState.APPROVED: {TaskExecutionState.EXECUTING},
        TaskExecutionState.EXECUTING: {TaskExecutionState.VERIFYING, TaskExecutionState.FAILED, TaskExecutionState.ROLLBACK},
        TaskExecutionState.VERIFYING: {TaskExecutionState.SUCCESS, TaskExecutionState.FAILED, TaskExecutionState.ROLLBACK},
        TaskExecutionState.FAILED: {TaskExecutionState.ROLLBACK, TaskExecutionState.EXECUTING},
        TaskExecutionState.ROLLBACK: {TaskExecutionState.ROLLED_BACK, TaskExecutionState.FAILED},
        TaskExecutionState.SUCCESS: set(), TaskExecutionState.ROLLED_BACK: set(),
    }

    def __init__(self, state: TaskExecutionState = TaskExecutionState.CREATED):
        self.state = TaskExecutionState(state)

    def transition(self, target: TaskExecutionState) -> TaskExecutionState:
        target = TaskExecutionState(target)
        if target not in self.TRANSITIONS[self.state]:
            raise ValueError(f"非法任务状态迁移: {self.state.value} -> {target.value}")
        self.state = target
        return self.state


@dataclass
class TaskContext:
    task_id: str
    workspace: str | None
    user_request: str
    requirement_card: dict | None = None
    confirmation_card: dict | None = None
    changed_files: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    status: TaskExecutionState = TaskExecutionState.CREATED
    retry_count: int = 0
    weight: int = 1
    need_escalation: bool = False
    effective_time_seconds: float = 0.0
    agent_task_id: str | None = None
    child_agents: list[str] = field(default_factory=list)
    agent_execution_plan: dict | None = None
    memory_hits: list[dict] = field(default_factory=list)
    project: str = "default"
    session_id: str | None = None
    last_event_type: str | None = None
    last_event_at: str | None = None
    created_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    ESCALATION_SECONDS = 15 * 60

    def transition(self, target: TaskExecutionState) -> None:
        self.status = TaskStateMachine(self.status).transition(target)
        self.updated_time = datetime.now(timezone.utc).isoformat()

    def record_failure(self, *, elapsed_seconds: int = 0) -> None:
        self.retry_count += 1
        self.record_progress(elapsed_seconds=elapsed_seconds)
        self.need_escalation = self.retry_count >= 3 or self.effective_time_seconds >= self.ESCALATION_SECONDS
        self.updated_time = datetime.now(timezone.utc).isoformat()

    def record_progress(self, *, elapsed_seconds: float = 0, failed: bool = False) -> None:
        self.effective_time_seconds += max(0.0, float(elapsed_seconds or 0))
        self.weight = min(3, 1 + int(self.effective_time_seconds // 300))
        if failed:
            self.retry_count += 1
        self.need_escalation = self.retry_count >= 3 or self.effective_time_seconds >= self.ESCALATION_SECONDS
        self.updated_time = datetime.now(timezone.utc).isoformat()

    def save(self, directory: str | Path) -> Path:
        path = Path(directory); path.mkdir(parents=True, exist_ok=True)
        target = path / f"{self.task_id}.json"
        data = asdict(self); data["status"] = self.status.value
        tmp = target.with_suffix(".tmp"); tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8"); tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "TaskContext":
        data = json.loads(Path(path).read_text(encoding="utf-8")); data["status"] = TaskExecutionState(data.get("status", "CREATED"))
        return cls(**data)


@dataclass(frozen=True)
class ChangeLog:
    task_id: str
    operation: str
    actor: str
    provider: str
    changed_files: tuple[str, ...]
    summary: str
    validation: tuple[str, ...] = ()
    failure_reason: str | None = None
    fallback: str | None = None
    rollback: str | None = None
    before_state: str | None = None
    after_state: str | None = None
    verification_result: bool | None = None
    rollback_available: bool = False
    event_type: str = "EXECUTION"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _safe_file(workspace: Path, relative: str) -> Path:
    root = workspace.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"文件超出 workspace 范围: {relative}")
    return target


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        return _digest(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, UnicodeDecodeError):
        return None


def classify_task(request: str, complexity: int | None = None, modules: Iterable[str] = ()) -> TaskType:
    text = request.lower()
    module_count = len(tuple(modules))
    if complexity is not None and complexity >= 5 and module_count >= 2:
        return TaskType.PROJECT_COMMANDER_TASK
    if any(word in text for word in ("重构", "kafka", "mq", "数据库结构", "跨模块", "项目级")):
        return TaskType.PROJECT_COMMANDER_TASK if module_count >= 2 else TaskType.COMPLEX_TASK
    if complexity is not None and complexity >= 4:
        return TaskType.COMPLEX_TASK
    if any(word in text for word in ("查询", "分析报错", "为什么", "版本兼容", "查资料")):
        return TaskType.TECHNICAL_RESEARCH
    return TaskType.SIMPLE_TASK


class SelfWorker:
    provider = "codex-subagent"

    def execute(self, task_id: str, prompt: str, allowed_files: Iterable[str] = (), workspace: str | Path | None = None,
                edits: dict[str, str] | None = None, dry_run: bool = False) -> AgentResult:
        files = tuple(allowed_files)
        if workspace is None:
            return AgentResult(task_id, "success", f"Self Worker 生成受限任务计划：{prompt}", (),
                               "未提供 workspace，未写入文件", ("plan only",), (), provider=self.provider)
        root = Path(workspace).resolve(); edits = edits or {}
        if set(edits) - set(files):
            extra = sorted(set(edits) - set(files))
            return AgentResult(task_id, "failed", "拒绝越权修改", (), "", (),
                               tuple([f"未授权文件: {p}" for p in extra]), "FILE_SCOPE_DENIED", provider=self.provider)
        before, after, changed = {}, {}, []
        try:
            for relative in files:
                target = _safe_file(root, relative)
                old = target.read_text(encoding="utf-8") if target.is_file() else ""
                before[relative] = _digest(old)
                new = edits.get(relative, old)
                after[relative] = _digest(new)
                if new != old:
                    changed.append(relative)
                    if not dry_run:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(new, encoding="utf-8")
            return AgentResult(task_id, "success", f"Self Worker 完成受限任务：{prompt}", tuple(changed),
                               f"修改文件: {', '.join(changed) or '无'}", ("scope checked",), (), provider=self.provider,
                               before_snapshot=before, after_snapshot=after)
        except (OSError, PermissionError) as exc:
            return AgentResult(task_id, "failed", "文件修改失败", tuple(changed), "", (), (), str(exc), provider=self.provider,
                               before_snapshot=before, after_snapshot=after)


class SelfResearcher:
    provider = "codex-subagent"

    def execute(self, task_id: str, prompt: str) -> AgentResult:
        return AgentResult(task_id, "success", f"Self Researcher 提供本地分析：{prompt}", provider=self.provider)


class FallbackExecutor:
    """Try configured executor first, then always-available Codex subagent."""

    def __init__(self, primary: Callable[[str, str], AgentResult] | None = None, max_calls: int = 5):
        self.primary = primary
        self.max_calls = max_calls
        self.calls: dict[str, int] = {}

    def execute(self, task_id: str, prompt: str) -> AgentResult:
        count = self.calls.get(task_id, 0)
        if count >= self.max_calls:
            return AgentResult(task_id, "failed", failure_reason="MAX_CALLS_EXCEEDED", provider="codex-self")
        self.calls[task_id] = count + 1
        if self.primary:
            try:
                result = self.primary(task_id, prompt)
                if result.status == "success":
                    return result
            except Exception:
                pass
        fallback = SelfResearcher().execute(task_id, prompt)
        return AgentResult(fallback.task_id, fallback.status, fallback.summary, provider=fallback.provider, fallback_used=True)


class SelfReviewer:
    def review(self, result: AgentResult, allowed_files: Iterable[str], has_tests: bool = True) -> ReviewResult:
        allowed = set(allowed_files)
        extra = set(result.changed_files) - allowed
        issues = []
        if extra:
            issues.append("Worker 修改了授权范围外文件: " + ", ".join(sorted(extra)))
        if result.status != "success":
            issues.append(result.failure_reason or "Worker 执行失败")
        if not has_tests:
            issues.append("没有提供测试或验证证据")
        return ReviewResult(not issues, 90 if not issues else 35, tuple(issues), tuple(issues),
                            (), tuple(issues), ("scope checked",))


class VerificationRunner:
    ALLOWED = ("python -m compileall", "python3 -m compileall", "pytest", "python -m unittest", "python3 -m unittest", "git diff --check")

    def run(self, command: str, workspace: str | Path, timeout_seconds: int = 120) -> VerificationResult:
        if not any(command == allowed or command.startswith(allowed + " ") for allowed in self.ALLOWED):
            return VerificationResult(command, "rejected", None, "", "命令不在允许列表", 0, error="COMMAND_NOT_ALLOWED")
        started = time.monotonic()
        try:
            completed = subprocess.run(command.split(), cwd=str(Path(workspace)), capture_output=True,
                                       text=True, timeout=timeout_seconds, check=False)
            return VerificationResult(command, "passed" if completed.returncode == 0 else "failed", completed.returncode,
                                      completed.stdout, completed.stderr, (time.monotonic() - started) * 1000)
        except subprocess.TimeoutExpired as exc:
            return VerificationResult(command, "timeout", None, exc.stdout or "", exc.stderr or "",
                                      (time.monotonic() - started) * 1000, True, "COMMAND_TIMEOUT")
        except OSError as exc:
            return VerificationResult(command, "failed", None, "", str(exc), (time.monotonic() - started) * 1000, error="COMMAND_ERROR")

    def run_many(self, commands: Iterable[str], workspace: str | Path, timeout_seconds: int = 120) -> tuple[VerificationResult, ...]:
        return tuple(self.run(command, workspace, timeout_seconds) for command in commands)


class Orchestrator:
    """Runs default self-agent flows without requiring any external model."""

    def __init__(self, memory: MemoryStore | None = None, checkpoints: CheckpointStore | None = None,
                 confirmations: ConfirmationStore | None = None):
        self.memory = memory
        self.checkpoints = checkpoints
        self.changelog: list[ChangeLog] = []
        self.reviewer = SelfReviewer()
        self.confirmations = confirmations or ConfirmationStore()
        self.analyzer = TaskAnalyzer()
        self.scheduler = TaskScheduler()
        self.decision_engine = TaskDecisionEngine()

    def run_simple(self, task_id: str, prompt: str, allowed_files: Iterable[str] = (), *,
                   workspace: str | Path | None = None, edits: dict[str, str] | None = None,
                   verification_commands: Iterable[str] = ()) -> tuple[AgentResult, ReviewResult, VerificationCard]:
        """Run a simple task, rolling back only task content after failed checks."""
        checkpoint_path = None
        if workspace is not None and self.checkpoints is not None:
            checkpoint_path = self.checkpoints.create_from_workspace(task_id, workspace, allowed_files,
                                                                       description=prompt)
        result = SelfWorker().execute(task_id, prompt, allowed_files, workspace, edits)
        checkpoint_data = self.checkpoints.finalize_from_workspace(checkpoint_path, workspace) if checkpoint_path else None
        review = self.reviewer.review(result, allowed_files)
        checks = VerificationRunner().run_many(verification_commands, workspace) if verification_commands and workspace else ()
        passed = review.approved and all(check.status == "passed" for check in checks)
        rollback_note = None
        if not passed and checkpoint_data is not None and result.changed_files:
            self.confirmations.add_event(task_id, "rollback_started")
            rollback_log = self.apply_rollback(task_id, checkpoint_data, confirmed=True, workspace=workspace)
            self.confirmations.add_event(task_id, "rollback_completed")
            rollback_note = rollback_log.rollback
        verification = VerificationCard(task_id, ("任务范围符合需求",), ("scope checked",),
                                        tuple(check.command for check in checks) or ("self-test",), (), (), (), passed, "Self Reviewer")
        self.changelog.append(ChangeLog(task_id, "worker", "codex-self", result.provider, result.changed_files,
                                        result.summary, verification.tests,
                                        failure_reason=None if passed else "VERIFICATION_FAILED", rollback=rollback_note,
                                        before_state="EXECUTING", after_state="SUCCESS" if passed else "FAILED",
                                        verification_result=passed, rollback_available=checkpoint_data is not None))
        return result, review, verification

    def run_research(self, task_id: str, prompt: str) -> AgentResult:
        result = SelfResearcher().execute(task_id, prompt)
        self.changelog.append(ChangeLog(task_id, "research", "codex-self", result.provider, (), result.summary))
        return result

    def execute_task(self, task_id: str, request: str, *, complexity: int | None = None,
                     modules: Iterable[str] = (), allowed_files: Iterable[str] = (),
                     workspace: str | Path | None = None, edits: dict[str, str] | None = None,
                     verification_commands: Iterable[str] = (), project: str = "default",
                     session_id: str | None = None, enable_commander: bool = False) -> dict:
        """Classify and execute a task using only always-available Codex agents."""
        decision = self.decision_engine.decide(request, retry_count=0, modules=modules)
        plan = self.analyzer.analyze(request, retry_count=0, modules=modules)
        task_type = TaskType(plan.task_type)
        executor = self.scheduler.select(plan)
        memory_hits = self.memory.search(request, project=project) if self.memory else []
        if task_type == TaskType.SIMPLE_TASK:
            result, review, verification = self.run_simple(task_id, request, allowed_files,
                                                            workspace=workspace, edits=edits,
                                                            verification_commands=verification_commands)
            if self.memory:
                self.memory.record_outcome(request, [result.summary], success=verification.passed,
                                           changed_files=list(result.changed_files), project=project,
                                           session_id=session_id)
            return {"task_type": task_type.value, "plan": plan, "decision": decision, "executor": executor, "memory_hits": memory_hits,
                    "result": result, "review": review, "verification": verification}
        if task_type == TaskType.TECHNICAL_RESEARCH:
            result = self.run_research(task_id, request)
            if self.memory:
                self.memory.record_outcome(
                    request,
                    [result.summary],
                    success=result.status == "success",
                    project=project,
                    session_id=session_id)
            return {"task_type": task_type.value, "plan": plan, "decision": decision, "executor": executor, "memory_hits": memory_hits, "result": result}
        if task_type == TaskType.CONFIRMATION_TASK:
            assessment, confirmation = self.create_confirmation_only(
                task_id, request, plan.difficulty, modules, allowed_files,
                workspace=workspace, edits=edits,
                verification_commands=verification_commands)
            return {"task_type": task_type.value, "plan": plan, "decision": decision,
                    "executor": executor, "memory_hits": memory_hits,
                    "assessment": assessment, "confirmation": confirmation}
        card, assessment, confirmation = self.create_requirement(
            task_id, request, complexity or plan.difficulty, modules, allowed_files,
            workspace=workspace, edits=edits, verification_commands=verification_commands)
        reports = self.run_commander(task_id, request) if (enable_commander and task_type == TaskType.PROJECT_COMMANDER_TASK) else ()
        return {"task_type": task_type.value, "plan": plan, "decision": decision, "executor": executor, "memory_hits": memory_hits, "card": card,
                "assessment": assessment, "confirmation": confirmation, "reports": reports}

    def record_verified_solution(self, problem: str, solution: list[str], *, source: str = "codex-self", environment: dict[str, str] | None = None) -> KnowledgeRecord | None:
        if not self.memory:
            return None
        record = KnowledgeRecord(problem=problem, solution=solution, source=source,
                                 environment=environment or {}, verified=True, confidence=0.9)
        return self.memory.add(record)

    def record_failure(self, task_id: str, task_type: str, instruction: str, *, agent: str = "codex-self", provider: str = "codex-subagent", attempts: int = 1, retry_count: int = 0, weight: int = 1, changed_files: Iterable[str] = (), failure_reason: str = "", validation_result: Iterable[str] = (), rollback_result: str = "", fallback_used: bool = False) -> KnowledgeRecord | None:
        if not self.memory:
            return None
        record = KnowledgeRecord(problem=instruction, solution=[f"失败: {failure_reason}"], symptoms=[task_type, f"retry_count={retry_count}", f"attempts={attempts}"], source=provider, confidence=0.1, verified=False, environment={"agent": agent, "weight": str(weight), "changed_files": ",".join(changed_files), "rollback": rollback_result, "fallback_used": str(fallback_used)})
        return self.memory.add(record)

    def create_requirement(self, task_id: str, request: str, complexity: int, modules: Iterable[str], files: Iterable[str], *,
                           workspace: str | Path | None = None, edits: dict[str, str] | None = None,
                           verification_commands: Iterable[str] = ()) -> tuple[RequirementCard, RequirementAssessment, ConfirmationCard]:
        modules = tuple(modules); files = tuple(files)
        task_type = classify_task(request, complexity, modules)
        commander = task_type == TaskType.PROJECT_COMMANDER_TASK
        text = request.lower()
        inferred = list(modules)
        for keyword, label in (("mq", "消息队列"), ("kafka", "Kafka"), ("rabbitmq", "RabbitMQ"),
                               ("数据库", "数据库"), ("订单", "订单"), ("前端", "frontend"), ("后端", "backend")):
            if keyword in text and label not in inferred:
                inferred.append(label)
        inferred_modules = tuple(inferred)
        inferred_files = list(files)
        if any(k in text for k in ("mq", "kafka", "rabbitmq")):
            for name in ("pom.xml", "application.yml", "消息生产者/消费者代码"):
                if name not in inferred_files:
                    inferred_files.append(name)
        elif "数据库" in text or "迁移" in text:
            for name in ("数据库迁移脚本", "application.yml"):
                if name not in inferred_files:
                    inferred_files.append(name)
        risks = ["范围扩大风险"]
        if any(k in text for k in ("mq", "kafka", "rabbitmq", "数据库", "迁移")):
            risks.append("兼容性与数据迁移风险")
        reason = "跨模块或高风险任务" if complexity >= 4 or len(inferred_modules) >= 2 else "复杂任务"
        assessment = RequirementAssessment(True, reason, complexity, inferred_modules, tuple(risks), commander, True)
        current, target = "当前实现保持不变", "按验收标准完成"
        if "kafka" in text and ("mq" in text or "rabbitmq" in text):
            current, target = "当前使用 RabbitMQ/MQ", "生产者和消费者切换到 Kafka 并保持消息可用"
        elif "重构" in text:
            current, target = "现有实现待重构", "按目标架构完成重构并通过验证"
        explanation = f"我准备：分析并修改 {', '.join(files) or '相关代码'}；调整必要配置；执行验证。"
        acceptance = ["修改后通过验证", "不修改未授权文件"]
        if "kafka" in text and ("mq" in text or "rabbitmq" in text):
            acceptance += ["生产者发送 Kafka 消息", "消费者能够正常消费"]
        checks = tuple(verification_commands) or ("python3 -m compileall -q scripts",)
        rollback_steps = ["保存修改前文件指纹和内容", "失败时逐文件检查冲突", "无冲突则恢复，有用户修改则跳过"]
        card = RequirementCard(task_id, request[:80], request, task_type.value, complexity, request,
                               acceptance, list(inferred_modules), inferred_files, [], list(assessment.risks),
                               ["确认需求范围", "执行最小修改", "运行验证并记录结果"], "awaiting_confirmation")
        card.user_requirement = request
        card.requirement_assessment = assessment.reason
        card.plain_explanation = explanation
        card.current_state = current + "，等待确认"
        card.target_state = target
        card.verification_plan = list(card.acceptance_criteria) + [f"运行: {check}" for check in checks]
        card.needs_confirmation = True
        card.affected_files = list(inferred_files)
        card.risk_analysis = list(assessment.risks)
        card.rollback_plan = rollback_steps
        # Complex business work has two distinct confirmations: first accept
        # the understood requirement, then approve execution or Commander.
        next_action = "enable_commander" if commander else "execute"
        confirm = ConfirmationCard(task_id, "confirm_requirement", files, tuple(card.acceptance_criteria),
                                   request=request, workspace=str(workspace) if workspace else None,
                                   allowed_files=files, edits=dict(edits or {}), verification_commands=checks,
                                   next_action=next_action)
        self.confirmations.create(confirm)
        return card, assessment, confirm

    def create_confirmation_only(self, task_id: str, request: str, complexity: int,
                                 modules: Iterable[str], files: Iterable[str], *,
                                 workspace: str | Path | None = None,
                                 edits: dict[str, str] | None = None,
                                 verification_commands: Iterable[str] = ()) -> tuple[RequirementAssessment, ConfirmationCard]:
        """Create one ordinary confirmation card for several small changes."""
        files = tuple(files)
        parts = [part.strip(" ，,。；;") for part in request.replace("以及", "并").replace("同时", "并").split("并")]
        requested_changes = tuple(part for part in parts if part) or (request,)
        checks = tuple(verification_commands) or ("python3 -m compileall -q scripts",)
        assessment = RequirementAssessment(False, "多个普通修改点，使用一张汇总确认卡", complexity,
                                           tuple(modules), ("确认后只修改授权文件",), False, True)
        confirmation = ConfirmationCard(
            task_id, "execute", files,
            ("执行以下确认的修改：" + "；".join(requested_changes), "不修改未授权文件", "修改后通过验证"),
            request=request, workspace=str(workspace) if workspace else None,
            allowed_files=files, edits=dict(edits or {}), verification_commands=checks,
        )
        self.confirmations.create(confirmation)
        return assessment, confirmation

    @staticmethod
    def inspect_workspace(workspace: str | Path) -> dict:
        root = Path(workspace)
        files = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
        diff = ""
        try:
            diff = subprocess.run(["git", "diff", "--stat"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return {"files": files[:200], "file_count": len(files), "git_diff_stat": diff}

    def execute_confirmed_task(self, task_id: str, *, workspace: str | Path | None = None,
                               edits: dict[str, str] | None = None,
                               verification_commands: Iterable[str] = ()) -> dict:
        """Execute exactly once from a persisted approved confirmation card."""
        card = self.confirmations.cards.get(task_id)
        if card is None:
            raise PermissionError("任务没有确认卡，拒绝执行")
        if card.status != "approved":
            raise PermissionError(f"确认卡状态 {card.status} 不允许执行")
        executing = self.confirmations.transition(task_id, "executing")
        executing = ConfirmationCard(executing.task_id, executing.action, executing.scope, executing.acceptance_criteria,
                                     executing.status, executing.request, executing.workspace, executing.allowed_files,
                                     executing.edits, executing.verification_commands, executing.attempts + 1,
                                     executing.last_error, executing.recovery_reason,
                                     executing.attempt_history + [{"attempt": executing.attempts + 1, "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": "", "status": "executing", "error": ""}])
        self.confirmations.cards[task_id] = executing
        self.confirmations._persist(executing)
        self.confirmations.add_event(task_id, "worker_started")
        try:
            run_workspace = workspace if workspace is not None else executing.workspace
            run_edits = edits if edits is not None else executing.edits
            commands = tuple(verification_commands) or executing.verification_commands
            result, review, verification = self.run_simple(
                task_id, executing.request or executing.action, executing.allowed_files or executing.scope,
                workspace=run_workspace, edits=run_edits, verification_commands=commands)
            successful = result.status == "success" and review.approved and verification.passed
            self.confirmations.add_event(task_id, "verification_started")
            if not successful:
                self.confirmations.add_event(task_id, "verification_failed", "verification failed")
            self.confirmations.transition(task_id, "completed" if successful else "failed")
            current = self.confirmations.cards[task_id]
            history = list(current.attempt_history)
            if history:
                history[-1] = {**history[-1], "finished_at": datetime.now(timezone.utc).isoformat(), "status": "completed" if successful else "failed", "error": "" if successful else (result.failure_reason or "verification failed")}
                current = ConfirmationCard(current.task_id,current.action,current.scope,current.acceptance_criteria,current.status,current.request,current.workspace,current.allowed_files,current.edits,current.verification_commands,current.attempts,current.last_error,current.recovery_reason,history)
                self.confirmations.cards[task_id]=current; self.confirmations._persist(current)
            if not successful:
                failed = self.confirmations.cards[task_id]
                failed = ConfirmationCard(failed.task_id, failed.action, failed.scope, failed.acceptance_criteria,
                                          failed.status, failed.request, failed.workspace, failed.allowed_files,
                                          failed.edits, failed.verification_commands, failed.attempts,
                                          result.failure_reason or "verification failed", failed.recovery_reason)
                self.confirmations.cards[task_id] = failed; self.confirmations._persist(failed)
            return {"result": result, "review": review, "verification": verification,
                    "status": "completed" if successful else "failed"}
        except Exception as exc:
            try:
                self.confirmations.transition(task_id, "failed")
            except (KeyError, ValueError):
                pass
            self.record_failure(task_id, "CONFIRMED_TASK", executing.request or executing.action,
                                failure_reason=str(exc))
            raise

    def plan_commander_roles(self, request: str, modules: Iterable[str] = (), *,
                             max_agents: int = 5, roles: Iterable[str] | None = None):
        try:
            from .research_commander import plan_commander_roles
        except ImportError:
            from research_commander import plan_commander_roles
        return plan_commander_roles(request, modules, max_agents=max_agents,
                                    requested_roles=roles)

    def run_commander(self, task_id: str, request: str, *, modules: Iterable[str] = (),
                      roles: Iterable[str] | None = None, max_agents: int = 5) -> tuple[AgentReport, ...]:
        """Return an offline plan without falsely claiming that work ran."""
        role_plan = self.plan_commander_roles(request, modules, max_agents=max_agents, roles=roles)
        reports = tuple(AgentReport(f"{item.role}-agent", item.role, task_id, "PLANNED",
                                    item.goal, next_action="run native Codex Commander")
                        for item in role_plan)
        self.changelog.append(ChangeLog(task_id, "commander-plan", "codex-self", "codex-self", (),
                                        request, ("roles planned",)))
        return reports

    def run_commander_auto(self, task_id: str, request: str, workspace: str | Path, *,
                           modules: Iterable[str] = (), roles: Iterable[str] | None = None,
                           max_agents: int = 5, timeout: float = 600, codex: str = "codex",
                           runtime_root: str | Path | None = None, native: bool = True,
                           model_configs: dict[str, dict] | None = None) -> dict:
        """Run a dynamic Commander using the current Codex CLI when available.

        This is the offline-first Commander path.  DeepSeek/local/GPT are not
        prerequisites.  Each Codex child gets a copied workspace, a role
        prompt, and its own bounded process.  The original workspace is never
        modified by this method; merging remains a reviewed confirmation step.
        """
        try:
            from .config_manager import apply_config_to_environment
        except ImportError:
            from config_manager import apply_config_to_environment
        apply_config_to_environment()
        try:
            configured_max_agents = int(os.environ.get("AGENTRELAY_COMMANDER_MAX_AGENTS", max_agents))
            if max_agents == 5:
                max_agents = max(1, min(5, configured_max_agents))
        except (TypeError, ValueError):
            pass
        role_plan = self.plan_commander_roles(request, modules, max_agents=max_agents, roles=roles)
        if not role_plan:
            raise ValueError("Commander 至少需要一个角色")
        if os.environ.get("AGENTRELAY_COMMANDER_CHILD") == "1":
            raise PermissionError("COMMANDER_RECURSION_DENIED: 子 Agent 不能再次启动 Commander")
        confirmation = self.confirmations.cards.get(task_id)
        if native and (confirmation is None or confirmation.status != "approved" or confirmation.action != "enable_commander"):
            raise PermissionError("Commander 必须先完成需求确认，并批准“启用审查官模式”确认卡")
        try:
            from .research_commander import CommanderRuntime, codex_cli_available
            from .commander_provider_pool import CommanderProviderCoordinator, ProviderProfile
        except ImportError:
            from research_commander import CommanderRuntime, codex_cli_available
            from commander_provider_pool import CommanderProviderCoordinator, ProviderProfile
        if not native or not codex_cli_available(codex):
            return {"mode": "offline-plan", "codex_available": codex_cli_available(codex),
                    "roles": [asdict(item) for item in role_plan],
                    "reports": self.run_commander(task_id, request, modules=modules,
                                                   roles=roles, max_agents=max_agents)}
        source = Path(workspace).resolve()
        if not source.is_dir():
            raise ValueError(f"Commander 源 workspace 不存在: {source}")
        runtime_path = Path(runtime_root).resolve() if runtime_root else source / ".agentrelay" / "commander"
        profiles = [
            ProviderProfile("deepseek-web", "web", login_command="agent_relay_login.py --provider deepseek"),
            ProviderProfile("qianwen-web", "web", login_command="agent_relay_login.py --provider qianwen"),
            ProviderProfile("kimi-web", "web", login_command="agent_relay_login.py --provider kimi"),
            ProviderProfile("local-llm", "local"),
            ProviderProfile("gpt-api", "api"),
        ]
        coordinator = CommanderProviderCoordinator(runtime_path, profiles)
        runtime = CommanderRuntime(runtime_path, max_agents=max_agents, coordinator=coordinator)
        configs = model_configs or {}
        agents = [runtime.create(item.role, task_id, model_config={"provider": "codex-self",
                    "task_id": task_id, "goal": item.goal, **dict(configs.get(item.role, {}))}) for item in role_plan]
        for agent in agents:
            runtime.prepare_workspace(agent, source)
            child = TaskContext(f"{task_id}:{agent.role}", str(agent.workspace), request)
            child.transition(TaskExecutionState.ANALYZING)
            child.transition(TaskExecutionState.EXECUTING)
            agent.context = child
            runtime._persist_child_context(agent)

        # Commander 只配置终端可执行文件路径；子 Agent 的启动参数由这里统一编排。
        configured_terminal = os.environ.get("AGENTRELAY_COMMANDER_TERMINAL_PATH", "").strip()
        # API/CLI 显式传入的路径优先；网页配置只覆盖默认的 `codex`。
        terminal = codex if codex != "codex" else (configured_terminal or codex)
        command_prefix = [terminal, "exec"]

        def command_factory(agent):
            goal = agent.model_config.get("goal", "完成分配任务")
            configured_provider = agent.model_config.get("provider")
            if configured_provider in {"deepseek-web", "qianwen-web", "kimi-web", "local-llm"}:
                default_provider = configured_provider
            else:
                default_provider = os.environ.get("AGENTRELAY_COMMANDER_DEFAULT_PROVIDER", "deepseek-web")
            provider_id = agent.model_config.get("consult_provider", default_provider)
            fallback_providers = tuple(agent.model_config.get("fallback_providers", ())) or tuple(
                item.strip() for item in os.environ.get(
                    "AGENTRELAY_COMMANDER_FALLBACK_PROVIDERS",
                    "qianwen-web,kimi-web,local-llm",
                ).split(",") if item.strip()
            )
            coordinator_cli = str(Path(__file__).with_name("commander_provider_call.py").resolve())
            assist_note = (
                f"当前主力角色仍是 {agent.model_config.get('assist_for_role')}；你现在只作为协助者，"
                "请提供分析、验证或补充修改建议，不得接管该角色，也不要把自己的角色改成目标角色。\n"
                if agent.model_config.get("assist_for") else ""
            )
            prompt = (
                f"你是 Commander 的 {agent.role} 子 Agent。\n"
                f"职责：{goal}\n"
                f"{assist_note}"
                f"指定 Provider：{provider_id}。如果该 Provider 是 web 或 local，"
                "你只能通过主 Agent/AgentRelay 的受控调用使用它；不要自行打开新的 Commander，"
                "不要调用任何 API Provider。\n"
                f"受控 Provider 入口（如确需咨询）：{coordinator_cli} --provider {provider_id} "
                + " ".join(f"--fallback-provider {item}" for item in fallback_providers) + " "
                + f"--task-id {task_id} --role {agent.role} --coordinator {runtime_path}\n"
                f"用户任务：{request}\n\n"
                "你在隔离 workspace 中工作，只处理自己的职责。可以读取和修改该隔离目录，"
                "不要访问父 workspace，不要 git commit，不要执行 git reset/checkout，不要调用外部模型。"
                "完成后用简洁文字报告：完成内容、修改文件、验证结果、风险和建议。"
            )
            model = agent.model_config.get("model")
            return list(command_prefix) + (["-m", str(model)] if model else []) + ["--skip-git-repo-check", "--sandbox", "workspace-write",
                    "--approve-for-me", "--ephemeral", "--color", "never", prompt]

        runtime.run_processes(agents, command_factory, timeout=timeout)
        for agent in agents:
            if agent.context is not None:
                target = TaskExecutionState.SUCCESS if agent.status == "SUCCESS" else TaskExecutionState.FAILED
                if target == TaskExecutionState.SUCCESS:
                    agent.context.transition(TaskExecutionState.VERIFYING)
                agent.context.transition(target)
                runtime._persist_child_context(agent)
            runtime.save(agent, runtime_path / "agents")
        merged = runtime.merge_results(agents)
        merge_plan = runtime.build_merge_plan(agents, source)
        merge_confirmation = None
        if merge_plan.get("entries"):
            merge_id = f"{task_id}:merge"
            merge_confirmation = ConfirmationCard(
                merge_id, "execute", tuple(sorted({item["path"] for item in merge_plan["entries"]})),
                ("主审查官已检查各子 Agent 输出", "只合并审查通过的子 Agent 文件",
                 "主工作区当前文件未被用户同时修改"), "pending",
                request=f"合并 Commander 子 Agent 结果：{request}", workspace=str(source),
                allowed_files=tuple(sorted({item["path"] for item in merge_plan["entries"]})),
                verification_commands=("git diff --check",))
            self.confirmations.create(merge_confirmation)
        merged["merge_plan"] = merge_plan
        merged["merge_confirmation"] = merge_confirmation
        merged["summary"] = runtime.summarize(agents, merge_plan)
        self.changelog.append(ChangeLog(task_id, "commander", "codex-self", "codex-self", (),
                                        request, ("native Codex reports collected",), event_type="EXECUTION"))
        return {"mode": "native-codex", "codex_available": True,
                "roles": [asdict(item) for item in role_plan], "agents": agents, "merged": merged,
                "runtime_root": str(runtime_path)}

    def merge_commander_results(self, task_id: str, workspace: str | Path,
                                runtime_root: str | Path, *, confirmed: bool = False) -> dict:
        """Review and merge a persisted Commander plan into the main workspace."""
        try:
            from .research_commander import CommanderRuntime
        except ImportError:
            from research_commander import CommanderRuntime
        merge_id = f"{task_id}:merge"
        card = self.confirmations.cards.get(merge_id)
        if card is None:
            raise KeyError(f"Commander 合并确认卡不存在: {merge_id}")
        if not confirmed or card.status != "approved":
            raise PermissionError("Commander 合并必须先批准合并确认卡")
        runtime = CommanderRuntime(runtime_root)
        try:
            plan = json.loads(runtime.merge_plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Commander 合并计划不可读: {exc}") from exc
        self.confirmations.transition(merge_id, "executing")
        result = runtime.apply_merge_plan(plan, confirmed=True)
        status = "completed" if result.get("status") == "merged" else "failed"
        if status == "completed":
            self.confirmations.transition(merge_id, "completed")
        else:
            current = self.confirmations.cards[merge_id]
            failed = ConfirmationCard(current.task_id, current.action, current.scope,
                                      current.acceptance_criteria, "failed", current.request,
                                      current.workspace, current.allowed_files, current.edits,
                                      current.verification_commands, current.attempts,
                                      str(result.get("errors") or result.get("skipped") or "merge failed"),
                                      current.recovery_reason, current.attempt_history, current.next_action)
            self.confirmations.cards[merge_id] = failed
            self.confirmations._persist(failed)
        summary = runtime.summarize([], plan, result)
        summary["status"] = status
        summary["message"] = ("主审查官已完成合并：" + ", ".join(result.get("merged_files", []))
                              if status == "completed" else "主审查官未完成合并，请检查冲突和用户并发修改。")
        return {"task_id": task_id, "status": status, "merge": result, "summary": summary,
                "confirmation": self.confirmations.cards[merge_id]}

    def run_commander_processes(self, task_id: str, request: str, root: str | Path,
                                command_factory, *, roles=("frontend", "backend", "git-devops"),
                                max_agents: int = 5, timeout: float = 120,
                                model_configs: dict[str, dict] | None = None) -> dict:
        """Run an explicitly requested Commander process group.

        ``command_factory`` receives each isolated CommanderAgent and returns
        argv.  The default workflow never starts child processes implicitly;
        this method is the opt-in bridge for a caller that has approved it.
        """
        try:
            from .research_commander import CommanderRuntime
        except ImportError:
            from research_commander import CommanderRuntime
        runtime = CommanderRuntime(root, max_agents=max_agents)
        configs = model_configs or {}
        agents = [runtime.create(role, task_id, model_config=configs.get(role, {})) for role in roles]
        for agent in agents:
            child = TaskContext(f"{task_id}:{agent.role}", str(agent.workspace), request)
            child.transition(TaskExecutionState.ANALYZING)
            child.transition(TaskExecutionState.EXECUTING)
            agent.context = child
            runtime._persist_child_context(agent)
        runtime.run_processes(agents, command_factory, timeout=timeout)
        for agent in agents:
            if agent.context is not None:
                target = TaskExecutionState.SUCCESS if agent.status == "SUCCESS" else TaskExecutionState.FAILED
                if target == TaskExecutionState.SUCCESS:
                    agent.context.transition(TaskExecutionState.VERIFYING)
                agent.context.transition(target)
                runtime._persist_child_context(agent)
            runtime.save(agent, Path(root) / "agents")
        merged = runtime.merge_results(agents)
        self.changelog.append(ChangeLog(task_id, "commander", "codex-self", "codex-subagent", (),
                                        request, ("processes collected",), event_type="EXECUTION"))
        return {"agents": agents, "merged": merged}

    def rollback_plan(self, task_id: str, files: Iterable[str], current: dict[str, str], checkpoint: dict[str, str]) -> dict:
        entries = []
        for path in files:
            if path in checkpoint:
                entries.append({"path": path, "before_content": checkpoint[path],
                                "after_fingerprint": _digest(current[path]) if path in current else None,
                                "existed_before": True})
        return {"task_id": task_id, "files": entries, "requires_confirmation": True,
                "method": "per-file restore; never git reset --hard"}

    def apply_rollback(self, task_id: str, plan: dict, *, confirmed: bool = False, workspace: str | Path | None = None) -> ChangeLog:
        """Restore task files only when their post-edit fingerprint is unchanged."""
        if not confirmed:
            raise PermissionError("回滚必须先获得明确确认")
        restored, conflicts, skipped, missing, errors = [], [], [], [], []
        raw_files = plan.get("files", [])
        if isinstance(raw_files, dict):
            raw_files = [{"path": p, "before_content": c, "after_fingerprint": None, "existed_before": True}
                         for p, c in raw_files.items()]
        root = Path(workspace).resolve() if workspace else None
        for entry in raw_files:
            path = entry.get("path", "")
            target = Path(path)
            if root:
                try:
                    target = _safe_file(root, path)
                except PermissionError:
                    errors.append(path); continue
            before = entry.get("before_content")
            expected_after = entry.get("after_fingerprint")
            current = _file_digest(target)
            if expected_after is not None and current != expected_after:
                conflicts.append(path); continue
            if before is None:
                if entry.get("existed_before", False):
                    errors.append(path)
                elif current is None:
                    missing.append(path)
                else:
                    target.unlink(); restored.append(path)
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(before, encoding="utf-8")
                restored.append(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        detail = {"restored_files": restored, "conflict_files": conflicts,
                  "skipped_files": skipped, "missing_files": missing, "errors": errors}
        detail["status"] = "rollback_conflict" if conflicts else ("rolled_back" if restored and not errors else "rollback_failed")
        log = ChangeLog(task_id, "rollback", "codex-self", "codex-self", tuple(restored),
                        "按 checkpoint 逐文件恢复", rollback=json.dumps(detail, ensure_ascii=False))
        self.changelog.append(log)
        return log

    def restore_checkpoint(self, task_id: str, checkpoint_path: str | Path, workspace: str | Path,
                           *, confirmed: bool = False, state: str = "after",
                           expected_current: dict[str, str | None] | None = None) -> ChangeLog:
        """Restore a selected retained version only after explicit confirmation."""
        if not confirmed:
            raise PermissionError("恢复历史版本需要 confirmed=True")
        if self.checkpoints is None:
            raise RuntimeError("未配置 CheckpointStore")
        detail = self.checkpoints.restore(checkpoint_path, workspace, state=state,
                                          expected_current=expected_current)
        log = ChangeLog(task_id, "restore", "codex-self", "codex-self",
                        tuple(detail.get("restored_files", ())),
                        "恢复用户确认的历史版本", rollback=json.dumps(detail, ensure_ascii=False),
                        event_type="ROLLBACK", rollback_available=True)
        self.changelog.append(log)
        return log


class WorkflowEngine:
    """Small stable facade for the end-to-end offline task workflow.

    It intentionally delegates to ``Orchestrator`` so existing callers keep
    their behavior while integrations get one entry point for task intake and
    explicit confirmation execution.
    """

    def __init__(self, orchestrator: Orchestrator | None = None, runtime_dir: str | Path | None = None,
                 agent_provider=None, task_executor=None, provider_options=None, executor_options=None,
                 escalation_provider=None, enable_local_model: bool = False):
        try:
            from .agent_provider import AgentProviderRegistry, LocalAgentProvider, LocalTaskExecutor, ProviderPlanExecutor, EscalationManager, ExecutionResult, RetryPolicy
            from .runtime_manager import RuntimeManager
            from .recovery_manager import RecoveryManager
        except ImportError:
            from agent_provider import AgentProviderRegistry, LocalAgentProvider, LocalTaskExecutor, ProviderPlanExecutor, EscalationManager, ExecutionResult, RetryPolicy
            from runtime_manager import RuntimeManager
            from recovery_manager import RecoveryManager
        self.orchestrator = orchestrator or Orchestrator()
        self.provider_registry = AgentProviderRegistry()
        self.provider_registry.register("local", LocalAgentProvider())
        self.agent_provider = agent_provider or self.provider_registry.get("local")
        try: from .model_router import ModelRouter, ProviderRouteMemory
        except ImportError: from model_router import ModelRouter, ProviderRouteMemory
        self.provider_options = dict(provider_options or {"local": self.provider_registry.get("local")})
        # The registry remains the canonical lookup point for all configured
        # providers. DeepSeek is advisory; only edit-capable providers receive
        # an executor bridge below.
        for name, provider in self.provider_options.items():
            self.provider_registry.register(name, provider)
        if enable_local_model and "local-llm" not in self.provider_options:
            self.provider_options["local-llm"] = LocalLLMProvider()
        self.task_executor = task_executor or LocalTaskExecutor(self.orchestrator)
        self.executor_options = dict(executor_options or {})
        if enable_local_model and "local-llm" not in self.executor_options:
            self.executor_options["local-llm"] = ProviderPlanExecutor(
                self.provider_options["local-llm"], self.task_executor)
        for name in ("local-llm", "gpt-api"):
            if name in self.provider_options and name not in self.executor_options:
                self.executor_options[name] = ProviderPlanExecutor(
                    self.provider_options[name], self.task_executor)
        self.runtime_manager = RuntimeManager(Path(runtime_dir or "runtime") / "tasks")
        self.route_memory = ProviderRouteMemory(
            self.runtime_manager.directory.parent / "provider_routes.json")
        self.model_router = ModelRouter(self.provider_options, route_memory=self.route_memory)
        self.last_route = None
        self.retry_policy = RetryPolicy()
        configured_escalation = escalation_provider or self.provider_options.get("deepseek")
        self.escalation_manager = EscalationManager(configured_escalation, self.agent_provider)
        self.recovery_manager = RecoveryManager(self.runtime_manager)
        self.contexts: dict[str, TaskContext] = {}

    @classmethod
    def from_environment(cls, *, orchestrator: Orchestrator | None = None,
                         runtime_dir: str | Path | None = None) -> "WorkflowEngine":
        """Build optional providers from environment without making them required."""
        try:
            from .config_manager import apply_config_to_environment
        except ImportError:
            from config_manager import apply_config_to_environment
        apply_config_to_environment()
        try:
            from .agent_adapter import DeepSeekAdapter, OpenAICompatibleAdapter
            from .agent_provider import DeepSeekProvider, LocalLLMProvider, OpenAIAPIProvider
        except ImportError:
            from agent_adapter import DeepSeekAdapter, OpenAICompatibleAdapter
            from agent_provider import DeepSeekProvider, LocalLLMProvider, OpenAIAPIProvider
        providers = {"local": LocalAgentProvider()}
        local_endpoint = os.environ.get("AGENTRELAY_LOCAL_LLM_ENDPOINT")
        if local_endpoint:
            local_adapter = OpenAICompatibleAdapter(
                local_endpoint,
                model=os.environ.get("AGENTRELAY_LOCAL_LLM_MODEL", ""),
                timeout=int(os.environ.get("AGENTRELAY_LOCAL_LLM_TIMEOUT", "60")))
            providers["local-llm"] = LocalLLMProvider(adapter=local_adapter)
        deepseek_enabled = os.environ.get("AGENTRELAY_DEEPSEEK_ENABLED", "0").lower() in {"1", "true", "yes"}
        if deepseek_enabled:
            adapter = DeepSeekAdapter(
                enabled=True,
                mode=os.environ.get("AGENTRELAY_DEEPSEEK_MODE", "playwright"),
                endpoint=os.environ.get("AGENTRELAY_DEEPSEEK_ENDPOINT", ""),
                api_key=os.environ.get("AGENTRELAY_DEEPSEEK_API_KEY"),
                model=os.environ.get("AGENTRELAY_DEEPSEEK_MODEL", "deepseek"),
                timeout=int(os.environ.get("AGENTRELAY_DEEPSEEK_TIMEOUT", "30")))
            providers["deepseek"] = DeepSeekProvider(adapter=adapter)
        api_endpoint = os.environ.get("AGENTRELAY_API_ENDPOINT")
        api_key = os.environ.get("AGENTRELAY_API_KEY")
        if api_endpoint and api_key:
            providers["gpt-api"] = OpenAIAPIProvider(adapter=OpenAICompatibleAdapter(
                api_endpoint, api_key=api_key,
                model=os.environ.get("AGENTRELAY_API_MODEL", "gpt-5.5"),
                timeout=int(os.environ.get("AGENTRELAY_API_TIMEOUT", "60"))))
        # Custom API models are provider-neutral.  A user can configure any
        # OpenAI-compatible relay/model without hard-coding its brand here:
        # AGENTRELAY_API_PROVIDERS_JSON='[{"id":"kimi-api","endpoint":"...",
        # "api_key_env":"KIMI_API_KEY","model":"..."}]'.
        try:
            custom_apis = json.loads(os.environ.get("AGENTRELAY_API_PROVIDERS_JSON", "[]"))
        except json.JSONDecodeError:
            custom_apis = []
        custom_items = []
        for item in custom_apis if isinstance(custom_apis, list) else []:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip()
            endpoint = str(item.get("endpoint") or os.environ.get(str(item.get("endpoint_env") or ""), "")).strip()
            api_key = str(item.get("api_key") or os.environ.get(str(item.get("api_key_env") or ""), "")).strip()
            if not provider_id or not endpoint or not api_key:
                continue
            custom_items.append((provider_id, endpoint, api_key, item))

        # 上游原生格式不是 chat 的供应商（responses / anthropic）走内置
        # 协议路由做双向转换；chat 格式直连上游，不起路由
        routed = [entry for entry in custom_items
                  if str(entry[3].get("format") or "chat") != "chat"]
        router_url = None
        if routed:
            try:
                from .api_format_router import ensure_router
            except ImportError:
                from api_format_router import ensure_router
            router_url = ensure_router([
                {"id": pid, "endpoint": ep, "api_key": key,
                 "model": str(it.get("model") or pid),
                 "format": str(it.get("format") or "chat"),
                 "timeout": int(it.get("timeout") or 300)}
                for pid, ep, key, it in routed
            ])

        for provider_id, endpoint, api_key, item in custom_items:
            if router_url and str(item.get("format") or "chat") != "chat":
                # 路由按 model（= provider_id）选择上游并注入密钥
                providers[provider_id] = OpenAIAPIProvider(adapter=OpenAICompatibleAdapter(
                    router_url, api_key="", model=provider_id,
                    timeout=int(item.get("timeout", 60))))
            else:
                providers[provider_id] = OpenAIAPIProvider(adapter=OpenAICompatibleAdapter(
                    endpoint, api_key=api_key, model=str(item.get("model") or ""),
                    timeout=int(item.get("timeout", 60))))
        return cls(orchestrator=orchestrator, runtime_dir=runtime_dir,
                   provider_options=providers, enable_local_model=bool(local_endpoint))

    def submit(self, task_id: str, request: str, **kwargs) -> dict:
        context = self.runtime_manager.create_context(task_id, request, kwargs.get("workspace"))
        context.project = str(kwargs.get("project") or "default")
        context.session_id = kwargs.get("session_id")
        self.contexts[task_id] = context
        self.runtime_manager.update_state(context, TaskExecutionState.ANALYZING)
        if self.orchestrator.memory:
            hits = self.orchestrator.memory.search(request, project=context.project)
            context.memory_hits = [asdict(item) for item in hits]
        # Routing can select an optional provider, but cannot change task state
        # or bypass the default local provider when it is unavailable.
        decision = self.agent_provider.analyze(context)
        plan = self.orchestrator.analyzer.analyze(request)
        choice = self.model_router.choose(plan.task_type, plan.difficulty,
                                          needs_web=plan.task_type == "TECHNICAL_RESEARCH",
                                          request=request)
        if choice.provider == "local-llm" and "local-llm" not in self.provider_options:
            choice = type(choice)("local", "本地模型未启用，回退离线规则")
        self.last_route = choice
        selected = self.provider_options.get(choice.provider, self.agent_provider)
        if selected is not self.agent_provider:
            decision = selected.analyze(context)
        # A provider may return a successful local fallback together with an
        # error marker. Treat that as a routing failure so the persisted plan
        # does not claim that an unavailable model handled the task.
        if getattr(decision, "error", None):
            fallback_choice = type(choice)("local", f"{choice.provider} 不可用，回退离线规则", score=5.0)
            choice = fallback_choice
            selected = self.provider_options.get("local", self.agent_provider)
            decision = selected.analyze(context)
            self.last_route = choice
        plan_response = selected.plan(context)
        if getattr(plan_response, "error", None) and selected is not self.agent_provider:
            fallback_choice = type(choice)("local", f"{choice.provider} 计划失败，回退离线规则", score=5.0)
            choice = fallback_choice
            selected = self.provider_options.get("local", self.agent_provider)
            plan_response = selected.plan(context)
            self.last_route = choice
        provider_response = {
            "provider": choice.provider,
            "analysis": decision.content,
            "analysis_data": decision.structured_data,
            "plan": plan_response.content,
            "plan_data": plan_response.structured_data,
            "error": decision.error or plan_response.error,
        }
        # A local/remote provider may propose structured edits for a SIMPLE_TASK.
        # They are accepted only inside the caller's explicit file scope; all
        # writes still happen in SelfWorker and its checkpoint path.
        execution_kwargs = dict(kwargs)
        proposed = plan_response.structured_data if hasattr(plan_response, "structured_data") else None
        proposed_edits = proposed.get("edits") if isinstance(proposed, dict) else None
        allowed = set(kwargs.get("allowed_files", ()))
        if isinstance(proposed_edits, dict) and not (set(proposed_edits) - allowed) and all(isinstance(value, str) for value in proposed_edits.values()):
            execution_kwargs["edits"] = proposed_edits
        context.agent_execution_plan = {"provider": choice.provider, "reason": choice.reason,
                                        "analysis": decision.content,
                                        "plan": plan_response.content,
                                        "plan_data": plan_response.structured_data,
                                        "memory_hits": context.memory_hits,
                                        "memory_guidance": (
                                            "优先参考已验证历史方案，仍需当前验证"
                                            if context.memory_hits else "无匹配历史方案")}
        result = self.orchestrator.execute_task(task_id, request, **execution_kwargs)
        result["provider_response"] = provider_response
        if "confirmation" in result:
            context.requirement_card = asdict(result["card"]) if "card" in result else None
            context.confirmation_card = asdict(result["confirmation"])
            context.verification_commands = list(result["confirmation"].verification_commands)
            self.runtime_manager.update_state(context, TaskExecutionState.WAITING_CONFIRMATION)
        else:
            context.changed_files = list(result.get("result", AgentResult(task_id, "success")).changed_files)
            self.runtime_manager.update_state(context, TaskExecutionState.SUCCESS)
            verification = result.get("verification")
            if verification is not None:
                self.route_memory.record(request, choice.provider,
                                         success=bool(getattr(verification, "passed", False)))
        context.agent_execution_plan = {**(context.agent_execution_plan or {}), "selected_provider": choice.provider}
        self.runtime_manager.save_context(context)
        return result

    def _record_provider_outcome(self, context: TaskContext, success: bool) -> None:
        provider = (context.agent_execution_plan or {}).get("selected_provider", "local")
        self.route_memory.record(context.user_request, str(provider), success=success)

    def _executor_for(self, context: TaskContext):
        """Select an explicitly supplied executor without letting providers execute code."""
        selected = (context.agent_execution_plan or {}).get("selected_provider")
        return self.executor_options.get(selected, self.task_executor)

    def evaluate_escalation(self, context: TaskContext):
        """Consult an explicitly configured fallback once per trigger level."""
        if not context.need_escalation:
            return None
        plan = context.agent_execution_plan or {}
        previous = plan.get("escalation", {})
        marker = (context.retry_count, context.weight)
        if tuple(previous.get("trigger", ())) == marker:
            return previous
        response = self.escalation_manager.consult(context)
        context.agent_execution_plan = {
            **plan,
            "escalation": {"success": response.success, "content": response.content,
                           "error": response.error, "trigger": list(marker)},
        }
        self.runtime_manager.save_context(context)
        self.runtime_manager.record_event(context, {
            "event_type": "ESCALATION", "success": response.success,
            "error": response.error, "trigger": list(marker),
        })
        if response.success and context.status == TaskExecutionState.FAILED:
            self._retry_after_escalation(context)
        return context.agent_execution_plan["escalation"]

    def _retry_after_escalation(self, context: TaskContext):
        """Retry an already-approved task after external advice.

        Advice never supplies executable code here. The retry reuses the
        previously approved confirmation card and therefore stays within the
        same allowed_files/checkpoint boundary.
        """
        card = context.confirmation_card or {}
        if card.get("status") == "failed" and context.task_id in self.orchestrator.confirmations.cards:
            try:
                updated = self.orchestrator.confirmations.recover(
                    context.task_id, "retry", "external advice retry")
                context.confirmation_card = asdict(updated)
                card = context.confirmation_card
            except (KeyError, ValueError):
                return None
        if card.get("status") not in {"approved", "executing"}:
            return None
        self.runtime_manager.update_state(context, TaskExecutionState.EXECUTING)
        result = self._executor_for(context).execute(context)
        try:
            from .agent_provider import ExecutionResult
        except ImportError:
            from agent_provider import ExecutionResult
        if not isinstance(result, ExecutionResult):
            result = ExecutionResult(bool(getattr(result, "success", False)), raw=result)
        context.changed_files = list(result.changed_files)
        self.runtime_manager.update_state(context, TaskExecutionState.VERIFYING)
        if result.success:
            context.weight = 1
            context.need_escalation = False
            self.runtime_manager.update_state(context, TaskExecutionState.SUCCESS)
            self._record_provider_outcome(context, True)
            self.runtime_manager.record_event(context, {
                "event_type": "ESCALATION_RETRY", "result": "success", "weight": 1,
            })
        else:
            self.handle_failure(context, result.error or "escalation retry failed")
        return result

    def approve_and_execute(self, task_id: str, **kwargs) -> dict:
        try: from .agent_provider import ExecutionResult
        except ImportError: from agent_provider import ExecutionResult
        context = self.contexts.get(task_id)
        if context is None:
            raise KeyError(f"TaskContext 不存在: {task_id}")
        if context.status != TaskExecutionState.WAITING_CONFIRMATION:
            raise ValueError(f"任务状态 {context.status.value} 不允许批准")
        current_card = self.orchestrator.confirmations.cards.get(task_id)
        if current_card is None:
            raise KeyError(f"确认卡不存在: {task_id}")
        if current_card.action == "confirm_requirement":
            next_card = self.orchestrator.confirmations.advance_requirement(task_id)
            context.confirmation_card = asdict(next_card)
            self.runtime_manager.save_context(context)
            return {"status": "awaiting_execution_confirmation", "confirmation": next_card}
        self.orchestrator.confirmations.transition(task_id, "approved")
        context.confirmation_card = asdict(self.orchestrator.confirmations.cards[task_id])
        self.runtime_manager.update_state(context, TaskExecutionState.APPROVED)
        if current_card.action == "enable_commander":
            self.runtime_manager.save_context(context)
            return {"status": "commander_approved", "confirmation": self.orchestrator.confirmations.cards[task_id],
                    "message": "审查官模式已获授权；请由主 Agent 启动动态角色规划。"}
        self.runtime_manager.update_state(context, TaskExecutionState.EXECUTING)
        context.confirmation_card = asdict(self.orchestrator.confirmations.cards[task_id])
        executor = self._executor_for(context)
        result = executor.execute(context)
        if not isinstance(result, ExecutionResult):
            result = ExecutionResult(result.get("status") == "completed", tuple(result["result"].changed_files), raw=result)
        context.changed_files = list(result.changed_files)
        if task_id in self.orchestrator.confirmations.cards:
            context.confirmation_card = asdict(self.orchestrator.confirmations.cards[task_id])
        self.runtime_manager.save_context(context)
        self.runtime_manager.update_state(context, TaskExecutionState.VERIFYING)
        if result.success:
            self.runtime_manager.update_state(context, TaskExecutionState.SUCCESS)
        else:
            self.handle_failure(context, result.error or "execution failed")
        if self.orchestrator.memory:
            self.orchestrator.memory.record_outcome(
                context.user_request,
                [result.output or ("执行成功" if result.success else result.error or "执行失败")],
                success=result.success, changed_files=list(result.changed_files),
                project=context.project, session_id=context.session_id)
        self._record_provider_outcome(context, bool(result.success))
        return result.raw if result.raw is not None else result

    def request_restore(self, task_id: str, checkpoint_path: str | Path,
                        workspace: str | Path) -> ConfirmationCard:
        """Create a pending confirmation card for a selected checkpoint."""
        if self.orchestrator.checkpoints is None:
            raise RuntimeError("未配置 CheckpointStore")
        payload = self.orchestrator.checkpoints.read(checkpoint_path)
        files = tuple(str(item.get("path", "")) for item in payload.get("files", ()))
        card = ConfirmationCard(task_id, "restore", files,
                                ("只恢复所选 checkpoint 文件", "冲突文件不得覆盖"),
                                request=f"恢复 checkpoint {Path(checkpoint_path).name}",
                                workspace=str(workspace), allowed_files=files)
        return self.orchestrator.confirmations.create(card)

    def approve_restore(self, task_id: str, checkpoint_path: str | Path,
                        workspace: str | Path, *, expected_current: dict[str, str | None] | None = None) -> ChangeLog:
        card = self.orchestrator.confirmations.cards.get(task_id)
        if card is None or card.action != "restore" or card.status != "pending":
            raise ValueError("恢复确认卡不存在或不在 pending 状态")
        self.orchestrator.confirmations.transition(task_id, "approved")
        self.orchestrator.confirmations.transition(task_id, "executing")
        try:
            log = self.orchestrator.restore_checkpoint(
                task_id, checkpoint_path, workspace, confirmed=True,
                state="after", expected_current=expected_current)
            detail = json.loads(log.rollback or "{}")
            self.orchestrator.confirmations.transition(task_id, "completed" if detail.get("status") == "rolled_back" else "failed")
            return log
        except Exception as exc:
            current = self.orchestrator.confirmations.cards[task_id]
            failed = ConfirmationCard(current.task_id, current.action, current.scope,
                                      current.acceptance_criteria, "failed", current.request,
                                      current.workspace, current.allowed_files, current.edits,
                                      current.verification_commands, current.attempts,
                                      str(exc), current.recovery_reason, current.attempt_history)
            self.orchestrator.confirmations.cards[task_id] = failed
            self.orchestrator.confirmations._persist(failed)
            raise

    def handle_failure(self, context: TaskContext, error: str, *, elapsed_seconds: int = 0) -> TaskContext:
        context.record_failure(elapsed_seconds=elapsed_seconds)
        escalation = self.escalation_manager.consult(context)
        context.agent_execution_plan = {
            **(context.agent_execution_plan or {}),
            "escalation": {"success": escalation.success, "content": escalation.content,
                           "error": escalation.error},
        }
        self.runtime_manager.save_context(context)
        if context.status not in {TaskExecutionState.FAILED, TaskExecutionState.ROLLBACK}:
            self.runtime_manager.update_state(context, TaskExecutionState.FAILED)
        if context.changed_files:
            self.runtime_manager.update_state(context, TaskExecutionState.ROLLBACK)
            self._executor_for(context).rollback(context)
            self.runtime_manager.update_state(context, TaskExecutionState.ROLLED_BACK)
        return context

    def resume(self, task_id: str):
        """Continue an interrupted execution and persist its terminal state."""
        try: from .agent_provider import ExecutionResult
        except ImportError: from agent_provider import ExecutionResult
        context = self.contexts.get(task_id) or self.runtime_manager.load_context(task_id)
        self.contexts[task_id] = context
        if context.status == TaskExecutionState.FAILED:
            if self.retry_policy.should_retry(context):
                self.runtime_manager.update_state(context, TaskExecutionState.EXECUTING)
            else:
                return context
        if context.status in {TaskExecutionState.EXECUTING, TaskExecutionState.VERIFYING}:
            result = self._executor_for(context).execute(context)
            if not isinstance(result, ExecutionResult):
                result = ExecutionResult(bool(getattr(result, "success", False)), raw=result)
            context.changed_files = list(result.changed_files)
            self.runtime_manager.save_context(context)
            if context.status == TaskExecutionState.EXECUTING:
                self.runtime_manager.update_state(context, TaskExecutionState.VERIFYING)
            if result.success:
                self.runtime_manager.update_state(context, TaskExecutionState.SUCCESS)
            else:
                self.handle_failure(context, result.error or "execution failed")
            return result
        raise ValueError(f"任务状态 {context.status.value} 不支持恢复")

    def rollback(self, task_id: str) -> TaskContext:
        context = self.contexts.get(task_id) or self.runtime_manager.load_context(task_id)
        self.runtime_manager.update_state(context, TaskExecutionState.ROLLBACK)
        self.task_executor.rollback(context)
        return self.runtime_manager.update_state(context, TaskExecutionState.ROLLED_BACK)


# Public compatibility exports for integrations importing from core.
try:
    from .agent_provider import (AgentProvider, AgentResponse, AgentAdapter, DeepSeekAdapter, PromptTemplate, LocalAgentProvider, LocalLLMProvider, DeepSeekProvider,
                                 AgentProviderRegistry, TaskExecutor, LocalTaskExecutor, ProviderPlanExecutor, OpenAIAPIProvider, EscalationManager)
except ImportError:
    from agent_provider import (AgentProvider, AgentResponse, AgentAdapter, DeepSeekAdapter, PromptTemplate, LocalAgentProvider, LocalLLMProvider, DeepSeekProvider,
                                AgentProviderRegistry, TaskExecutor, LocalTaskExecutor, ProviderPlanExecutor, OpenAIAPIProvider, EscalationManager)
