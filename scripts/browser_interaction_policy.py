"""Detect browser interactions and direct system-pointer actions needing consent."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any


MODES = {"background", "foreground"}
PAGE_INTERACTION_ACTIONS = {
    "check",
    "click",
    "dblclick",
    "double_click",
    "drag",
    "press",
    "right_click",
    "select",
    "uncheck",
}
POINTER_ACTIONS = {
    "click",
    "double_click",
    "drag",
    "left_click",
    "middle_click",
    "mouse_click",
    "mouse_down",
    "mouse_move",
    "mouse_up",
    "right_click",
    "scroll",
    "triple_click",
}
DIRECT_TOOL_MARKERS = (
    "computer",
    "desktop_control",
    "mouse",
    "pointer",
)
BROWSER_TOOL_MARKERS = (
    "browser",
    "playwright",
)
DIRECT_COMMAND = re.compile(
    r"(?:"
    r"\bpyautogui\.(?:click|doubleClick|drag|moveTo|scroll)\s*\(|"
    r"\bcliclick\b|"
    r"\bxdotool\b[^\n]*(?:click|mousemove|mousedown|mouseup)|"
    r"\bydotool\b[^\n]*(?:click|mousemove)|"
    r"\bosascript\b[^\n]*System Events[^\n]*click|"
    r"\bmouse_event\b|"
    r"\bSendInput\s*\("
    r")",
    re.I,
)
BROWSER_COMMAND = re.compile(
    r"(?:"
    r"\bplaywright\b[^\n]*(?:sync_playwright|chromium\.launch|"
    r"\.goto\s*\(|\.click\s*\(|\.fill\s*\(|\.press\s*\(|"
    r"keyboard\.|mouse\.|headless\s*=)|"
    r"\bagent_relay(?:\.py)?\b[^\n]*(?:--monitor|"
    r"AGENT_RELAY_WEB_HEADFUL\s*=\s*1|"
    r"AGENT_RELAY_WEB_INTERACTION_MODE\s*=)|"
    r"AGENT_RELAY_WEB_HEADFUL\s*=\s*1[^\n]*\bagent_relay(?:\.py)?\b"
    r")",
    re.I,
)
PLAYWRIGHT_MUTATION = re.compile(
    r"(?:"
    r"\.(?:click|dblclick|press|select_option|check|uncheck|"
    r"press_sequentially|drag_to)\s*\(|"
    r"\bkeyboard\.(?:press|type|down|up)\s*\(|"
    r"\bmouse\.(?:click|down|up|move|wheel)\s*\("
    r")",
    re.I,
)
MODE_PATTERN = re.compile(
    r"(?:--interaction-mode(?:\s+|=)|"
    r"AGENT_RELAY_WEB_INTERACTION_MODE\s*=\s*)"
    r"[\"']?(foreground|background)\b",
    re.I,
)


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").split())


def _tool_name(tool_name: Any) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", _normalize(tool_name).lower())


def _command_text(tool_input: Any) -> str:
    if isinstance(tool_input, str):
        return tool_input.replace("\r\n", "\n").strip()
    if isinstance(tool_input, (list, tuple)):
        return " ".join(str(item) for item in tool_input)
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd", "script", "shell_command", "shellCommand"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.replace("\r\n", "\n").strip()
    argv = tool_input.get("argv")
    if isinstance(argv, (list, tuple)):
        return " ".join(str(item) for item in argv)
    return ""


def _is_patch_payload(command: str) -> bool:
    """A code patch may mention browser filenames without running them."""
    return (
        "*** Begin Patch" in command
        or "*** Update File:" in command
        or "*** Add File:" in command
    )


def _invokes_browse_script(command: str) -> bool:
    """Match an actual browse.py invocation, not grep/cat/edit text."""
    if "browse.py" not in command:
        return False
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    for index, token in enumerate(tokens):
        cleaned = token.strip("\"'")
        if Path(cleaned).name != "browse.py":
            continue
        if index == 0:
            return True
        previous = Path(tokens[index - 1].strip("\"'")).name.lower()
        if previous == "py" or previous.startswith("python"):
            return True
    return False


def _mode_from_value(value: Any) -> str:
    mode = _normalize(value).lower()
    return mode if mode in MODES else ""


def interaction_mode(tool_name: Any, tool_input: Any) -> str:
    """Return a mode explicitly supplied by the caller, never a silent default."""
    del tool_name
    if isinstance(tool_input, dict):
        for key in (
            "interaction_mode",
            "execution_mode",
            "browser_mode",
            "display_mode",
        ):
            mode = _mode_from_value(tool_input.get(key))
            if mode:
                return mode
        foreground = tool_input.get("foreground")
        if isinstance(foreground, bool):
            return "foreground" if foreground else "background"
        headless = tool_input.get("headless")
        if isinstance(headless, bool):
            return "background" if headless else "foreground"
    command = _command_text(tool_input)
    match = MODE_PATTERN.search(command)
    if match:
        return match.group(1).lower()
    if re.search(r"(?:^|\s)--headed(?:\s|$)", command):
        return "foreground"
    if re.search(r"(?:^|\s)--headless(?:\s|$)", command):
        return "background"
    headless = re.search(r"\bheadless\s*[:=]\s*(true|false)\b", command, re.I)
    if headless:
        return "background" if headless.group(1).lower() == "true" else "foreground"
    headful = re.search(
        r"AGENT_RELAY_WEB_HEADFUL\s*=\s*(0|1|true|false|yes|no|on|off)",
        command,
        re.I,
    )
    if headful:
        enabled = headful.group(1).lower() in {"1", "true", "yes", "on"}
        return "foreground" if enabled else "background"
    return ""


def _action_value(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    value = tool_input.get("action")
    if value is None:
        value = tool_input.get("operation")
    return _normalize(value).lower()


def _cwd(tool_input: Any) -> Path:
    if isinstance(tool_input, dict):
        value = str(tool_input.get("cwd") or "").strip()
        if value:
            return Path(value).expanduser()
    return Path.cwd()


def _workflow_path(tool_name: Any, tool_input: Any) -> Path | None:
    """Resolve `browse.py run <steps.json>` without trusting the command alone."""
    del tool_name
    command = _command_text(tool_input)
    if "browse.py" not in command:
        return None
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    browse_index = next(
        (
            index
            for index, token in enumerate(tokens)
            if Path(token.strip("\"'")).name == "browse.py"
        ),
        None,
    )
    if browse_index is None:
        return None
    try:
        run_index = tokens.index("run", browse_index + 1)
    except ValueError:
        return None
    for token in tokens[run_index + 1:]:
        if token.startswith("-"):
            continue
        path = Path(token.strip("\"'")).expanduser()
        return path if path.is_absolute() else _cwd(tool_input) / path
    return None


def _workflow_details(tool_name: Any, tool_input: Any) -> dict[str, Any]:
    path = _workflow_path(tool_name, tool_input)
    if path is None:
        return {
            "workflow_path": None,
            "workflow_sha256": None,
            "workflow_actions": [],
            "interaction_count": 0,
        }
    resolved = path.resolve()
    try:
        content = resolved.read_bytes()
    except OSError:
        return {
            "workflow_path": str(resolved),
            "workflow_sha256": None,
            "workflow_actions": [],
            "interaction_count": 0,
        }
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = []
    actions = []
    if isinstance(payload, list):
        for index, step in enumerate(payload, 1):
            if not isinstance(step, dict):
                continue
            action = _normalize(step.get("action")).lower()
            if action in PAGE_INTERACTION_ACTIONS:
                actions.append({
                    "step": index,
                    "action": action,
                    "selector": _normalize(step.get("selector"))[:240],
                })
    return {
        "workflow_path": str(resolved),
        "workflow_sha256": hashlib.sha256(content).hexdigest(),
        "workflow_actions": actions,
        "interaction_count": len(actions),
    }


def _described_workflow_actions(
    tool_name: Any,
    tool_input: Any,
) -> list[dict[str, Any]]:
    details = _workflow_details(tool_name, tool_input)
    if details["workflow_path"] is not None:
        return list(details["workflow_actions"])
    command = _command_text(tool_input)
    return [
        {"step": index, "action": "playwright", "selector": ""}
        for index, _ in enumerate(PLAYWRIGHT_MUTATION.finditer(command), 1)
    ]


def _normalized_input(tool_input: Any) -> str:
    try:
        return json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(tool_input or "")


def interaction_request(tool_name: Any, tool_input: Any) -> dict[str, Any] | None:
    """Describe one browser or system-pointer operation that needs consent."""
    normalized_tool = _tool_name(tool_name)
    command = _command_text(tool_input)
    if _is_patch_payload(command):
        return None
    action = _action_value(tool_input)
    direct = any(marker in normalized_tool for marker in DIRECT_TOOL_MARKERS)
    direct = direct or bool(DIRECT_COMMAND.search(command))
    browser = any(marker in normalized_tool for marker in BROWSER_TOOL_MARKERS)
    browser = (
        browser
        or _invokes_browse_script(command)
        or bool(BROWSER_COMMAND.search(command))
    )
    if direct and action and action not in POINTER_ACTIONS:
        direct = False
    if not direct and not browser:
        return None

    kind = "direct_pointer" if direct else "browser_automation"
    workflow = _workflow_details(tool_name, tool_input)
    described_actions = _described_workflow_actions(tool_name, tool_input)
    interaction_count = (
        1
        if kind == "direct_pointer"
        else max(
            len(described_actions),
            int(workflow["interaction_count"]),
            1 if action in PAGE_INTERACTION_ACTIONS else 0,
        )
    )
    fingerprint_material = "\0".join((
        normalized_tool,
        _normalized_input(tool_input),
        str(workflow.get("workflow_path") or ""),
        str(workflow.get("workflow_sha256") or ""),
    ))
    request_id = hashlib.sha256(
        fingerprint_material.encode("utf-8")
    ).hexdigest()[:24]
    return {
        "kind": kind,
        "mode": interaction_mode(tool_name, tool_input),
        "request_id": request_id,
        "background_allowed": kind != "direct_pointer",
        "interaction_count": interaction_count,
        "actions": described_actions,
        "workflow_path": workflow["workflow_path"],
        "workflow_sha256": workflow["workflow_sha256"],
        "description": (
            "直接操作系统鼠标/桌面指针"
            if direct
            else "浏览器页面操作"
        ),
    }
