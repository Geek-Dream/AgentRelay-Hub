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
    r"\bpyautogui\.(?:click|doubleClick|drag|moveTo|scroll|typewrite|press|hotkey)\s*\(|"
    r"\bmouse_event\b|"
    r"\bSendInput\s*\(|"
    r"\bSendKeys\b"
    r")",
    re.I,
)
# Commands whose arguments are data, never work. A grep/echo that merely mentions
# cliclick must not be treated as a system-pointer action.
READONLY_TEXT_COMMANDS = {
    "ag", "awk", "cat", "comm", "cut", "diff", "echo", "fgrep", "file",
    "find", "grep", "head", "jq", "less", "ls", "more", "printf", "rg",
    "sed", "sort", "stat", "tail", "tr", "uniq", "wc",
}
SHELL_WRAPPERS = {
    "builtin", "command", "env", "exec", "nice", "sudo", "time",
}
BACKGROUND_WRAPPERS = {"nohup", "setsid"}
POINTER_EXECUTABLES = {"cliclick", "xdotool", "ydotool"}
OSASCRIPT_POINTER = re.compile(
    r"System Events.*(?:click|keystroke|key code|key down|key up)",
    re.I | re.S,
)
SHELL_SPLIT = re.compile(r"(?:&&|\|\||[;|\n]|(?<![&])&(?![&]))")
TRAILING_BACKGROUND = re.compile(r"(?<![&])&(?![&])\s*$")
HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
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


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: their text is data for a reader, not work."""
    if "<<" not in command:
        return command
    output: list[str] = []
    lines = command.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        output.append(line)
        index += 1
        for match in HEREDOC_START.finditer(line):
            terminator = match.group(2)
            while index < len(lines):
                candidate = lines[index].strip()
                index += 1
                if candidate == terminator:
                    break
    return "\n".join(output)


def _strip_comments(command: str) -> str:
    """Remove whole-line shell comments so commented examples are not acted on."""
    kept = [
        line for line in command.split("\n")
        if line.lstrip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(kept)


def _segments(command: str) -> list[str]:
    """Split a shell command into segments, ignoring quoted && ; | inside quotes."""
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    text = command
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            index += 1
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if char in ";|\n":
            segments.append("".join(current))
            current = []
            index += 1
            continue
        if char == "&":
            # `>&`, `2>&1`, `<&`, `&>` are redirections, not background jobs.
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if (
                previous in "><"
                or following in {">", "-"}
                or following.isdigit()
            ):
                current.append(char)
                index += 1
                continue
            # Keep the marker on the preceding segment so background detection
            # still sees `cmd &`, then start the next segment.
            segments.append("".join(current).rstrip() + " &")
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    segments.append("".join(current))
    return [item.strip() for item in segments if item.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=os.name != "nt")
    except ValueError:
        return segment.split()


def _leading_command(segment: str) -> tuple[str, list[str], bool]:
    """Return (executable name, argv, backgrounded) for one segment."""
    backgrounded = bool(TRAILING_BACKGROUND.search(segment))
    if backgrounded:
        segment = TRAILING_BACKGROUND.sub("", segment).strip()
    words = _tokens(segment)
    while words:
        first = words[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", first):
            words.pop(0)
            continue
        name = Path(first.strip("\"'")).name.lower()
        if name in BACKGROUND_WRAPPERS:
            return "", [], True
        if name in SHELL_WRAPPERS:
            words.pop(0)
            while words and words[0].startswith("-"):
                words.pop(0)
            continue
        if name in {"py", "python", "python3", "python.exe", "python3.exe"}:
            rest = words[1:]
            if rest[:1] == ["-c"]:
                return "python-c", rest[1:2], backgrounded
            return "python", rest, backgrounded
        if name in {"sh", "bash", "zsh", "pwsh", "powershell"}:
            rest = words[1:]
            # `-c`, `-lc`, `-ec`, `--command` all mean "run this string".
            for index, word in enumerate(rest):
                lowered = word.lower()
                is_command_flag = (
                    lowered in {"--command", "-command"}
                    or (
                        lowered.startswith("-")
                        and not lowered.startswith("--")
                        and "c" in lowered[1:]
                        and lowered[1:].isalpha()
                    )
                )
                if is_command_flag and index + 1 < len(rest):
                    return "shell-c", rest[index + 1:index + 2], backgrounded
            return "shell", rest, backgrounded
        return name, words[1:], backgrounded
    return "", [], backgrounded


def _readonly_text_segment(segment: str) -> bool:
    name, _, _ = _leading_command(segment)
    return name in READONLY_TEXT_COMMANDS


def _data_first_payloads(command: str) -> list[str]:
    """Return only the strings a shell would truly execute."""
    cleaned = _strip_comments(_strip_heredocs(command))
    payloads: list[str] = []
    for segment in _segments(cleaned):
        name, argv, _ = _leading_command(segment)
        if name in {"python-c", "shell-c"} and argv:
            payloads.append(argv[0])
        elif name in READONLY_TEXT_COMMANDS:
            continue
        else:
            payloads.append(segment)
    return payloads


def _executes(segment: str, executable: str) -> bool:
    name, _, _ = _leading_command(segment)
    return name == executable


def _pointer_invocation(command: str) -> bool:
    """True only when a pointer executable/osascript runs as a real command."""
    cleaned = _strip_comments(_strip_heredocs(command))
    for segment in _segments(cleaned):
        name, argv, _ = _leading_command(segment)
        if name in POINTER_EXECUTABLES:
            return True
        if name == "osascript":
            if OSASCRIPT_POINTER.search(segment):
                return True
        if name in {"python-c", "shell-c"} and argv:
            inner = argv[0]
            if DIRECT_COMMAND.search(inner):
                return True
            if any(
                _executes(item, pointer)
                for item in _segments(_strip_comments(_strip_heredocs(inner)))
                for pointer in POINTER_EXECUTABLES
            ):
                return True
    return False


def _app_launch(command: str) -> dict[str, Any] | None:
    """Describe an app/window launch that may steal focus or linger."""
    cleaned = _strip_comments(_strip_heredocs(command))
    for segment in _segments(cleaned):
        name, argv, backgrounded = _leading_command(segment)
        if name in {"python-c", "shell-c"} and argv:
            # `sh -c 'open -a Slack'` is still an app launch: classify what
            # the inner shell would actually run.
            inner = _app_launch(argv[0])
            if inner is not None:
                return inner
            continue
        if name not in {"open", "xdg-open", "start", "cmd"}:
            continue
        if name == "cmd" and not argv[:1] == ["/c"]:
            continue
        if name == "cmd":
            argv = argv[1:]
            if argv[:1] and argv[0].lower() == "start":
                name, argv = "start", argv[1:]
            else:
                continue
        background = backgrounded
        target = ""
        for word in argv:
            lowered = word.lower()
            if lowered in {"-g", "--background", "--no-focus"}:
                background = True
                continue
            if lowered in {"/b", "/background"}:
                background = True
                continue
            if lowered in {"-a", "-b", "--args", "-n"}:
                continue
            if word.startswith("-"):
                continue
            if not target:
                target = word
        if name == "start" and target and target.startswith(("http://", "https://")):
            background = False
        return {
            "command": name,
            "target": target[:200],
            "background": background,
            "affects_user": not background,
        }
    return None


def _invokes_browse_script(command: str) -> bool:
    """Match an actual browse.py invocation, not grep/cat/edit text."""
    if "browse.py" not in command:
        return False
    cleaned = _strip_comments(_strip_heredocs(command))
    for segment in _segments(cleaned):
        name, argv, _ = _leading_command(segment)
        if name == "browse.py":
            return True
        candidates = [name, *argv]
        for index, token in enumerate(candidates):
            if Path(str(token).strip("\"'")).name != "browse.py":
                continue
            if index == 0:
                return True
            previous = Path(str(candidates[index - 1]).strip("\"'")).name.lower()
            if previous == "py" or previous.startswith("python"):
                return True
    return False


def _help_only_browse_invocation(command: str) -> bool:
    """Argparse help exits before Playwright starts; quoted help text is data."""
    found = False
    cleaned = _strip_comments(_strip_heredocs(command))
    for segment in _segments(cleaned):
        name, argv, _ = _leading_command(segment)
        if name == "browse.py":
            arguments = argv
        elif name == "python":
            browse_index = next((
                index for index, token in enumerate(argv)
                if Path(str(token).strip("\"'")).name == "browse.py"
            ), None)
            if browse_index is None:
                continue
            arguments = argv[browse_index + 1:]
        else:
            continue
        found = True
        if not any(token in {"-h", "--help"} for token in arguments):
            return False
    return found


def _background_process(command: str) -> bool:
    """A detached process that keeps running after the tool call returns."""
    cleaned = _strip_comments(_strip_heredocs(command))
    if "nohup" in cleaned or "setsid" in cleaned:
        return True
    for segment in _segments(cleaned):
        name, argv, backgrounded = _leading_command(segment)
        if name in {"python-c", "shell-c"} and argv:
            # `bash -c 'python app.py &'` still leaves a process running.
            if _background_process(argv[0]):
                return True
            continue
        if backgrounded and name not in {"", "open", "xdg-open"}:
            return True
        if TRAILING_BACKGROUND.search(segment):
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


def _mode_only_retry_input(tool_name: Any, tool_input: Any) -> Any:
    """Remove only display-mode syntax; preserve the operation byte for byte."""
    normalized_name = _tool_name(tool_name)
    if any(marker in normalized_name for marker in DIRECT_TOOL_MARKERS):
        return tool_input
    if isinstance(tool_input, dict):
        normalized = dict(tool_input)
        for key in ("interaction_mode", "execution_mode", "browser_mode", "display_mode"):
            if _mode_from_value(normalized.get(key)):
                normalized[key] = ""
        for key in ("foreground", "headless"):
            if isinstance(normalized.get(key), bool):
                normalized.pop(key)
        for key in (
            "command", "cmd", "script", "shell_command", "shellCommand",
        ):
            value = normalized.get(key)
            if isinstance(value, str) and value.strip():
                normalized[key] = _mode_only_retry_command(value)
        argv = normalized.get("argv")
        if isinstance(argv, (list, tuple)):
            normalized["argv"] = [
                _mode_only_retry_command(str(item)) for item in argv
            ]
        return normalized
    return _mode_only_retry_command(_command_text(tool_input))


def _mode_only_retry_command(command: str) -> str:
    """Strip actual CLI flags, never matching text inside quoted arguments."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return command
    stripped = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--interaction-mode" and index + 1 < len(tokens):
            if tokens[index + 1].lower() in MODES:
                index += 2
                continue
        if token.startswith("--interaction-mode="):
            if token.split("=", 1)[1].lower() in MODES:
                index += 1
                continue
        match = re.fullmatch(
            r"(AGENT_RELAY_WEB_INTERACTION_MODE\s*=\s*)(foreground|background)",
            token, flags=re.I,
        )
        stripped.append(match.group(1) if match else token)
        index += 1
    return " ".join(shlex.quote(token) for token in stripped)


def interaction_request(tool_name: Any, tool_input: Any) -> dict[str, Any] | None:
    """Describe one automation operation that needs consent and prior notice."""
    normalized_tool = _tool_name(tool_name)
    command = _command_text(tool_input)
    if _is_patch_payload(command) or _help_only_browse_invocation(command):
        return None
    action = _action_value(tool_input)
    direct = any(marker in normalized_tool for marker in DIRECT_TOOL_MARKERS)
    direct = direct or _pointer_invocation(command)
    browser = any(marker in normalized_tool for marker in BROWSER_TOOL_MARKERS)
    browser = (
        browser
        or _invokes_browse_script(command)
        or bool(BROWSER_COMMAND.search(command))
    )
    if direct and action and action not in POINTER_ACTIONS:
        direct = False
    launch = None if direct or browser else _app_launch(command)
    background = (
        False
        if direct or browser or launch is not None
        else _background_process(command)
    )
    if not direct and not browser and launch is None and not background:
        return None

    if direct:
        kind = "direct_pointer"
    elif browser:
        kind = "browser_automation"
    elif launch is not None:
        kind = "foreground_window" if launch["affects_user"] else "background_window"
    else:
        kind = "background_process"
    workflow = _workflow_details(tool_name, tool_input)
    described_actions = _described_workflow_actions(tool_name, tool_input)
    interaction_count = (
        1
        if kind != "browser_automation"
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
    retry_fingerprint_material = "\0".join((
        normalized_tool,
        _normalized_input(_mode_only_retry_input(tool_name, tool_input)),
        str(workflow.get("workflow_path") or ""),
        str(workflow.get("workflow_sha256") or ""),
    ))
    request = {
        "kind": kind,
        "mode": interaction_mode(tool_name, tool_input),
        "request_id": request_id,
        "retry_request_id": hashlib.sha256(
            retry_fingerprint_material.encode("utf-8")
        ).hexdigest()[:24],
        "background_allowed": kind != "direct_pointer",
        "interaction_count": interaction_count,
        "actions": described_actions,
        "workflow_path": workflow["workflow_path"],
        "workflow_sha256": workflow["workflow_sha256"],
        "target": (launch or {}).get("target", ""),
        "affects_user": (
            True if kind == "direct_pointer"
            else bool((launch or {}).get("affects_user", False))
        ),
        "description": DESCRIPTIONS[kind],
    }
    request["notice"] = notice_text(request)
    return request


DESCRIPTIONS = {
    "direct_pointer": "直接操作系统鼠标/键盘",
    "browser_automation": "浏览器页面操作",
    "foreground_window": "打开窗口，会抢走你的前台焦点",
    "background_window": "打开窗口但不抢焦点",
    "background_process": "在后台常驻运行，会一直占用资源",
}


def notice_text(request: dict[str, Any]) -> str:
    """Plain, non-alarmist prior notice the user must see before execution."""
    kind = str(request.get("kind") or "")
    target = str(request.get("target") or "").strip()
    named = f"「{target}」" if target else "程序"
    if kind == "direct_pointer":
        return "即将接管系统鼠标或键盘，执行期间你的鼠标会被占用。"
    if kind == "browser_automation":
        return "即将启动浏览器自动化并操作页面。"
    if kind == "foreground_window":
        return f"即将打开{named}，窗口会跳到前台并可能打断你当前操作。"
    if kind == "background_window":
        return f"即将在后台打开{named}，不会抢焦点。"
    if kind == "background_process":
        return "即将在后台常驻运行一个进程，它会一直占用资源直到被关闭。"
    return "即将执行自动化操作。"
