"""Shared strict-change contract for main agents and delegated workers.

A contract is explicit: references and file scope are never guessed from a vague
"match the style" request. Once approved, both tool writes and final diff use it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

ENV_CONTRACT = "AGENTRELAY_CHANGE_CONTRACT_JSON"
STYLE_SUFFIXES = {".css", ".scss", ".sass", ".less", ".styl", ".html", ".htm", ".vue", ".svelte"}
STYLE_SOURCE_NAMES = {"config_web.py"}
WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch", "applypatch",
               "writefile", "createfile", "editfile", "strreplaceeditor", "searchreplace",
               "replacefilecontent"}
COMMAND_TOOLS = {"bash", "shell", "exec_command", "execute_command", "run_command"}
COMPLETION = re.compile(r"(?:已完成|已修复|验证通过|测试通过|检查通过)")
REFERENCE_COMPARISON = re.compile(
    r"(?:对照|比较|对比|一致|相同|相符|符合|差异|compared?|comparison|same|match|diff)",
    re.IGNORECASE,
)


def contract_for_prompt(prompt: str, workspace: str, files: Iterable[str] = (),
                        references: Iterable[str] = (), creative: bool | None = None,
                        style_only: bool | None = None) -> dict[str, Any]:
    """Create an explicit contract; creative freedom defaults to strict reference."""
    allowed = normalize_files(workspace, files)
    refs = [str(value).strip() for value in references if str(value).strip()]
    if creative is None:
        creative = bool(re.search(r"自由发挥|自由优化|可以优化得更好看|允许.*(?: redesign|creative)", prompt, re.I))
    if style_only is None:
        style_only = bool(re.search(r"只(?:修改|改|调整)?(?:样式|风格|颜色|布局)|复刻|UI|界面|页面", prompt, re.I))
    return {
        "version": 1, "mode": "creative" if creative else "strict",
        "workspace": str(Path(workspace).expanduser().resolve()),
        "allowed_files": allowed, "references": refs,
        "style_only": bool(style_only), "approved": False,
        "baseline": {}, "created_by": "user",
        "reference_verified": False, "reference_evidence": [],
        "target_scope": "", "allowed_properties": [],
        "forbidden_effects": ["公共组件", "兄弟元素布局", "其他模块"],
        "shared_files": [], "shared_impact": False,
        "reuse_checked": False, "reuse_evidence": [], "new_component_approved": False,
        "unchanged_checks": [], "unchanged_results": [],
    }


def normalize_files(workspace: str, files: Iterable[str]) -> list[str]:
    root = Path(workspace).expanduser().resolve()
    result = []
    for value in files:
        text = str(value).strip()
        if not text:
            continue
        path = Path(text).expanduser()
        absolute = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            relative = absolute.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"允许文件必须在 workspace 内：{text}") from exc
        if relative not in result:
            result.append(relative)
    return result


def require_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict) or contract.get("version") != 1:
        raise ValueError("无效变更约束")
    if not contract.get("references") or any("待确认" in str(value) for value in contract["references"]):
        raise ValueError("严格变更必须指定具体 UI 参考文件、页面或截图")
    if not contract.get("allowed_files"):
        raise ValueError("严格变更必须指定允许修改的文件")
    normalize_files(str(contract.get("workspace") or "."), contract["allowed_files"])


def card_fields(contract: dict[str, Any]) -> list[tuple[str, str]]:
    mode = "授权范围内自由优化" if contract.get("mode") == "creative" else "默认严格参考，不擅自优化"
    fields = []
    if contract.get("target_scope"):
        fields.append(("作用域", str(contract["target_scope"])))
    fields.extend([
        ("UI 参考", "、".join(contract.get("references") or ["必须明确指定"])),
        ("创作权限", mode),
        ("允许修改", "、".join(contract.get("allowed_files") or ["必须明确指定"])),
    ])
    if contract.get("allowed_properties"):
        fields.append(("允许属性", "、".join(contract["allowed_properties"])))
    fields.append(("复用优先", "已检索并优先复用：" + "、".join(contract["reuse_evidence"])
                   if contract.get("reuse_checked") and contract.get("reuse_evidence")
                   else "必须先检索公共组件、样式变量和相似实现；不得另造同类组件"))
    if contract.get("new_component_approved"):
        fields.append(("新组件授权", "已明确批准；仅限批准范围，不得扩展成第二套设计系统"))
    fields.append(("禁止修改", "名单外文件；接口、字段、数据、排序和业务逻辑" if contract.get("style_only")
                   else "名单外文件和未授权业务范围"))
    if contract.get("forbidden_effects"):
        fields.append(("禁止副作用", "、".join(contract["forbidden_effects"])))
    if contract.get("shared_files"):
        fields.append(("共享影响", "允许修改共享实现，但必须限在声明作用域" if contract.get("shared_impact")
                       else "禁止修改共享组件；确需影响须重新确认：" + "、".join(contract["shared_files"])))
    fields.append(("额外 Bug", "只报告位置、原因和影响；未经明确要求不修复"))
    if contract.get("unchanged_checks"):
        fields.append(("不变验收", "、".join(contract["unchanged_checks"])))
    fields.append(("验收", "参考一致；真实 diff 无越界；数据、接口与逻辑保持冻结" if contract.get("style_only")
                   else "参考一致；真实 diff 无越界文件"))
    return fields


def tool_targets(tool_name: str, tool_input: Any) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    targets = [tool_input.get(key) for key in ("file_path", "path", "file", "target_file")]
    patch = tool_input.get("input") or tool_input.get("patch")
    if isinstance(patch, str):
        targets.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+)$", patch, re.MULTILINE))
    return [str(value).strip() for value in targets if isinstance(value, str) and value.strip()]


def check_tool_write(contract: dict[str, Any], tool_name: str, tool_input: Any) -> str | None:
    """Allowlist is enforced after approval too, not only while awaiting approval."""
    if not contract.get("approved"):
        return None
    normalized = re.sub(r"[^a-z0-9]", "", tool_name.lower())
    if normalized in COMMAND_TOOLS:
        return None  # Command effects are captured by the actual before/after diff.
    if normalized not in WRITE_TOOLS:
        return None
    if contract.get("target_scope") and not contract.get("reuse_checked"):
        return ("[AgentRelay 复用优先门禁] 尚未检索项目已有组件、样式变量和相似实现，已阻断。"
                "先列出可复用依据；确需新组件必须说明现有实现缺口并重新获得明确批准。")
    if contract.get("target_scope") and not contract.get("new_component_approved") and not contract.get("reuse_evidence"):
        return ("[AgentRelay 复用优先门禁] 没有公共实现复用依据，不能自行另造骨架/组件。"
                "请补充检索证据或申请新组件授权。")
    if contract.get("target_scope") and contract.get("reuse_evidence"):
        try:
            validate_reuse_evidence(contract)
        except ValueError as exc:
            return f"[AgentRelay 复用优先门禁] {exc}；不能编造公共组件。"
    targets = tool_targets(tool_name, tool_input)
    if not targets:
        return "[AgentRelay 变更约束] 无法确定写入目标，已阻断；请明确目标文件。"
    allowed = set(contract.get("allowed_files") or [])
    shared = set(contract.get("shared_files") or [])
    workspace = str(contract.get("workspace") or ".")
    for target in targets:
        path = Path(target).expanduser()
        absolute = path.resolve() if path.is_absolute() else (Path(workspace) / path).resolve()
        try:
            relative = absolute.relative_to(Path(workspace).resolve()).as_posix()
        except ValueError:
            return f"[AgentRelay 变更约束] 写入越出 workspace：{target}；已阻断。"
        if relative in shared and not contract.get("shared_impact"):
            return (f"[AgentRelay 作用域边界] 目标是「{contract.get('target_scope') or '局部元素'}」，"
                    f"但 {relative} 属于共享组件/公共样式，修改会波及其他模块，已阻断。"
                    "必须提出共享影响并重新确认；只有明确全局/模块级授权才可扩大。")
        if relative not in allowed:
            return (f"[AgentRelay 变更约束] {relative} 不在允许修改名单内，已阻断。"
                    "需要扩大范围时必须重新确认；额外 Bug 只报告，不擅自修复。")
    return None


def validate_reuse_evidence(contract: dict[str, Any]) -> list[str]:
    """Reuse references must be real project files, not invented component names."""
    evidence = contract.get("reuse_evidence") or []
    root = Path(contract["workspace"]).resolve()
    for value in evidence:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise ValueError(f"复用证据不存在：{value}")
    return list(evidence)


def validate_unchanged_evidence(contract: dict[str, Any]) -> bool:
    """Every declared outside invariant needs an actual passing result."""
    expected = [item for item in (contract.get("unchanged_checks") or []) if item]
    results = contract.get("unchanged_results") or []
    if expected and not results:
        raise ValueError("目标外检查结果不能为空，不能只写无副作用")
    covered = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        check = str(item.get("check") or "")
        if check not in expected:
            continue
        if not item.get("passed") or not str(item.get("actual") or "").strip():
            raise ValueError(f"目标外检查未通过或缺少实际结果：{check}")
        covered.add(check)
    if covered != set(expected):
        raise ValueError("目标外检查未覆盖全部声明项目")
    return True


def approve(contract: dict[str, Any]) -> dict[str, Any]:
    require_contract(contract)
    updated = dict(contract)
    updated["approved"] = True
    updated["baseline"] = _worktree_files(updated["workspace"])
    updated["preexisting_changes"] = _git_changes(updated["workspace"])
    return updated


def _git_changes(workspace: str) -> list[str]:
    root = Path(workspace).resolve()
    try:
        result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                cwd=root, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode:
        return []
    changed = []
    for entry in result.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if len(entry) < 4 or entry[:2].startswith("R"):
            continue
        changed.append(Path(entry[3:]).as_posix())
    return sorted(set(changed))


def _is_ignored(path: Path, workspace: Path) -> bool:
    relative = path.relative_to(workspace)
    return any(part in {".git", "__pycache__", "node_modules", "runtime", ".agentrelay"} for part in relative.parts)


def _worktree_files(workspace: str) -> dict[str, str | None]:
    root = Path(workspace).resolve()
    if not root.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and not _is_ignored(path, root)}
def snapshot(workspace: str, files: Iterable[str]) -> dict[str, str | None]:
    root = Path(workspace).resolve()
    result = {}
    for relative in normalize_files(workspace, files):
        path = root / relative
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def changed_files(workspace: str, before: dict[str, str | None]) -> list[str]:
    """Use git for actual diff; fall back to baseline for non-git workspaces."""
    root = Path(workspace).resolve()
    try:
        result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                cwd=root, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        return _git_changes(workspace)
    current = _worktree_files(workspace)
    before = {name: digest for name, digest in before.items() if name is not None}
    return sorted(set(relative for relative, digest in current.items() if before.get(relative) != digest)
                  | set(relative for relative in before if relative not in current))


def diff_violations(contract: dict[str, Any]) -> list[str]:
    if not contract.get("approved"):
        return []
    allowed = set(contract.get("allowed_files") or [])
    changes = [name for name in changed_files(contract["workspace"], contract.get("baseline") or {})
               if name not in set(contract.get("preexisting_changes") or [])]
    violations = [f"名单外文件发生变化：{name}" for name in changes if name not in allowed]
    shared = set(contract.get("shared_files") or [])
    if not contract.get("shared_impact"):
        violations.extend(f"局部目标禁止修改共享组件/公共样式：{name}" for name in changes if name in shared)
    if contract.get("style_only"):
        for name in changes:
            if name in allowed and Path(name).suffix.lower() not in STYLE_SUFFIXES and Path(name).name not in STYLE_SOURCE_NAMES:
                violations.append(f"样式任务禁止修改数据、接口或业务文件：{name}")
    return sorted(set(violations))


def completion_block(contract: dict[str, Any], message: str) -> str | None:
    if contract.get("draft") or not contract.get("approved") or not COMPLETION.search(str(message or "")):
        return None
    if not contract.get("reference_verified"):
        return ("[AgentRelay 变更约束验收失败]\n尚未对照指定 UI 参考完成真实验收；"
                "不能把浏览器可运行等同于参考一致。请提供参考核对记录/截图证据，或明确报告未验证。")
    if contract.get("unchanged_checks"):
        try:
            validate_unchanged_evidence(contract)
        except ValueError as exc:
            return "[AgentRelay 作用域验收失败]\n" + str(exc)
    evidence = contract.get("reference_evidence")
    if not isinstance(evidence, list) or not evidence:
        return "[AgentRelay 变更约束验收失败]\n缺少 UI 参考核对证据，不能声称参考一致。"
    try:
        validate_reference_evidence(contract, evidence)
    except ValueError as exc:
        return "[AgentRelay 变更约束验收失败]\n" + str(exc)
    violations = diff_violations(contract)
    if violations:
        return "[AgentRelay 变更约束验收失败]\n" + "\n".join(violations) + "\n不能声称按约束完成；需恢复越界修改或重新获得用户批准。"
    return None


def child_prompt(contract: dict[str, Any]) -> str:
    fields = "\n".join(f"{label}：{value}" for label, value in card_fields(contract))
    return ("[AgentRelay 严格变更约束——子 Agent 必须原样遵守]\n" + fields +
            "\n不得扩大范围；发现额外 Bug 只报告。需要改变约定时必须交回主 Agent 重新确认。")


def with_child_environment(contract: dict[str, Any], environ: dict[str, str]) -> dict[str, str]:
    child = dict(environ)
    child[ENV_CONTRACT] = json.dumps(contract, ensure_ascii=False)
    return child


def _normalized_evidence_key(value: str) -> str:
    return re.sub(r"[\s:：。.!！]+", "", str(value or "")).lower()


def validate_reference_evidence(contract: dict[str, Any], evidence: Iterable[str]) -> list[str]:
    """Require comparison text that itemizes every declared target boundary."""
    paths = []
    chunks = []
    for value in evidence:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = Path(contract["workspace"]) / path
        path = path.resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"参考核对证据不存在或为空：{value}")
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"参考核对证据无法读取为文本：{value}") from exc
        paths.append(str(path))
    if not paths:
        raise ValueError("至少提供一份真实参考核对证据")

    text = "\n".join(chunks).strip()
    if len(text) < 8 or not REFERENCE_COMPARISON.search(text):
        raise ValueError("参考核对证据必须包含对目标、属性或不变项的真实对照结果，不能只放任意字符")

    required = [str(contract.get("target_scope") or "").strip()]
    required.extend(str(item).strip() for item in contract.get("unchanged_checks") or [] if str(item).strip())
    required = [item for item in dict.fromkeys(required) if item]
    if not required:
        return paths

    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?([^:：]{1,160})[:：]\s*(.+?)\s*$", line)
        if match:
            key = _normalized_evidence_key(match.group(1))
            value = match.group(2).strip()
            if key and value:
                entries[key] = value

    def covered(item: str) -> bool:
        wanted = _normalized_evidence_key(item)
        return any(wanted == key or wanted in key or key in wanted for key in entries)

    target_scope = str(contract.get("target_scope") or "").strip()
    if target_scope and not covered(target_scope):
        raise ValueError("参考核对证据必须逐项对照目标元素，不能只写泛泛结论")
    missing = [
        str(item) for item in contract.get("unchanged_checks") or []
        if str(item).strip() and not covered(str(item))
    ]
    if missing:
        raise ValueError("参考核对证据没有逐项覆盖目标外检查：" + "、".join(missing))
    return paths


def record_reference_evidence(codex_home: Path, session_id: str, evidence: Iterable[str]) -> dict[str, Any]:
    """Attach itemized comparison evidence to the session's approved contract."""
    state_path = codex_home / "agent_relay_tracker" / "sessions" / f"{session_id}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("当前会话没有已批准的变更约束") from exc
    contract = state.get("change_contract")
    if not isinstance(contract, dict) or not contract.get("approved"):
        raise ValueError("当前会话没有已批准的变更约束")
    paths = validate_reference_evidence(contract, evidence)
    contract = dict(contract, reference_verified=True, reference_evidence=paths)
    if contract.get("unchanged_checks"):
        validate_unchanged_evidence(contract)
        contract["unchanged_verified"] = True
    state["change_contract"] = contract
    state.setdefault("task_gate", {})["change_contract"] = contract
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, state_path)
    return contract


def from_environment(environ: dict[str, str] | None = None) -> dict[str, Any] | None:
    raw = (environ or os.environ).get(ENV_CONTRACT, "")
    if not raw:
        return None
    data = json.loads(raw)
    require_contract(data)
    return data


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify-reference", help="记录已完成的 UI 参考对照证据")
    verify.add_argument("--session", required=True)
    verify.add_argument("--evidence", action="append", required=True,
                        help="真实截图或对照报告路径；可重复传入")
    args = parser.parse_args()
    if args.command == "verify-reference":
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        contract = record_reference_evidence(home, args.session, args.evidence)
        print(json.dumps({"status": "reference-verified", "evidence": contract["reference_evidence"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
