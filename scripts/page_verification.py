"""Bind real browser evidence to the page files changed in a Hook session."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digest(path: Path) -> str:
    return _digest(path) if path.is_file() else ""


def _workspace(cwd: str | None = None) -> Path:
    return Path(cwd or os.getcwd()).expanduser().resolve()


def _state_file(codex_home: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    if not safe:
        raise ValueError("page verification requires a session id")
    return codex_home / "agent_relay_tracker" / "page-verification" / f"{safe}.json"


def read_state(codex_home: Path, session_id: str) -> dict[str, Any]:
    try:
        data = json.loads(_state_file(codex_home, session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(codex_home: Path, session_id: str, state: dict[str, Any]) -> Path:
    path = _state_file(codex_home, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


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
    state = read_state(codex_home, session_id)
    state["command_dirty_pages"] = sorted(before)
    write_state(codex_home, session_id, state)


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
    state = read_state(codex_home, session_id)
    files = state.setdefault("files", {})
    for value in changed:
        path = Path(value)
        files[value] = {"sha256": _digest(path), "changed_at": time.time()}
    state["workspace"] = str(_workspace(cwd))
    state.pop("verification", None)
    write_state(codex_home, session_id, state)
    return changed


def completion_claim(message: str) -> bool:
    text = str(message or "")
    return not NEGATIVE_PATTERN.search(text) and any(re.search(pattern, text, re.I) for pattern in COMPLETION_PATTERNS)


def verify_evidence(codex_home: Path, session_id: str) -> str | None:
    """Return a blocking reason, or None when current page bytes were really checked."""
    state = read_state(codex_home, session_id)
    files = state.get("files")
    if not isinstance(files, dict) or not files:
        return None
    evidence = state.get("verification")
    if not isinstance(evidence, dict) or evidence.get("status") != "passed":
        return _instruction("本轮修改了页面，但没有成功的真实浏览器检查记录。")
    if any(not Path(value).name == "config_web.py" for value in files) and not evidence.get("url"):
        return _instruction("修改了配置中心以外的页面，必须用 --url 指定实际运行的本地页面重新检查。")
    if not evidence.get("browser") or not evidence.get("screenshots"):
        return _instruction("验证记录缺少真实浏览器或截图证据。")
    for value, changed in files.items():
        path = Path(value)
        if not path.is_file() or not isinstance(changed, dict):
            return _instruction(f"页面文件无法核对：{value}")
        if _source_digest(path) != changed.get("sha256"):
            return _instruction(f"页面文件在检查后又有修改，必须重新检查：{value}")
        checked = (evidence.get("files") or {}).get(value)
        if not isinstance(checked, dict) or checked.get("sha256") != changed.get("sha256"):
            return _instruction(f"浏览器证据没有覆盖当前页面文件：{value}")
    for value in evidence["screenshots"]:
        if not isinstance(value, str) or not Path(value).is_file():
            return _instruction("浏览器截图不存在，不能声称页面已检查。")
    return None


def _instruction(reason: str) -> str:
    return ("[AgentRelay 页面验证门禁] " + reason +
            " 不得声称页面已完成或验证通过。请运行 scripts/verify_page.py（真实 Chromium），"
            "检查控制台、主要导航、主题和桌面/移动截图；失败要如实报告，修复后重新验证。")
