"""Bind real browser evidence to the page files changed in a Hook session."""
from __future__ import annotations

import hashlib
import importlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from . import verification_state as _verification_state
except ImportError:
    import verification_state as _verification_state

PAGE_SUFFIXES = {".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
PAGE_SOURCE_NAMES = {"config_web.py"}
WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch", "applypatch",
               "writefile", "createfile", "editfile", "multiedit", "strreplaceeditor",
               "searchreplace", "replacefilecontent"}
COMPLETION_PATTERNS = (
    r"(?:页面|界面|UI|前端|样式|配置中心)[^\n。！？]{0,60}(?:已完成|已修复|已修改|检查通过|验证通过|测试通过)",
    r"(?:已完成|已修复|验证通过|测试通过)[^\n。！？]{0,60}(?:页面|界面|UI|前端|样式|配置中心)",
)
NEGATIVE_PATTERN = re.compile(r"未验证|尚未验证|没有验证|未检查|尚未检查|未完成|无法验证|不能验证")
FUTURE_PATTERN = re.compile(
    r"(?:将|将要|将会|预计|计划|准备|拟|待|后续|下一步|确认后|实施后|修改后|完成后)"
    r"[^\n。！？；;]{0,80}(?:页面|界面|UI|前端|样式|配置中心)",
    re.IGNORECASE,
)
META_PATTERN = re.compile(
    r"是否|为什么|为何|什么|怎么|如何|能否|可否|请问|用于|用来|作用是|"
    r"含义|机制|规则|判断|识别|检测|解释|说明|表示|示例|例如|"
    r"如果|假如|假设|计划|准备|后续|下一步|"
    r"\b(?:whether|explain|example|rule|if|plan|would|should)\b|"
    r"(?:吗|呢|么)[。.!！?？\s]*$",
    re.IGNORECASE,
)
QUOTED_SPAN = re.compile(r"“[^”\n]{0,300}”|‘[^’\n]{0,300}’|\"[^\"\n]{0,300}\"|'[^'\n]{0,300}'")
CLAUSE_SPLIT = re.compile(r"[。！？\n；;]+")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digest(path: Path) -> str:
    return _digest(path) if path.is_file() else ""


def _workspace(cwd: str | None = None) -> Path:
    return Path(cwd or os.getcwd()).expanduser().resolve()


def _state_file(codex_home: Path, session_id: str) -> Path:
    return _verification_state.state_path(codex_home, session_id)


def read_state(codex_home: Path, session_id: str) -> dict[str, Any]:
    return _verification_state.read_state(codex_home, session_id)


def write_state(codex_home: Path, session_id: str, state: dict[str, Any]) -> Path:
    return _verification_state.write_state(codex_home, session_id, state)


def _candidate_paths(tool_input: Any) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    paths = [tool_input.get(key) for key in ("file_path", "path", "file", "target_file")]
    patch = tool_input.get("input") or tool_input.get("patch")
    if isinstance(patch, str):
        paths.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+)$", patch, re.MULTILINE))
    return [str(value).strip() for value in paths if isinstance(value, str) and value.strip()]


def _is_page(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    return (path.suffix.lower() in PAGE_SUFFIXES and not lowered_parts.intersection({"node_modules", "dist", "build"})) \
        or path.name in PAGE_SOURCE_NAMES


def _command_text(tool_name: str, tool_input: Any) -> str:
    if str(tool_name).lower() not in {"bash", "shell", "exec_command", "execute_command", "run_command"}:
        return ""
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            if isinstance(tool_input.get(key), str):
                return tool_input[key]
    return ""


def _git_page_changes(cwd: str, before: set[str] | None = None) -> list[Path]:
    """Cover Bash/Python formatters too; use actual worktree changes, not command keywords."""
    try:
        result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                cwd=_workspace(cwd), capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode:
        return []
    entries = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    paths = []
    for index, entry in enumerate(entries):
        if len(entry) < 4:
            continue
        if entry[:2].startswith("R"):
            continue
        value = entry[3:]
        path = Path(value)
        if not path.is_absolute():
            path = _workspace(cwd) / path
        path = path.resolve()
        if _is_page(path) and path.is_file() and (before is None or str(path) not in before):
            paths.append(path)
    return paths


def record_command_start(codex_home: Path, session_id: str, cwd: str,
                         tool_name: str, tool_input: Any) -> None:
    if not _command_text(tool_name, tool_input):
        return
    before = {str(path) for path in _git_page_changes(cwd)}
    def update(state: dict[str, Any]) -> None:
        state["command_dirty_pages"] = sorted(before)

    _verification_state.mutate_state(codex_home, session_id, update)


def record_page_edit(codex_home: Path, session_id: str, cwd: str,
                     tool_name: str, tool_input: Any, tool_output: Any = None) -> list[str]:
    """Remember only successful writes whose targets exist on disk."""
    normalized = re.sub(r"[^a-z0-9]", "", str(tool_name).lower())
    command = _command_text(tool_name, tool_input)
    if normalized not in WRITE_TOOLS and not command:
        return []
    if isinstance(tool_output, dict) and (tool_output.get("is_error") or tool_output.get("exit_code") not in (None, 0)):
        return []
    paths = []
    if normalized in WRITE_TOOLS:
        for value in _candidate_paths(tool_input):
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = _workspace(cwd) / path
            paths.append(path.resolve())
    else:
        paths = _git_page_changes(cwd, set(read_state(codex_home, session_id).get("command_dirty_pages") or []))
    changed = []
    for path in paths:
        if _is_page(path) and path.is_file():
            changed.append(str(path))
    changed = list(dict.fromkeys(changed))
    if not changed:
        return []
    def update(state: dict[str, Any]) -> None:
        files = state.setdefault("files", {})
        for value in changed:
            path = Path(value)
            files[value] = {"sha256": _digest(path), "changed_at": time.time()}
        state["workspace"] = str(_workspace(cwd))
        state.pop("verification", None)
        for key in ("target_review", "unchanged_results", "visual_review", "cleanup_completed"):
            state.pop(key, None)

    _verification_state.mutate_state(codex_home, session_id, update)
    return changed


def completion_claim(message: str) -> bool:
    text = QUOTED_SPAN.sub(" ", str(message or ""))
    if NEGATIVE_PATTERN.search(text):
        return False
    clauses = [part.strip() for part in CLAUSE_SPLIT.split(text) if part.strip()]
    return any(
        (not META_PATTERN.search(part))
        and (not FUTURE_PATTERN.search(part))
        and any(re.search(pattern, part, re.I) for pattern in COMPLETION_PATTERNS)
        for part in clauses
    )


def _target_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(str(value.get("selector") or "").strip())
        and isinstance(value.get("styles"), dict)
        and isinstance(value.get("rect"), dict)
    )


def _target_review_reason(state: dict[str, Any]) -> str | None:
    review = state.get("target_review")
    if not isinstance(review, dict):
        return "缺少修改后目标验收记录，不能证明目标元素达到预期状态。"
    if review.get("status") != "passed" or not review.get("target_verified"):
        return "指定目标元素没有变成预期状态，不能声称修改成功。"
    before = review.get("before")
    after = review.get("after")
    if not _target_snapshot(before) or not _target_snapshot(after):
        return "目标验收缺少修改前后的真实元素快照，不能只记录 target_verified。"
    if before.get("selector") != after.get("selector"):
        return "目标验收的修改前后选择器不一致，不能证明同一个元素已修改。"
    expected = review.get("expected_changes")
    if not isinstance(expected, dict) or not expected:
        return "目标验收缺少 expected_changes，不能证明目标属性符合要求。"
    checks = {}
    for prop, wanted in expected.items():
        actual = after.get("styles", {}).get(prop)
        if isinstance(wanted, str) and wanted.startswith("!="):
            checks[prop] = actual != before.get("styles", {}).get(prop) and actual != wanted[2:]
        else:
            checks[prop] = str(actual) == str(wanted)
    if not checks or not all(checks.values()):
        return "目标快照没有满足 expected_changes，不能声称修改成功。"
    if not any(
        after.get("styles", {}).get(prop) != before.get("styles", {}).get(prop)
        for prop in expected
    ):
        return "目标元素修改前后完全相同，没有实际变化，不能声称目标已经修改。"

    unchanged_checks = [
        str(item).strip() for item in review.get("unchanged_checks") or [] if str(item).strip()
    ]
    unchanged_results = state.get("unchanged_results")
    if not unchanged_checks or not isinstance(unchanged_results, list) or not unchanged_results:
        return "目标验收缺少目标外检查结果，不能证明兄弟元素和其他模块无副作用。"
    covered = set()
    for item in unchanged_results:
        if not isinstance(item, dict):
            return "目标外检查结果格式无效，不能证明无副作用。"
        check = str(item.get("check") or "").strip()
        if not item.get("passed") or not str(item.get("actual") or "").strip():
            return f"目标外检查未通过或缺少实际结果：{check or '未命名项目'}。"
        if check in unchanged_checks:
            covered.add(check)
    if covered != set(unchanged_checks):
        return "目标外检查没有覆盖全部声明项目，不能证明无副作用。"
    return None


def _visual_certificate_reason(review: dict[str, Any], message: str = "") -> str:
    try:
        module = importlib.import_module("scripts.visual_review")
    except ImportError:
        try:
            module = importlib.import_module("visual_review")
        except ImportError:
            return "无法加载视觉认证校验器，不能把 status=passed 当成逐项视觉验收。"
    reason = module.review_certificate_reason(review, message)
    return str(reason or "")


def verify_evidence(codex_home: Path, session_id: str, message: str = "") -> str | None:
    """Return a blocking reason, or None when current page bytes were really checked."""
    state = read_state(codex_home, session_id)
    files = state.get("files")
    if not isinstance(files, dict) or not files:
        if state.get("target_before") or state.get("target_review"):
            return _instruction("页面目标验收缺少被修改文件记录，无法核对证据是否仍然有效。")
        if completion_claim(message):
            return _instruction("页面完成声明缺少本轮页面文件修改记录，无法核对浏览器证据。")
        return None
    evidence = state.get("verification")
    if not isinstance(evidence, dict) or evidence.get("status") != "passed":
        return _instruction("本轮修改了页面，但没有成功的真实浏览器检查记录。")
    if any(Path(value).name != "config_web.py" for value in files) and not evidence.get("url"):
        return _instruction("修改了配置中心以外的页面，必须用 --url 指定实际运行的本地页面重新检查。")
    if not evidence.get("browser"):
        return _instruction("验证记录缺少真实浏览器。")
    for value, changed in files.items():
        path = Path(value)
        if not path.is_file() or not isinstance(changed, dict):
            return _instruction(f"页面文件无法核对：{value}")
        current_digest = _source_digest(path)
        if current_digest != changed.get("sha256"):
            return _instruction(f"页面文件在检查后又有修改，必须重新检查：{value}")
        checked = (evidence.get("files") or {}).get(value)
        if not isinstance(checked, dict) or checked.get("sha256") != current_digest:
            return _instruction(f"浏览器证据没有覆盖当前页面文件：{value}")
    if state.get("target_before") and not state.get("target_review"):
        return _instruction("只有修改前目标快照，尚未完成 --after 目标验收，不能声称修改成功。")
    if state.get("target_review"):
        reason = _target_review_reason(state)
        if reason:
            return _instruction(reason)
    if not state.get("cleanup_completed"):
        for value in evidence.get("screenshots") or []:
            if not isinstance(value, str) or not Path(value).is_file():
                return _instruction("浏览器截图不存在，不能声称页面已检查。")
        return _instruction("视觉审查后必须运行 visual_review.py cleanup 删除截图，不能保留垃圾。")
    if not state.get("visual_review", {}).get("screenshots_deleted"):
        return _instruction("清理状态缺少截图删除证据，不能把清理当作验收。")
    review = state.get("visual_review") or {}
    if review.get("status") == "user_review_required":
        return _instruction("当前模型无视觉或视觉审查未通过，仍需用户复查，不能声称视觉验收通过。")
    if review.get("status") != "passed":
        return _instruction("缺少逐项真实视觉审查，不能声称页面完成。")
    certificate_reason = _visual_certificate_reason(review, message)
    if certificate_reason:
        return _instruction(certificate_reason)
    return None


def _instruction(reason: str) -> str:
    return ("[AgentRelay 页面验证门禁] " + reason +
            " 不得声称页面已完成或验证通过。请运行 scripts/verify_page.py（真实 Chromium），"
            "检查控制台、主要导航、主题和桌面/移动截图；失败要如实报告，修复后重新验证。")
