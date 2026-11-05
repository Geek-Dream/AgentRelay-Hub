"""User-adjustable approval tolerance and one-visible-card policy.

Modes change task confirmation only. Dangerous tools, shared impacts and an
approved change contract always retain their own hard gates.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

MODES = {"strict", "standard", "relaxed"}
MENU_MODES = {"1": "strict", "2": "standard", "3": "relaxed"}
DESCRIPTIONS = {
    "strict": "所有写操作先确认；完整交付审计：逐项需求、真实 diff、测试/行为、页面视觉、主审和独立审查。",
    "standard": "默认轻量检查；单点颜色/圆角/按钮方圆等样式只验目标与边界，复杂/共享/高风险自动升级。",
    "relaxed": "小任务不做正式审计；仅大型、多文件、多目标、共享或高风险任务审计，安全底线不放宽。",
}
COMMAND_PATTERN = re.compile(r"^/(?:agent-approval|设置)(?:\s+(\S+))?\s*$", re.I)
SELECTION_PATTERN = re.compile(r"^[1-3](?:\s*[、.，,]\s*(?:严格|标准|宽松|strict|standard|relaxed))?\s*$", re.I)
CANCEL_PATTERN = re.compile(r"^(?:0|q|取消|退出|cancel|exit)$", re.I)
DEFAULT_MODE = "standard"
LABELS = {"strict": "严格：所有写操作先确认", "standard": "标准：清晰小改免确认，复杂/共享影响先确认",
          "relaxed": "宽松：仅危险命令、大工程和越界先确认"}
LARGE = re.compile(r"大型任务|大工程|完整实现|从零实现|整体重构|全面重构|系统性改造|整个项目|全项目|跨模块|全链路|端到端|前后端|数据库迁移|部署上线|升级依赖", re.I)
WRITE = re.compile(r"修改|新增|删除|重构|迁移|升级|替换|改造|修复|接入|集成|实现|搭建|部署|发布|安装|配置|调整|implement|add|remove|delete|refactor|migrate|upgrade|replace|fix|integrate|build|deploy|install", re.I)
MULTI = re.compile(r"并且|同时|另外|然后|接着|还要|还需要|以及|最后|(?:^|\n)\s*(?:\d+[.)、]|[-*])\s+", re.I)
SHARED = re.compile(r"公共组件|共享组件|全局样式|整个项目全部|所有模块|修改全部|全站", re.I)
AMBIGUOUS = re.compile(r"自由发挥|看着办|随便改|更好看一些|风格参考|参考当前风格", re.I)


def policy_path(codex_home: Path) -> Path:
    return codex_home / "agent_relay_tracker" / "approval-policy.json"


def load_policy(codex_home: Path) -> dict[str, str]:
    try:
        data = json.loads(policy_path(codex_home).read_text(encoding="utf-8"))
        mode = data.get("mode")
        if mode in MODES:
            return {"version": "1", "mode": mode}
    except (OSError, ValueError, TypeError):
        pass
    return {"version": "1", "mode": DEFAULT_MODE}


def save_policy(codex_home: Path, mode: str) -> dict[str, str]:
    if mode not in MODES:
        raise ValueError(f"权限模式必须是 {', '.join(sorted(MODES))}")
    path = policy_path(codex_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"version": 1, "mode": mode}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return {"version": "1", "mode": mode}


def should_confirm(prompt: str, mode: str = DEFAULT_MODE, file_count: int = 0,
                   domain_count: int = 0, contract: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Evaluate only the task gate, never the dangerous/shared hard gates."""
    text = str(prompt or "").strip()
    if not text:
        return False, []
    if contract and not contract.get("approved"):
        return True, ["已提出严格变更约束，需要确认目标与边界"]
    write = bool(WRITE.search(text))
    if not write:
        return False, []
    if LARGE.search(text):
        return True, ["大工程、跨模块或高风险修改"]
    if SHARED.search(text):
        return True, ["涉及公共组件、共享实现或全局影响"]
    if mode == "strict":
        return True, ["严格模式：新增、修改、删除都需要确认"]
    if mode == "relaxed":
        return False, []
    if file_count >= 2:
        return True, ["涉及多个代码文件"]
    if domain_count >= 2:
        return True, ["同时涉及多个技术领域"]
    if MULTI.search(text):
        return True, ["包含多个连续修改目标"]
    if AMBIGUOUS.search(text):
        return True, ["目标或参考不清楚，需要先确认"]
    return False, []


def apply_selection(codex_home: Path, selection: str) -> dict[str, str]:
    value = str(selection).strip().lower()
    if value in MENU_MODES:
        mode = MENU_MODES[value]
    elif value in MODES:
        mode = value
    else:
        raise ValueError("请选择 1、2、3，或 strict、standard、relaxed")
    save_policy(codex_home, mode)
    saved = load_policy(codex_home)
    if saved["mode"] != mode:
        raise OSError("权限模式保存后回读不一致")
    return saved


def render_menu(codex_home: Path) -> str:
    current = load_policy(codex_home)["mode"]
    divider = "─" * 24
    lines = [divider, "「AgentRelay · 权限设置」", divider, "",
             f"当前模式: {current} · {LABELS[current]}", "",
             "01 · 选择权限容忍度"]
    for number, mode in MENU_MODES.items():
        marker = "（当前）" if mode == current else ("（默认）" if mode == DEFAULT_MODE else "")
        lines.extend([f"[{number}] {LABELS[mode]} · {mode} {marker}", f"    {DESCRIPTIONS[mode]}", ""])
    lines.extend([
        "02 · 交付审计绑定",
        "strict: 完整交付审计，缺需求/行为/视觉/主审/独立审查证据不能完成。",
        "standard: 轻量审计，单点样式只验目标/边界；复杂/共享/高风险自动升级，不让小改走多余流程。",
        "relaxed: 小任务不做正式审计；大型/多文件/多目标/共享/高风险才审计，安全底线保留。",
        "", "03 · 始终保留的安全边界",
        "危险命令、共享影响、元素级约束和未批准的扩大范围不会被宽松模式豁免。",
        "切换权限模式不会批准当前待确认任务。",
        "", divider, "请输入 1、2、3；输入 0 取消。", divider,
    ])
    return "\n".join(lines)


def saved_message(codex_home: Path, mode: str) -> str:
    return (f"「AgentRelay · 权限设置已保存」\n当前模式: {mode} · {LABELS[mode]}\n"
            f"{DESCRIPTIONS[mode]}\n配置: {policy_path(codex_home)}\n"
            "已回读验证；不影响当前待确认任务，也不会放宽危险命令和越界门禁。")


def command_request(prompt: str, codex_home: Path, menu_pending: bool = False) -> dict[str, Any] | None:
    """Parse the portable slash menu. None means this is an ordinary prompt."""
    text = str(prompt or "").strip()
    match = COMMAND_PATTERN.match(text)
    if match:
        argument = match.group(1)
        if argument is None:
            return {"action": "menu", "message": render_menu(codex_home)}
        if CANCEL_PATTERN.match(argument):
            return {"action": "cancel", "message": "已退出权限设置，未修改配置。"}
        try:
            saved = apply_selection(codex_home, argument)
        except ValueError:
            return {"action": "invalid", "message": "无效选项。请输入 1、2、3，或 strict、standard、relaxed。"}
        return {"action": "saved", "message": saved_message(codex_home, saved["mode"])}
    if not menu_pending:
        return None
    if CANCEL_PATTERN.match(text):
        return {"action": "cancel", "message": "已退出权限设置，未修改配置。"}
    if not SELECTION_PATTERN.match(text):
        return None
    number = text[0]
    saved = apply_selection(codex_home, number)
    return {"action": "saved", "message": saved_message(codex_home, saved["mode"])}


def mark_card_shown(gate: dict[str, Any], request_hash: str) -> bool:
    """Return True only the first time this request's visible card is emitted."""
    if not request_hash or gate.get("card_shown_sha256") == request_hash:
        return False
    gate["card_shown_sha256"] = request_hash
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", help="show、set、菜单，或直接输入 1/2/3")
    parser.add_argument("mode", nargs="?", help="1/2/3 或 strict/standard/relaxed")
    parser.add_argument("--codex-home", type=Path)
    args = parser.parse_args()
    home = args.codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    if args.command is None:
        print(render_menu(home))
        if not sys.stdin.isatty():
            return 0
        while True:
            try:
                selection = input("请选择 1、2、3（0 取消）：").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消，未修改配置。")
                return 0
            if CANCEL_PATTERN.match(selection):
                print("已取消，未修改配置。")
                return 0
            try:
                saved = apply_selection(home, selection)
                print(saved_message(home, saved["mode"]))
                return 0
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
    if args.command == "show":
        if args.mode:
            parser.error("show 不接受模式参数")
        policy = load_policy(home)
    else:
        selection = args.mode if args.command == "set" else args.command
        if not selection or (args.command != "set" and args.mode):
            parser.error("用法：show、set <1/2/3/模式>，或直接输入 1/2/3")
        try:
            policy = apply_selection(home, selection)
        except ValueError as exc:
            parser.error(str(exc))
    print(json.dumps({**policy, "label": LABELS[policy["mode"]], "path": str(policy_path(home))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
