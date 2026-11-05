#!/usr/bin/env python3
"""Human-facing local console for AgentRelay cards and model routing.

The console does not start a web server and never contacts a model while
rendering status.  It is intentionally a review surface: execution remains a
separate, explicit confirmation action.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
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


CARD_STYLE_CHOICES = ("default", "codex", "auto")
ANSI_RED_BOLD = "\033[1;31m"
ANSI_BLUE_BOLD = "\033[1;34m"
ANSI_CYAN_BOLD = "\033[1;36m"
ANSI_GREEN = "\033[32m"
ANSI_RESET = "\033[0m"
CODEX_DIVIDER = "────────────────────────"
_CJK_TEXT = re.compile(r"[\u3400-\u9fff]")


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


def _aligned_lines(fields, *, colored: bool = False, risk: bool = False) -> list[str]:
    """Align values within one section; long labels don't widen other sections."""
    visible = [(str(label), field_value) for label, field_value in fields
               if field_value not in (None, "", (), []) or label in {"最近错误", "恢复原因"}]
    width = max((len(label) for label, _ in visible), default=0)
    lines = []
    for label, value in visible:
        values = ["、".join(str(item) for item in value)] if isinstance(value, (list, tuple)) else _display_values(value)
        prefix = f"{label + ':':<{width + 1}} "
        content = _paint(values[0], ANSI_RED_BOLD) if risk else _paint_technical(values[0])
        lines.append(_paint(f"{label + ':':<{width + 1}}", ANSI_BLUE_BOLD) + " " + content if colored
                     else prefix + values[0])
        continuation = " " * (width + 2)
        lines.extend(continuation + (_paint_technical(item) if colored else item) for item in values[1:])
    return lines


def _present_line(title: str, value) -> str | None:
    if value in (None, "", (), [], 0):
        return None
    return _line(title, value)


def resolve_card_style(
    style: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve card style while keeping the existing plain output as default."""
    env = os.environ if environ is None else environ
    requested = str(style or env.get("AGENTRELAY_CARD_STYLE", "default")).strip().lower()
    if requested == "auto":
        target = str(env.get("AGENTRELAY_TARGET", "")).strip().lower()
        is_codex = target == "codex" or bool(
            env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
        )
        return "codex" if is_codex else "default"
    return requested if requested in {"default", "codex"} else "default"


def _paint(value, color: str) -> str:
    text = str(value)
    return f"{color}{text}{ANSI_RESET}" if text else text


def _display_values(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)] or ["无"]
    return [str(value)] if value not in (None, "") else ["无"]


def _paint_technical(value) -> str:
    text = str(value)
    if text == "无" or _CJK_TEXT.search(text):
        return text
    return _paint(text, ANSI_GREEN)


def _codex_field(label: str, value, *, risk: bool = False) -> list[str]:
    label_color = ANSI_RED_BOLD if risk else ANSI_BLUE_BOLD
    lines = [_paint(label, label_color)]
    for item in _display_values(value):
        if risk and item != "无":
            lines.append(_paint(item, ANSI_RED_BOLD))
        else:
            lines.append(_paint_technical(item))
    return lines


def _confirmation_action_label(action: str | None) -> str:
    labels = {
        "confirm_requirement": "确认需求理解",
        "enable_commander": "启用审查官模式",
        "execute": "执行确认的修改",
    }
    return labels.get(action, action or "无")


def _confirmation_choices(action: str | None) -> tuple[str, str, str]:
    if action == "confirm_requirement":
        return ("确认需求", "调整方案", "取消任务")
    if action == "enable_commander":
        return ("启用审查", "调整方案", "取消任务")
    return ("批准执行", "调整方案", "取消任务")


def _render_codex_requirement_card(card: dict) -> str:
    lines = [
        CODEX_DIVIDER,
        _paint("「AgentRelay · 需求卡」", ANSI_RED_BOLD),
        CODEX_DIVIDER,
    ]
    fields = (
        ("任务", card.get("task_id")),
        ("用户需求", card.get("user_requirement") or card.get("original_request")),
        ("需求评估", card.get("requirement_assessment")),
        ("大白话", card.get("plain_explanation")),
        ("影响模块", card.get("affected_modules")),
        ("影响文件", card.get("affected_files") or card.get("expected_files")),
        ("当前功能", card.get("current_state")),
        ("目标功能", card.get("target_state")),
    )
    lines.extend(_aligned_lines(fields, colored=True))
    lines.extend(_aligned_lines([("风险", card.get("risk_analysis") or card.get("risks"))],
                                colored=True, risk=True))
    lines.extend(_aligned_lines([
        ("验证", card.get("verification_plan")),
        ("回滚", card.get("rollback_plan")),
    ], colored=True))
    lines.append(CODEX_DIVIDER)
    return "\n".join(lines)


def _contract_fields(value: dict) -> list[tuple[str, str]]:
    contract = value.get("change_contract")
    if not isinstance(contract, dict):
        return []
    try:
        from .change_contract import card_fields
    except ImportError:
        from change_contract import card_fields
    return card_fields(contract)


def _confirmation_summary(value: dict) -> str:
    action = value.get("action")
    if action == "confirm_requirement":
        return "先确认需求、目标与边界；此步不授权修改代码。"
    if action == "enable_commander":
        return "确认在下列边界内启用 Commander；子 Agent 继承同一约束。"
    contract = value.get("change_contract")
    if isinstance(contract, dict) and contract.get("target_scope"):
        return f"只修改「{contract['target_scope']}」，不得影响其他元素或模块。"
    return "仅批准下列明确范围内的修改；新增文件、数据或业务范围需重新确认。"


def _render_codex_confirmation_card(value: dict) -> str:
    action = value.get("action")
    lines = [
        CODEX_DIVIDER,
        _paint("「AgentRelay · 执行确认卡」", ANSI_RED_BOLD),
        CODEX_DIVIDER,
        _confirmation_summary(value),
        "",
        _paint("01 · 当前决定", ANSI_CYAN_BOLD),
    ]
    fields = (
        ("任务", value.get("task_id")),
        ("状态", value.get("status")),
        ("当前确认", _confirmation_action_label(action)),
        ("授权文件", value.get("allowed_files") or value.get("scope")),
    )
    lines.extend(_aligned_lines(fields, colored=True))
    contract_fields = _contract_fields(value)
    if contract_fields:
        lines.append(_paint("02 · 目标与边界（必须逐项遵守）", ANSI_CYAN_BOLD))
        lines.extend(_aligned_lines(contract_fields, colored=True))
    lines.append(_paint("03 · 验收与执行", ANSI_CYAN_BOLD))
    execution_fields = [
        ("验证命令", value.get("verification_commands")),
        ("验收条件", value.get("acceptance_criteria")),
        ("工作区", value.get("workspace")),
        ("修改尝试", value.get("attempts")),
        ("最近错误", value.get("last_error")),
        ("恢复原因", value.get("recovery_reason")),
    ]
    lines.extend(_aligned_lines(execution_fields, colored=True))
    if value.get("status") == "pending":
        first, second, third = _confirmation_choices(action)
        lines.extend([
            "",
            CODEX_DIVIDER,
            _paint(f"[1] {first}", ANSI_GREEN),
            _paint(f"[2] {second}", ANSI_BLUE_BOLD),
            _paint(f"[3] {third}", ANSI_RED_BOLD),
        ])
    lines.append(CODEX_DIVIDER)
    return "\n".join(lines)


def render_requirement_card(card: dict, *, style: str | None = None) -> str:
    if resolve_card_style(style) == "codex":
        return _render_codex_requirement_card(card)
    lines = [CODEX_DIVIDER, "「AgentRelay · 需求卡」", CODEX_DIVIDER, ""]
    lines.extend(_aligned_lines([
        ("任务", card.get("task_id", "")),
        ("用户需求", card.get("user_requirement") or card.get("original_request")),
        ("需求评估", card.get("requirement_assessment")),
        ("大白话", card.get("plain_explanation")),
        ("影响模块", card.get("affected_modules")),
        ("影响文件", card.get("affected_files") or card.get("expected_files")),
        ("当前功能", card.get("current_state")),
        ("目标功能", card.get("target_state")),
        ("风险", card.get("risk_analysis") or card.get("risks")),
        ("验证", card.get("verification_plan")),
        ("回滚", card.get("rollback_plan")),
    ]))
    lines.append(CODEX_DIVIDER)
    return "\n".join(lines)


def render_confirmation_card(
    card: ConfirmationCard | dict,
    *,
    style: str | None = None,
) -> str:
    value = asdict(card) if isinstance(card, ConfirmationCard) else dict(card)
    if resolve_card_style(style) == "codex":
        return _render_codex_confirmation_card(value)
    lines = [CODEX_DIVIDER, "「AgentRelay · 执行确认卡」", CODEX_DIVIDER, _confirmation_summary(value), "",
             "01 · 当前决定"]
    lines.extend(_aligned_lines([
        ("任务", value.get("task_id", "")),
        ("状态", value.get("status")),
        ("当前确认", _confirmation_action_label(value.get("action"))),
        ("授权文件", value.get("allowed_files") or value.get("scope")),
    ]))
    contract_fields = _contract_fields(value)
    if contract_fields:
        lines.extend(["", "02 · 目标与边界（必须逐项遵守）"])
        lines.extend(_aligned_lines(contract_fields))
    lines.extend(["", "03 · 验收与执行"])
    lines.extend(_aligned_lines([
        ("验证命令", value.get("verification_commands")),
        ("验收条件", value.get("acceptance_criteria")),
        ("工作区", value.get("workspace")),
        ("修改尝试", value.get("attempts")),
        ("最近错误", value.get("last_error")),
        ("恢复原因", value.get("recovery_reason")),
    ]))
    lines.append(CODEX_DIVIDER)
    return "\n".join(lines)


def _add_card_style_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--card-style",
        choices=CARD_STYLE_CHOICES,
        default=None,
        help="卡片样式：default 保持原样，codex 使用彩色详细卡片，auto 按目标 Agent 判断",
    )


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
    _add_card_style_argument(create)
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
        _add_card_style_argument(action)
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
        requirement = (render_requirement_card(
            payload["requirement_card"], style=args.card_style) + "\n\n"
                       if payload["requirement_card"] else "")
        return _emit(
            requirement + render_confirmation_card(
                payload["confirmation_card"], style=args.card_style
            ),
            False,
        )
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
        text = (
            render_requirement_card(requirement, style=args.card_style) + "\n\n"
            if requirement else ""
        )
        return _emit(
            text + render_confirmation_card(card, style=args.card_style),
            False,
        )
    if args.action in {"approve", "reject", "cancel"}:
        target = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[args.action]
        updated = store.advance_requirement(args.task_id) if (args.action == "approve" and card.action == "confirm_requirement") else store.transition(args.task_id, target)
        return _emit(asdict(updated), args.as_json) if args.as_json else _emit(
            render_confirmation_card(updated, style=args.card_style), False
        )
    if card.action != "execute":
        print("当前确认卡不是执行确认；请先完成需求确认或启用审查官模式确认。", file=sys.stderr)
        return 1
    result = Orchestrator(confirmations=store, checkpoints=CheckpointStore(root / "checkpoints")).execute_confirmed_task(args.task_id)
    return _emit(result, args.as_json) if args.as_json else _emit(f"Task {args.task_id}: {result['status']}", False)


if __name__ == "__main__":
    raise SystemExit(main())
