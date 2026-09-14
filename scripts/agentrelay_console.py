#!/usr/bin/env python3
"""Human-facing local console for AgentRelay cards and model routing.

The console does not start a web server and never contacts a model while
rendering status.  It is intentionally a review surface: execution remains a
separate, explicit confirmation action.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys

try:
    from .model_router import ModelRouter, ProviderRouteMemory
    from .orchestrator_core import (CheckpointStore, ConfirmationCard,
                                    ConfirmationStore, Orchestrator,
                                    TaskContext, WorkflowEngine)
    from .orchestrator_store import MemoryStore
    from .task_scheduler import TaskAnalyzer
except ImportError:
    from model_router import ModelRouter, ProviderRouteMemory
    from orchestrator_core import (CheckpointStore, ConfirmationCard,
                                   ConfirmationStore, Orchestrator,
                                   TaskContext, WorkflowEngine)
    from orchestrator_store import MemoryStore
    from task_scheduler import TaskAnalyzer


def _root(value: str | None) -> Path:
    return Path(value or os.environ.get("AGENTRELAY_HOME", ".agentrelay")).resolve()


def _context_path(root: Path, task_id: str) -> Path:
    return root / "runtime" / "tasks" / f"{task_id}.json"


def _store(root: Path) -> ConfirmationStore:
    return ConfirmationStore(root / "cards")


def _line(title: str, value) -> str:
    if isinstance(value, (list, tuple)):
        value = "、".join(str(item) for item in value) or "无"
    return f"{title}: {value or '无'}"


def render_requirement_card(card: dict) -> str:
    lines = ["需求卡", f"任务: {card.get('task_id', '')}",
             _line("用户需求", card.get("user_requirement") or card.get("original_request")),
             _line("需求评估", card.get("requirement_assessment")),
             _line("大白话", card.get("plain_explanation")),
             _line("影响模块", card.get("affected_modules")),
             _line("影响文件", card.get("affected_files") or card.get("expected_files")),
             _line("当前功能", card.get("current_state")),
             _line("目标功能", card.get("target_state")),
             _line("风险", card.get("risk_analysis") or card.get("risks")),
             _line("验证", card.get("verification_plan")),
             _line("回滚", card.get("rollback_plan"))]
    return "\n".join(lines)


def render_confirmation_card(card: ConfirmationCard | dict) -> str:
    value = asdict(card) if isinstance(card, ConfirmationCard) else dict(card)
    lines = ["确认卡", f"任务: {value.get('task_id', '')}",
             _line("状态", value.get("status")),
             _line("当前确认", {"confirm_requirement": "确认需求理解", "enable_commander": "启用审查官模式", "execute": "执行确认的修改"}.get(value.get("action"), value.get("action"))),
             _line("授权文件", value.get("allowed_files") or value.get("scope")),
             _line("验证命令", value.get("verification_commands")),
             _line("验收条件", value.get("acceptance_criteria")),
             _line("工作区", value.get("workspace")),
             _line("修改尝试", value.get("attempts")),
             _line("最近错误", value.get("last_error")),
             _line("恢复原因", value.get("recovery_reason"))]
    return "\n".join(lines)


def model_status(root: Path) -> list[dict]:
    registry_path = Path(__file__).with_name("model_registry.json")
    try:
        configured = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        configured = {"models": []}
    route_records = ProviderRouteMemory(root / "runtime" / "provider_routes.json").status()
    successful = {record.get("provider") for record in route_records if record.get("success_count", 0)}
    codex_ready = bool(shutil.which("codex"))
    rows = [
        {"name": "codex-self", "kind": "agent", "registered": True, "configured": codex_ready,
         "ready": codex_ready, "purpose": "主 Agent 审查、确认与安全执行"},
        {"name": "codex-subagent", "kind": "agent", "registered": True, "configured": codex_ready,
         "ready": codex_ready, "purpose": "受控子任务与离线回退"},
    ]
    for item in configured.get("models", []):
        name = str(item.get("name", ""))
        if name in {"codex-self", "codex-subagent"}:
            continue
        env_key = "AGENTRELAY_" + "".join(ch if ch.isalnum() else "_" for ch in name).upper()
        # Keep the original DeepSeek environment names working while custom
        # web/API providers use their own stable provider-id prefix.
        if name == "deepseek-web":
            env_key = "AGENTRELAY_DEEPSEEK"
        if item.get("kind") == "web":
            enabled = os.environ.get(f"{env_key}_ENABLED", "0").lower() in {"1", "true", "yes"}
            mode = os.environ.get(f"{env_key}_MODE", "playwright")
            state = Path(os.environ.get(
                f"{env_key}_LOGIN_STATE",
                str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) /
                    "skills" / "agent-relay" / "agent_relay_login_state.json"),
            ))
            if name == "deepseek-web" and os.environ.get("AGENT_RELAY_LOGIN_STATE"):
                state = Path(os.environ["AGENT_RELAY_LOGIN_STATE"])
            ready = enabled and (bool(os.environ.get(f"{env_key}_ENDPOINT"))
                                 if mode == "api" else state.is_file())
            configured_now = ready
            purpose = "联网研究与只读专家建议"
        elif item.get("kind") == "local":
            ready = bool(os.environ.get(f"{env_key}_ENDPOINT") or os.environ.get("AGENTRELAY_LOCAL_LLM_ENDPOINT"))
            configured_now = ready
            purpose = "简单样式、格式化和低风险修改"
        elif item.get("kind") == "api":
            ready = bool(os.environ.get(f"{env_key}_ENDPOINT") and os.environ.get(f"{env_key}_API_KEY"))
            if name == "gpt-api":
                ready = ready or bool(os.environ.get("AGENTRELAY_API_ENDPOINT") and os.environ.get("AGENTRELAY_API_KEY"))
            configured_now = ready
            purpose = "API 分析与受限修改计划"
        else:
            ready = configured_now = False
            purpose = "可选 Provider"
        rows.append({"name": name, "kind": item.get("kind"), "registered": bool(item.get("enabled", False)),
                     "configured": configured_now, "ready": ready, "purpose": purpose,
                     "capabilities": item.get("capabilities", []),
                     "verified_route": bool({name, "deepseek" if name == "deepseek-web" else name} & successful)})
    return rows


def render_models(rows: list[dict], route=None) -> str:
    lines = ["模型状态"]
    for row in rows:
        state = "可用" if row["ready"] else ("已注册，待配置" if row["registered"] else "未启用")
        verified = "，有已验证相似问题记录" if row.get("verified_route") else ""
        lines.append(f"- {row['name']} [{row.get('kind', '')}] {state}{verified}: {row['purpose']}")
    if route:
        lines.append(f"路由预览: {route.provider}，{route.reason}")
    return "\n".join(lines)


def _engine(root: Path) -> WorkflowEngine:
    store = _store(root)
    orchestrator = Orchestrator(
        memory=MemoryStore(root / "memory.json"),
        checkpoints=CheckpointStore(root / "checkpoints"),
        confirmations=store,
    )
    return WorkflowEngine.from_environment(orchestrator=orchestrator, runtime_dir=root / "runtime")


def _emit(value, as_json: bool) -> int:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, default=str, indent=2))
    else:
        print(value)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AgentRelay local review console")
    parser.add_argument("--root", help="state directory; default .agentrelay")
    parser.add_argument("--json", action="store_true", dest="as_json")
    sub = parser.add_subparsers(dest="command", required=True)
    models = sub.add_parser("models", help="show registered/configured model status")
    models.add_argument("--request", help="also preview routing for this request")
    task = sub.add_parser("task", help="create and review confirmation cards")
    actions = task.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="submit a task; complex work waits for confirmation")
    create.add_argument("task_id")
    create.add_argument("request")
    create.add_argument("--workspace")
    create.add_argument("--module", action="append", default=[])
    create.add_argument("--file", action="append", default=[])
    create.add_argument("--verify", action="append", default=[])
    create.add_argument("--project", default="default")
    create.add_argument("--session")
    actions.add_parser("list")
    for name in ("show", "approve", "reject", "cancel", "execute"):
        action = actions.add_parser(name)
        action.add_argument("task_id")
    commander = sub.add_parser("commander", help="run dynamic 1-5 role Commander")
    commander.add_argument("task_id")
    commander.add_argument("request")
    commander.add_argument("--workspace", required=True)
    commander.add_argument("--module", action="append", default=[])
    commander.add_argument("--role", action="append", default=[])
    commander.add_argument("--role-model", action="append", default=[], metavar="ROLE=MODEL")
    commander.add_argument("--max-agents", type=int, default=5)
    commander.add_argument("--timeout", type=float, default=600)
    commander.add_argument("--offline-plan", action="store_true")
    commander_merge = sub.add_parser("commander-merge", help="approve and merge reviewed Commander results")
    commander_merge.add_argument("task_id")
    commander_merge.add_argument("--workspace", required=True)
    commander_merge.add_argument("--runtime-root", required=True)
    args = parser.parse_args(argv)
    root = _root(args.root)
    if args.command == "commander-merge":
        store = _store(root)
        try:
            merge_id = f"{args.task_id}:merge"
            store.transition(merge_id, "approved")
            result = Orchestrator(confirmations=store).merge_commander_results(
                args.task_id, args.workspace, args.runtime_root, confirmed=True)
        except (KeyError, ValueError, PermissionError, OSError) as exc:
            return _emit({"error": str(exc)}, True) if args.as_json else _emit(str(exc), False)
        if args.as_json:
            return _emit(result, True)
        return _emit(result.get("summary", result), False)
    if args.command == "commander":
        if os.environ.get("AGENTRELAY_COMMANDER_CHILD") == "1":
            message = "COMMANDER_RECURSION_DENIED: 子 Agent 不能再次启动 Commander"
            if args.as_json:
                print(json.dumps({"error": message}, ensure_ascii=False))
            else:
                print(message, file=sys.stderr)
            return 1
        engine = _engine(root)
        try:
            result = engine.orchestrator.run_commander_auto(
                args.task_id, args.request, args.workspace, modules=tuple(args.module),
                roles=tuple(args.role) or None, max_agents=args.max_agents,
                timeout=args.timeout, runtime_root=root / "commander",
                native=not args.offline_plan,
                model_configs={item.split("=", 1)[0]: {"model": item.split("=", 1)[1]}
                               for item in args.role_model if "=" in item})
        except (PermissionError, ValueError) as exc:
            if args.as_json:
                print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            else:
                print(str(exc), file=sys.stderr)
            return 1
        if args.as_json:
            value = {key: value for key, value in result.items() if key not in {"agents", "reports"}}
            if "reports" in result:
                value["reports"] = [asdict(item) for item in result["reports"]]
            if "agents" in result:
                value["agents"] = [{"agent_id": item.agent_id, "role": item.role,
                                    "status": item.status, "workspace": str(item.workspace),
                                    "result": item.result} for item in result["agents"]]
            return _emit(value, True)
        lines = [f"Commander 模式: {result.get('mode')}"]
        for role in result.get("roles", ()):
            lines.append(f"- {role.get('role')}: {role.get('goal')}")
        for agent in result.get("agents", ()):
            lines.append(f"- {agent.role}: {agent.status} ({agent.workspace})")
        for report in result.get("reports", ()):
            lines.append(f"- {report.role}: {report.status} ({report.completed_work})")
        if result.get("merged"):
            merged = result["merged"]
            lines.append(f"汇总: {merged}")
            for notice in merged.get("notifications", ()):
                message = notice.get("message") or notice.get("reason") or str(notice)
                suffix = "（子 Agent 继续工作）" if notice.get("continue_work") else ""
                lines.append(f"- 通知: {message}{suffix}")
            if merged.get("summary", {}).get("message"):
                lines.append(f"主审查官总结: {merged['summary']['message']}")
        return _emit("\n".join(lines), False)
    if args.command == "models":
        rows = model_status(root)
        route = None
        if args.request:
            plan = TaskAnalyzer().analyze(args.request)
            providers = {"local": object()}
            for row in rows:
                if not row["ready"]:
                    continue
                aliases = {"local-9b": "local-llm", "deepseek-web": "deepseek"}
                providers[aliases.get(row["name"], row["name"])] = {"provider": object(), "kind": row.get("kind")}
            route = ModelRouter(providers, route_memory=ProviderRouteMemory(root / "runtime" / "provider_routes.json")).choose(
                plan.task_type, plan.difficulty, needs_web=plan.task_type == "TECHNICAL_RESEARCH", request=args.request)
        return _emit({"models": rows, "route": asdict(route) if route else None}, True) if args.as_json else _emit(render_models(rows, route), False)

    store = _store(root)
    if args.action == "create":
        workspace = str(Path(args.workspace).resolve()) if args.workspace else None
        result = _engine(root).submit(args.task_id, args.request, workspace=workspace,
                                      modules=tuple(args.module), allowed_files=tuple(args.file),
                                      verification_commands=tuple(args.verify), project=args.project,
                                      session_id=args.session)
        if "confirmation" not in result:
            value = {"task_id": args.task_id, "status": "completed", "task_type": result["task_type"],
                     "provider": result["provider_response"]["provider"]}
            return _emit(value, args.as_json)
        payload = {"requirement_card": asdict(result["card"]) if "card" in result else None,
                   "confirmation_card": asdict(result["confirmation"]),
                   "provider": result["provider_response"]["provider"]}
        if args.as_json:
            return _emit(payload, True)
        requirement = (render_requirement_card(payload["requirement_card"]) + "\n\n"
                       if payload["requirement_card"] else "")
        return _emit(requirement + render_confirmation_card(payload["confirmation_card"]), False)
    if args.action == "list":
        cards = [asdict(card) for card in store.cards.values()]
        return _emit(cards, True) if args.as_json else _emit("\n".join(
            f"{card['task_id']}\t{card['status']}\t{card['request'][:80]}" for card in cards) or "无任务", False)
    card = store.cards.get(args.task_id)
    if card is None:
        print(f"Task {args.task_id} not found.", file=sys.stderr)
        return 1
    if args.action == "show":
        context_path = _context_path(root, args.task_id)
        requirement = None
        try:
            requirement = TaskContext.load(context_path).requirement_card
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if args.as_json:
            return _emit({"requirement_card": requirement, "confirmation_card": asdict(card)}, True)
        text = (render_requirement_card(requirement) + "\n\n") if requirement else ""
        return _emit(text + render_confirmation_card(card), False)
    if args.action in {"approve", "reject", "cancel"}:
        target = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[args.action]
        updated = store.advance_requirement(args.task_id) if (args.action == "approve" and card.action == "confirm_requirement") else store.transition(args.task_id, target)
        return _emit(asdict(updated), args.as_json) if args.as_json else _emit(render_confirmation_card(updated), False)
    if card.action != "execute":
        print("当前确认卡不是执行确认；请先完成需求确认或启用审查官模式确认。", file=sys.stderr)
        return 1
    result = Orchestrator(confirmations=store, checkpoints=CheckpointStore(root / "checkpoints")).execute_confirmed_task(args.task_id)
    return _emit(result, args.as_json) if args.as_json else _emit(f"Task {args.task_id}: {result['status']}", False)


if __name__ == "__main__":
    raise SystemExit(main())
