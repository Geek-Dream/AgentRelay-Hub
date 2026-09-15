"""Offline-first research and commander foundations."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json, os, shutil, subprocess, uuid, hashlib
import time
from typing import Iterable
from urllib.request import Request, urlopen

@dataclass(frozen=True)
class ResearchReport:
    query: str
    sources: tuple[dict, ...] = ()
    conclusions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"query": self.query, "sources": list(self.sources),
                "conclusions": list(self.conclusions), "limitations": list(self.limitations)}

class ResearchAgent:
    def __init__(self, searchers=None): self.searchers = searchers or {}
    def search(self, query: str, *, sources=("local",)) -> ResearchReport:
        results=[]
        for source in sources:
            if source in self.searchers:
                try: results.append({"source": source, "query": query, "status": "ok", "results": self.searchers[source](query)})
                except Exception as exc: results.append({"source": source, "query": query, "status": "failed", "error": str(exc)})
            else: results.append({"source": source, "query": query, "status": "not_configured" if source != "local" else "offline"})
        conclusions = tuple(str(item.get("results", ""))[:500] for item in results if item.get("status") == "ok") or ("未配置联网工具，保留离线研究报告。",)
        limits = tuple("外部搜索未启用" for item in results if item.get("status") != "ok")
        return ResearchReport(query, tuple(results), conclusions, limits)

    def search_configured(self, query: str) -> ResearchReport:
        """Use all explicitly configured standard sources, never implicit network."""
        return self.search(query, sources=tuple(self.searchers) or ("local",))

class HttpSearchAdapter:
    """Optional JSON search endpoint; disabled unless explicitly enabled."""
    def __init__(self, endpoint: str, *, enabled=False, timeout=10): self.endpoint, self.enabled, self.timeout = endpoint, enabled, timeout
    def search(self, query: str):
        if not self.enabled: raise RuntimeError("搜索适配器未启用")
        url = self.endpoint + ("&" if "?" in self.endpoint else "?") + "q=" + __import__('urllib.parse', fromlist=['quote']).quote(query)
        with urlopen(Request(url, headers={"Accept":"application/json"}), timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

class WebSearchAdapter(HttpSearchAdapter): pass
class DocumentationSearchAdapter(HttpSearchAdapter): pass
class GitHubSearchAdapter(HttpSearchAdapter): pass

def configured_searchers(environ=None):
    env = environ or __import__('os').environ
    return standard_searchers(
        web=WebSearchAdapter(env["AGENTRELAY_WEB_SEARCH_ENDPOINT"], enabled=env.get("AGENTRELAY_SEARCH_ENABLED") == "1") if env.get("AGENTRELAY_WEB_SEARCH_ENDPOINT") else None,
        docs=DocumentationSearchAdapter(env["AGENTRELAY_DOCS_SEARCH_ENDPOINT"], enabled=env.get("AGENTRELAY_SEARCH_ENABLED") == "1") if env.get("AGENTRELAY_DOCS_SEARCH_ENDPOINT") else None,
        github=GitHubSearchAdapter(env["AGENTRELAY_GITHUB_SEARCH_ENDPOINT"], enabled=env.get("AGENTRELAY_SEARCH_ENABLED") == "1") if env.get("AGENTRELAY_GITHUB_SEARCH_ENDPOINT") else None)

def standard_searchers(*, web=None, docs=None, github=None):
    """Build named search sources without requiring network credentials."""
    return {name: adapter.search for name, adapter in (("web", web), ("docs", docs), ("github", github)) if adapter is not None}

@dataclass
class CommanderAgent:
    agent_id: str
    role: str
    workspace: Path
    budget: int = 1
    status: str = "CREATED"
    result: object = None
    owner: str = "commander"
    process_id: int | None = None
    model_config: dict = field(default_factory=dict)
    context: object = field(default=None, repr=False)
    # The primary role never changes.  An idle agent may run a separate
    # assistance pass for another role, but it remains accountable for its
    # original role and result.
    assist_for: str | None = None
    assist_history: list[dict] = field(default_factory=list)
    assist_result: object = None


@dataclass(frozen=True)
class CommanderRolePlan:
    role: str
    goal: str


ROLE_GOALS = {
    "frontend-style": "检查并修改前端样式、布局、主题和交互视觉",
    "frontend-script": "检查并修改前端脚本、组件状态和接口调用",
    "backend-db": "检查数据库、缓存、消息队列、迁移和配置变更",
    "backend-code": "检查后端 API、业务逻辑、服务代码和测试",
    "git-audit": "审查 diff、依赖、测试、风险和回滚边界",
}


def plan_commander_roles(request: str, modules: Iterable[str] = (), *, max_agents: int = 5,
                         requested_roles: Iterable[str] | None = None) -> tuple[CommanderRolePlan, ...]:
    """Choose only the roles justified by the request.

    ``frontend``/``backend``/``git-devops`` remain accepted aliases for
    compatibility, but the default plan is granular and can contain one to
    five roles.  No model availability is consulted here: roles are work
    assignments, while the runner chooses Codex, a local model, or an API.
    """
    limit = max(1, min(5, int(max_agents)))
    if requested_roles:
        roles = [str(role) for role in requested_roles if str(role)]
    else:
        text = (str(request) + " " + " ".join(str(item) for item in modules)).lower()
        roles = []
        if any(word in text for word in ("样式", "css", "主题", "布局", "按钮", "视觉", "style")):
            roles.append("frontend-style")
        if any(word in text for word in ("前端", "vue", "react", "组件", "脚本", "javascript", "typescript", "页面")):
            roles.append("frontend-script")
        if any(word in text for word in ("数据库", "sql", "redis", "缓存", "mq", "kafka", "rabbitmq", "迁移")):
            roles.append("backend-db")
        if any(word in text for word in ("后端", "接口", "api", "java", "服务", "业务", "backend")):
            roles.append("backend-code")
        if any(word in text for word in ("git", "审计", "检查", "验证", "测试", "部署", "devops", "review")):
            roles.append("git-audit")
        if not roles:
            if any(word in text for word in ("跨模块", "大型", "项目级", "重构", "架构")):
                roles = ["frontend-script", "backend-code", "git-audit"]
            else:
                roles = ["backend-code", "git-audit"]
    aliases = {"frontend": "frontend-script", "backend": "backend-code", "git-devops": "git-audit", "git": "git-audit"}
    normalized = []
    for role in roles:
        role = aliases.get(role, role)
        if role not in ROLE_GOALS:
            role = role[:64]
        if role not in normalized:
            normalized.append(role)
    normalized = normalized[:limit]
    return tuple(CommanderRolePlan(role, ROLE_GOALS.get(role, f"处理 {role} 相关工作")) for role in normalized)


def codex_cli_available(binary: str = "codex") -> bool:
    return bool(shutil.which(binary))

class CommanderRuntime:
    def __init__(self, root: str | Path, max_agents: int = 5, coordinator=None):
        self.root=Path(root); self.max_agents=max(1, int(max_agents)); self.agents={}; self._processes={}
        self.coordinator = coordinator
        self.notifications: list[dict] = []
        self.assist_assignments: list[dict] = []
        self._provider_notice_count = 0
        self._child_notice_keys: set[tuple[str, str]] = set()
        self.child_tasks_directory = self.root / "child-tasks"
        self.baselines_directory = self.root / "baselines"
        self.merge_plan_path = self.root / "merge-plan.json"
    def create(self, role: str, task_id: str, *, budget: int = 1,
               model_config: dict | None = None) -> CommanderAgent:
        if len(self.agents) >= self.max_agents: raise ValueError("Commander Agent 数量超过预算")
        config = dict(model_config or {})
        config.setdefault("runtime_root", str(self.root))
        agent=CommanderAgent(f"{task_id}-{role}-{uuid.uuid4().hex[:6]}", role,
                             self.root / task_id / role, budget=budget,
                             model_config=config)
        agent.workspace.mkdir(parents=True, exist_ok=True); self.agents[agent.agent_id]=agent; return agent
    def run(self, agent: CommanderAgent, fn):
        agent.status="RUNNING"
        try:
            agent.result = fn(agent.workspace)
            if agent.status == "RUNNING":
                agent.status = "SUCCESS"
        except Exception as exc:
            agent.result = str(exc)
            if agent.status == "RUNNING":
                agent.status = "FAILED"
        return agent
    def run_parallel(self, agents, fn, timeout: float = 120):
        pool = ThreadPoolExecutor(max_workers=len(agents) or 1)
        futures = [pool.submit(self.run, agent, fn) for agent in agents]
        done, pending = wait(futures, timeout=timeout)
        for future in done:
            future.result()
        for agent, future in zip(agents, futures):
            if future in pending:
                agent.status = "WAITING"
                agent.result = {"error": "timeout"}
        pool.shutdown(wait=False, cancel_futures=True)
        return agents

    def run_processes(self, agents, command_factory, timeout: float = 120):
        """Run one bounded child process per role in isolated workspaces.

        The factory receives an agent and must return an argv list.  This is
        intentionally explicit: real Codex processes are opt-in, while tests
        and offline deployments can use a local Python worker.
        """
        if len(agents) > self.max_agents:
            raise ValueError("Commander Agent 数量超过预算")
        runner = CommanderProcess()
        self._process_runner = runner
        with ThreadPoolExecutor(max_workers=max(1, len(agents))) as pool:
            futures = {
                pool.submit(self._run_process_with_provider, runner, agent, command_factory, timeout): agent
                for agent in agents
            }
            assigned_targets: set[str] = set()
            while futures:
                done, _ = wait(tuple(futures), timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                    futures.pop(future, None)
                self._refresh_child_contexts(agents)
                running_targets = {
                    agent.agent_id for agent in futures.values()
                    if agent.agent_id not in assigned_targets and self._needs_assistance(agent)
                }
                idle = [agent for agent in agents
                        if agent.status == "SUCCESS" and not agent.assist_for
                        and agent.agent_id not in {item.agent_id for item in futures.values()}]
                for target in (agent for agent in agents if agent.agent_id in running_targets):
                    if not idle:
                        break
                    source = idle.pop(0)
                    self._create_assistance_assignment(source, target)
                    assigned_targets.add(target.agent_id)
                    futures[pool.submit(self._run_assistance_with_restore,
                                        runner, source, command_factory, timeout)] = source
        self._auto_assign_assistance(agents, command_factory, timeout, runner)
        return agents

    def _refresh_child_contexts(self, agents) -> None:
        for agent in agents:
            persisted = self._load_child_context(agent)
            if persisted is not None:
                agent.context = persisted

    @staticmethod
    def _needs_assistance(agent) -> bool:
        context = getattr(agent, "context", None)
        return agent.status in {"WAITING", "FAILED"} or bool(
            getattr(context, "need_escalation", False)
        )

    def _auto_assign_assistance(self, agents, command_factory, timeout, runner) -> None:
        """Let a completed role help a difficult role without replacing it.

        Assistance is deliberately a second pass.  The target keeps its own
        task id, status, timer and final authority; the idle source keeps its
        original role and result.  The source's extra output is stored in
        ``assist_result`` and included in Commander summaries.
        """
        idle = [agent for agent in agents
                if agent.status == "SUCCESS" and not agent.assist_for]
        targets = [agent for agent in agents if self._needs_assistance(agent)]
        for target in targets:
            if not idle:
                break
            source = idle.pop(0)
            self._create_assistance_assignment(source, target)
            self._run_assistance_with_restore(runner, source, command_factory, timeout)

    def _create_assistance_assignment(self, source, target) -> dict:
        assignment = {
            "source_agent_id": source.agent_id,
            "source_role": source.role,
            "target_agent_id": target.agent_id,
            "target_role": target.role,
            "target_task_id": getattr(getattr(target, "context", None), "task_id", ""),
            "status": "ASSIGNED",
            "reason": "目标 Agent 仍在处理、超时或已达到升级条件",
        }
        source.assist_for = target.agent_id
        source.assist_history.append(dict(assignment))
        source.model_config["assist_for"] = target.agent_id
        source.model_config["assist_for_role"] = target.role
        self.assist_assignments.append(assignment)
        return assignment

    def _run_assistance_with_restore(self, runner, source, command_factory, timeout):
        assignment = source.assist_history[-1]
        original_status, original_result = source.status, source.result
        source.status = "ASSISTING"
        try:
            self._run_process_with_provider(runner, source, command_factory, timeout)
            source.assist_result = source.result
            assignment["status"] = "COMPLETED" if source.status == "SUCCESS" else "FAILED"
        finally:
            # Assistance is supplemental work and must not replace the source's
            # original role, state, or primary report.
            source.status = original_status
            source.result = original_result
            source.model_config.pop("assist_for", None)
            source.model_config.pop("assist_for_role", None)
        source.assist_history[-1].update(assignment)

    def _run_process_with_provider(self, runner, agent, command_factory, timeout):
        provider_id = agent.model_config.get("provider")
        if self.coordinator is not None and provider_id in self.coordinator.profiles:
            profile = self.coordinator.profiles[provider_id]
            if profile.kind == "api":
                agent.status = "FAILED"
                agent.result = {"error": "COMMANDER_CHILD_API_DENIED", "provider": provider_id}
                return agent
        started = time.monotonic()
        result = runner.run(agent, command_factory(agent), timeout)
        self._collect_provider_notifications()
        self._collect_child_notifications()
        elapsed = time.monotonic() - started
        context = getattr(agent, "context", None)
        # A child Hook records only active tool time and excludes download or
        # build waits. Do not add process duration again when it observed work.
        persisted = self._load_child_context(agent)
        if persisted is not None:
            agent.context = persisted
            context = persisted
        if context is not None and hasattr(context, "record_progress"):
            failed = agent.status == "FAILED"
            if not getattr(context, "last_event_type", None):
                self.record_child_progress(agent, elapsed_seconds=elapsed, failed=failed)
        return agent

    def _collect_provider_notifications(self) -> None:
        if self.coordinator is None:
            return
        entries = self.coordinator.status().get("notifications", [])
        if not isinstance(entries, list):
            return
        for notice in entries[self._provider_notice_count:]:
            self.notifications.append({"type": "PROVIDER_RECOVERY_REQUIRED", **notice,
                                       "continue_work": True})
        self._provider_notice_count = len(entries)

    def _load_child_context(self, agent):
        context = getattr(agent, "context", None)
        task_id = getattr(context, "task_id", "")
        if not task_id:
            return None
        try:
            try:
                from .orchestrator_core import TaskContext
            except ImportError:
                from orchestrator_core import TaskContext
            return TaskContext.load(self.child_tasks_directory / f"{task_id}.json")
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _collect_child_notifications(self) -> None:
        path = self.root / "commander-events.jsonl"
        try:
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError, json.JSONDecodeError):
            return
        for notice in entries:
            if not isinstance(notice, dict) or notice.get("type") != "CHILD_ESCALATION_REQUIRED":
                continue
            key = (str(notice.get("task_id", "")), str(notice.get("event_type", "")))
            if not key[0] or key in self._child_notice_keys:
                continue
            self._child_notice_keys.add(key)
            self.notifications.append(notice)

    def _persist_child_context(self, agent) -> None:
        context = getattr(agent, "context", None)
        if context is not None and hasattr(context, "save"):
            context.save(self.child_tasks_directory)

    def record_child_progress(self, agent, *, elapsed_seconds: float = 0,
                              failed: bool = False, error: str = "") -> object | None:
        """Update one child only and emit an advisory escalation notification.

        The method intentionally does not alter ``agent.status``. A Provider
        outage is information for Commander and the user, not an instruction
        for the child to stop its assigned work.
        """
        context = getattr(agent, "context", None)
        if context is None or not hasattr(context, "record_progress"):
            return context
        was_escalated = bool(getattr(context, "need_escalation", False))
        context.record_progress(elapsed_seconds=elapsed_seconds, failed=failed)
        if context.need_escalation and not was_escalated:
            notice = {
                "type": "CHILD_ESCALATION_REQUIRED",
                "task_id": context.task_id,
                "parent_task_id": agent.model_config.get("task_id"),
                "agent_id": agent.agent_id,
                "role": agent.role,
                "retry_count": context.retry_count,
                "effective_time_seconds": round(context.effective_time_seconds, 3),
                "weight": context.weight,
                "error": str(error)[:500],
                "continue_work": True,
                "message": (f"子 Agent {agent.role} 已达到自己的升级条件，"
                            "请 Commander 告知用户并决定是否求援；子 Agent 继续工作。"),
            }
            self.notifications.append(notice)
            context.agent_execution_plan = {**(context.agent_execution_plan or {}),
                                            "commander_notice": notice,
                                            "continue_work": True}
        self._persist_child_context(agent)
        return context

    def prepare_workspace(self, agent: CommanderAgent, source: str | Path) -> None:
        """Copy the approved source into the agent-owned workspace.

        The child never receives the user's live working directory.  Its edits
        remain in the role workspace until the Commander reviews and merges
        them through the normal confirmation/checkpoint path.
        """
        source_path = Path(source).resolve()
        target = agent.workspace.resolve()
        if source_path == target:
            raise ValueError("Commander 隔离 workspace 不能覆盖源 workspace")
        target.mkdir(parents=True, exist_ok=True)
        ignore_names = {".git", ".agentrelay", "runtime", "node_modules", "__pycache__"}
        ignored = shutil.ignore_patterns(*ignore_names)
        shutil.copytree(source_path, target, dirs_exist_ok=True, ignore=ignored)
        self._save_baseline(agent, source_path)

    @staticmethod
    def _ignored_path(path: Path) -> bool:
        return any(part in {".git", ".agentrelay", "runtime", "node_modules", "__pycache__"}
                   for part in path.parts)

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _snapshot_files(self, root: Path) -> dict[str, str]:
        result = {}
        if not root.is_dir():
            return result
        for path in root.rglob("*"):
            if path.is_file() and not self._ignored_path(path.relative_to(root)):
                try:
                    result[str(path.relative_to(root))] = self._file_digest(path)
                except OSError:
                    continue
        return result

    def _save_baseline(self, agent: CommanderAgent, source: Path) -> None:
        self.baselines_directory.mkdir(parents=True, exist_ok=True)
        path = self.baselines_directory / f"{agent.agent_id}.json"
        path.write_text(json.dumps({"source": str(source), "files": self._snapshot_files(source)},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_baseline(self, agent: CommanderAgent, source: Path) -> dict[str, str]:
        path = self.baselines_directory / f"{agent.agent_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("source") == str(source) and isinstance(value.get("files"), dict):
                return {str(key): str(item) for key, item in value["files"].items()}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return self._snapshot_files(source)

    def build_merge_plan(self, agents, source: str | Path) -> dict:
        """Compute per-child changes against its pre-run baseline."""
        source_path = Path(source).resolve()
        entries = []
        for agent in agents:
            baseline = self._load_baseline(agent, source_path)
            child_files = self._snapshot_files(agent.workspace)
            changed_paths = sorted(set(baseline) | set(child_files))
            for relative in changed_paths:
                before = baseline.get(relative)
                after = child_files.get(relative)
                if before == after:
                    continue
                action = "added" if before is None else ("deleted" if after is None else "modified")
                target = (source_path / relative).resolve()
                child_target = (agent.workspace / relative).resolve()
                if source_path not in target.parents and target != source_path:
                    entries.append({"role": agent.role, "agent_id": agent.agent_id, "path": relative,
                                    "action": action, "error": "MERGE_PATH_ESCAPE"})
                    continue
                current = self._file_digest(target) if target.is_file() else None
                entries.append({"role": agent.role, "agent_id": agent.agent_id, "path": relative,
                                "action": action, "baseline_digest": before,
                                "current_digest": current, "child_digest": after,
                                "child_path": str(child_target), "source_path": str(target),
                                "status": str(agent.status)})
        by_path = {}
        for entry in entries:
            by_path.setdefault(entry["path"], []).append(entry)
        conflicts = []
        for relative, group in by_path.items():
            digests = {(item.get("action"), item.get("child_digest")) for item in group}
            if len(digests) > 1:
                conflicts.append({"path": relative, "reason": "MULTIPLE_CHILD_CONTENTS",
                                  "roles": [item.get("role") for item in group]})
            if any(item.get("current_digest") != item.get("baseline_digest") for item in group):
                conflicts.append({"path": relative, "reason": "SOURCE_CHANGED_DURING_COMMANDER",
                                  "roles": [item.get("role") for item in group]})
            if any(item.get("error") for item in group):
                conflicts.append({"path": relative, "reason": "MERGE_PATH_ESCAPE"})
        summaries = []
        for agent in agents:
            own = [item for item in entries if item.get("agent_id") == agent.agent_id]
            result = agent.result if isinstance(agent.result, dict) else {"report": str(agent.result or "")}
            summaries.append({"agent_id": agent.agent_id, "role": agent.role, "status": agent.status,
                              "changed_files": [item["path"] for item in own],
                              "report": str(result.get("stdout", result.get("report", result)))[:2000],
                              "validation": "通过" if agent.status == "SUCCESS" else "未通过或超时"})
        plan = {"source": str(source_path), "entries": entries, "conflicts": conflicts,
                "agent_summaries": summaries, "mergeable": not conflicts,
                "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
        plan["review"] = {
            "reviewer": "codex-self",
            "status": "approved" if not conflicts else "blocked",
            "findings": (["所有子 Agent 文件改动均通过基线和路径检查"] if not conflicts
                         else [f"{item.get('path')}: {item.get('reason')}" for item in conflicts]),
            "continue_work": True,
        }
        self.merge_plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.merge_plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return plan

    def apply_merge_plan(self, plan: dict, *, confirmed: bool = False) -> dict:
        """Apply a reviewed plan with source-digest preflight checks."""
        if not confirmed:
            raise PermissionError("Commander 合并必须先批准合并确认卡")
        if not plan.get("mergeable") or plan.get("conflicts"):
            return {"status": "merge_conflict", "conflicts": plan.get("conflicts", [])}
        source = Path(plan["source"]).resolve()
        merged, skipped, errors = [], [], []
        for entry in plan.get("entries", []):
            target = (source / entry["path"]).resolve()
            if source not in target.parents and target != source:
                errors.append({"path": entry["path"], "error": "MERGE_PATH_ESCAPE"}); continue
            current = self._file_digest(target) if target.is_file() else None
            if current != entry.get("current_digest"):
                skipped.append({"path": entry["path"], "reason": "SOURCE_CHANGED_AFTER_REVIEW"}); continue
            try:
                if entry["action"] == "deleted":
                    if target.is_file():
                        target.unlink()
                else:
                    child = Path(entry["child_path"]).resolve()
                    if not child.is_file():
                        errors.append({"path": entry["path"], "error": "CHILD_FILE_MISSING"}); continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.agentrelay-merge.tmp")
                    shutil.copy2(child, temporary)
                    os.replace(temporary, target)
                merged.append(entry["path"])
            except OSError as exc:
                errors.append({"path": entry["path"], "error": str(exc)})
        status = "merged" if not errors and not skipped else ("merge_partial" if merged else "merge_failed")
        result = {"status": status, "merged_files": sorted(set(merged)), "skipped": skipped, "errors": errors,
                  "conflicts": plan.get("conflicts", [])}
        plan["merge_result"] = result
        self.merge_plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def summarize(self, agents, plan: dict, merge_result: dict | None = None) -> dict:
        changed = sorted({entry["path"] for entry in plan.get("entries", [])})
        completed = [agent.role for agent in agents if agent.status == "SUCCESS"]
        failed = [agent.role for agent in agents if agent.status != "SUCCESS"]
        return {"status": "completed" if not failed and not plan.get("conflicts") else "needs_review",
                "completed_roles": completed, "incomplete_roles": failed,
                "changed_files": changed, "merge_status": (merge_result or {}).get("status", "pending_review"),
                "conflicts": plan.get("conflicts", []),
                "assistance_assignments": list(self.assist_assignments),
                "message": (f"主审查官已汇总 {len(agents)} 个子 Agent：完成角色 {', '.join(completed) or '无'}；"
                            f"涉及文件 {', '.join(changed) or '无'}。" +
                            ("存在冲突，需要处理后再合并。" if plan.get("conflicts") else "等待合并确认卡批准。"))}

    def cancel(self, agent_id: str) -> bool:
        """Cancel a running child process and isolate the cancellation result."""
        runner = getattr(self, "_process_runner", None)
        process = runner.processes.get(agent_id) if runner else None
        agent = self.agents.get(agent_id)
        if process is None or agent is None or process.poll() is not None:
            return False
        process.kill()
        agent.status = "WAITING"
        agent.result = {"error": "cancelled"}
        return True

    def cancel_and_wait(self, agent_id: str, timeout: float = 5) -> bool:
        if not self.cancel(agent_id):
            return False
        runner = getattr(self, "_process_runner", None)
        process = runner.processes.get(agent_id) if runner else None
        if process is not None:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
        return True

    def save(self, agent: CommanderAgent, directory="runtime/agents") -> Path:
        path=Path(directory); path.mkdir(parents=True, exist_ok=True); target=path / f"agent-{agent.agent_id}.json"
        target.write_text(json.dumps(asdict(agent), ensure_ascii=False, default=str), encoding="utf-8"); return target

    def load(self, agent_id: str, directory="runtime/agents") -> dict:
        return json.loads((Path(directory) / f"agent-{agent_id}.json").read_text(encoding="utf-8"))

    def merge_results(self, agents) -> dict:
        self._collect_child_notifications()
        results = {agent.role: agent.result for agent in agents}
        conflicts = []
        values = [str(value) for value in results.values() if value is not None]
        if len(set(values)) > 1 and len(values) > 1:
            conflicts.append("Agent 输出存在差异，需 Commander 审查")
        return {"results": results, "conflicts": conflicts,
                "notifications": list(self.notifications),
                "all_success": all(agent.status == "SUCCESS" for agent in agents)}

    def resolve_conflicts(self, merged: dict, resolver=None) -> dict:
        if not merged.get("conflicts"): return {**merged, "resolved": True}
        if resolver is None: return {**merged, "resolved": False}
        return {**merged, "resolution": resolver(merged["results"]), "resolved": True}

class CommanderProcess:
    """Run a bounded child process in an agent-owned workspace."""
    def __init__(self):
        self.processes = {}

    def run(self, agent: CommanderAgent, command: list[str], timeout: float = 120) -> dict:
        if not command or any(not isinstance(part, str) for part in command):
            raise ValueError("Commander command 必须是非空字符串数组")
        child_env = os.environ.copy()
        # Commander children never call API Providers. Do not expose API keys
        # to their inherited environment even though the controlled bridge
        # also rejects API provider IDs.
        for name in tuple(child_env):
            if name.endswith("_API_KEY") or name in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}:
                child_env.pop(name, None)
        child_env.pop("AGENTRELAY_API_PROVIDERS_JSON", None)
        child_env["AGENTRELAY_COMMANDER_CHILD"] = "1"
        child_env["AGENTRELAY_COMMANDER_DEPTH"] = "1"
        context = getattr(agent, "context", None)
        if context is not None:
            child_env["AGENTRELAY_COMMANDER_TASK_ID"] = str(getattr(context, "task_id", ""))
            child_env["AGENTRELAY_COMMANDER_RUNTIME_DIR"] = str(agent.model_config.get("runtime_root", ""))
            child_env["AGENTRELAY_COMMANDER_PARENT_TASK_ID"] = str(agent.model_config.get("task_id", ""))
            child_env["AGENTRELAY_COMMANDER_ROLE"] = str(agent.role)
            child_env["AGENTRELAY_COMMANDER_REQUEST"] = str(getattr(context, "user_request", ""))
            child_env["AGENTRELAY_COMMANDER_WORKSPACE"] = str(agent.workspace)
        process = subprocess.Popen(command, cwd=agent.workspace, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=child_env)
        self.processes[agent.agent_id] = process
        agent.process_id = process.pid; agent.status = "RUNNING"
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            agent.status = "SUCCESS" if process.returncode == 0 else "FAILED"
            agent.result = {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr}
        except subprocess.TimeoutExpired:
            process.kill(); stdout, stderr = process.communicate()
            agent.status = "WAITING"; agent.result = {"error": "timeout", "stdout": stdout, "stderr": stderr}
        finally:
            self.processes.pop(agent.agent_id, None)
        return {"agent_id": agent.agent_id, "status": agent.status, "result": agent.result}

    def run_codex(self, agent: CommanderAgent, prompt: str, *, codex="codex", timeout: float = 120) -> dict:
        """Run the currently authenticated Codex CLI in the isolated workspace."""
        model = agent.model_config.get("model")
        command = [codex, "exec"] + (["-m", str(model)] if model else []) + ["--skip-git-repo-check",
                                "--sandbox", "workspace-write", "--approve-for-me",
                                "--ephemeral", "--color", "never", prompt]
        return self.run(agent, command, timeout=timeout)


def service_config(kind: str, runtime_dir: str | Path, command: list[str], *, label="com.agentrelay.daemon") -> str:
    """Return installable launchd/systemd text without modifying the host."""
    runtime = str(Path(runtime_dir).resolve()); cmd = " ".join(command)
    if kind == "launchd":
        return f'<plist version="1.0"><dict><key>Label</key><string>{label}</string><key>ProgramArguments</key><array>' + "".join(f"<string>{x}</string>" for x in command) + f'</array><key>WorkingDirectory</key><string>{runtime}</string><key>RunAtLoad</key><true/></dict></plist>'
    if kind == "systemd":
        return f"[Unit]\nDescription=AgentRelay daemon\n[Service]\nExecStart={cmd}\nWorkingDirectory={runtime}\nRestart=on-failure\n[Install]\nWantedBy=default.target\n"
    raise ValueError("service kind must be launchd or systemd")
