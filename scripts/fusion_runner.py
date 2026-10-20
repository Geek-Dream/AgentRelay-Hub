#!/usr/bin/env python3
"""Persistent AgentRelay Fusion sidekick sessions and local tool execution."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:
    from . import fusion_mode
except ImportError:
    import fusion_mode  # type: ignore


MODEL_CALL = Callable[[list[dict[str, Any]], list[dict[str, Any]], int], dict[str, Any]]
MAX_TOOL_OUTPUT = 32000
MAX_READ_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 1024 * 1024
SESSION_MESSAGE_LIMIT = 48
SESSION_CONTENT_LIMIT = 16000
SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
}


class FusionRunnerError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def session_key(workspace: Path, session_id: str = "") -> str:
    workspace_identity = str(workspace.expanduser().resolve())
    session_identity = str(session_id or "").strip()
    identity = (
        f"{workspace_identity}\0{session_identity}"
        if session_identity
        else workspace_identity
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def session_path(
    workspace: Path,
    *,
    session_id: str = "",
    home: Path | None = None,
) -> Path:
    key = session_key(workspace, session_id)
    return fusion_mode.fusion_sessions_directory(home) / f"{key}.json"


@contextmanager
def _session_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    for _attempt in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            break
        except FileExistsError:
            value = _read_json(lock_path)
            try:
                pid = int(value.get("pid") or 0) if value else 0
            except (TypeError, ValueError):
                pid = 0
            if pid > 0 and fusion_mode._pid_alive(pid):
                raise FusionRunnerError(
                    "FUSION_BUSY",
                    "该 Fusion 会话已有任务正在运行",
                    session_file=str(path),
                )
            lock_path.unlink(missing_ok=True)
    if descriptor is None:
        raise FusionRunnerError(
            "FUSION_BUSY",
            "无法获取 Fusion 会话锁",
            session_file=str(path),
        )
    try:
        os.write(
            descriptor,
            json.dumps({"pid": os.getpid(), "created_at": _now()}).encode("utf-8"),
        )
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _system_prompt(tool_mode: str, tool_capable: bool) -> str:
    if not tool_capable:
        return (
            "You are Agent Fusion, a persistent read-only sidekick for a coding agent. "
            "The selected web provider has no local tools. Work only from the task and "
            "context supplied by the main agent. Analyze bulky output, propose patches, "
            "and return a concise concrete result. Never claim to have read, changed, or "
            "executed anything you did not receive in the prompt. Never start Fusion or "
            "Commander recursively."
        )
    permission = (
        "You may inspect files and run read-only repository searches, but you must not "
        "modify files or run shell commands."
        if tool_mode == "readonly"
        else "You may inspect and modify files inside the workspace and run shell commands."
    )
    return (
        "You are Agent Fusion, a persistent sidekick agent for a coding agent. "
        "The main agent owns planning, ambiguity, and final review. You own delegated "
        "execution and bulky investigation. Gather context with your tools instead of "
        "asking the main agent to restate the repository. "
        f"{permission} "
        "Stay inside the workspace, do not expand scope, and report changed files, "
        "commands, evidence, blockers, and any ambiguity you resolved. "
        "Never start another Fusion session and never start Commander mode. "
        "If a Commander lock appears, stop immediately with a handoff summary."
    )


def _new_session(
    *,
    key: str,
    workspace: Path,
    provider_ref: str,
    tool_mode: str,
    tool_capable: bool,
) -> dict[str, Any]:
    return {
        "version": 1,
        "session_id": key,
        "workspace": str(workspace),
        "provider_ref": provider_ref,
        "tool_mode": tool_mode,
        "tool_capable": tool_capable,
        "state": "idle",
        "created_at": _now(),
        "updated_at": _now(),
        "messages": [{
            "role": "system",
            "content": _system_prompt(tool_mode, tool_capable),
        }],
        "seen_commander_results": [],
        "last_result": None,
        "last_handoff": None,
    }


def load_session(
    workspace: Path,
    *,
    session_id: str = "",
    provider_ref: str,
    tool_mode: str,
    tool_capable: bool,
    home: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    key = session_key(workspace, session_id)
    path = session_path(workspace, session_id=session_id, home=home)
    value = _read_json(path)
    reset_reason = ""
    if value is None:
        value = _new_session(
            key=key,
            workspace=workspace,
            provider_ref=provider_ref,
            tool_mode=tool_mode,
            tool_capable=tool_capable,
        )
    elif (
        value.get("provider_ref") != provider_ref
        or value.get("tool_mode") != tool_mode
        or value.get("tool_capable") != tool_capable
        or value.get("workspace") != str(workspace)
    ):
        reset_reason = "provider, workspace, or tool mode changed"
        value = _new_session(
            key=key,
            workspace=workspace,
            provider_ref=provider_ref,
            tool_mode=tool_mode,
            tool_capable=tool_capable,
        )
    value["session_id"] = key
    value["reset_reason"] = reset_reason
    return value, path


def reset_session(
    workspace: Path,
    *,
    session_id: str = "",
    home: Path | None = None,
) -> bool:
    path = session_path(workspace, session_id=session_id, home=home)
    try:
        with _session_lock(path):
            existed = path.is_file()
            path.unlink(missing_ok=True)
            return existed
    except FusionRunnerError:
        raise


def _trim_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return []
    system_messages = [
        item for item in messages if item.get("role") == "system"
    ]
    system = system_messages[:1]
    retained_notices = [
        item for item in system_messages[1:]
        if item.get("kind") in {"commander_result", "commander_handoff"}
    ][-12:]
    system.extend(retained_notices)
    rest = [item for item in messages if item.get("role") != "system"]
    rest = rest[-SESSION_MESSAGE_LIMIT:]
    trimmed: list[dict[str, Any]] = []
    for message in rest:
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, str) and len(content) > SESSION_CONTENT_LIMIT:
            copied["content"] = (
                content[:SESSION_CONTENT_LIMIT]
                + f"\n[... truncated {len(content) - SESSION_CONTENT_LIMIT} characters]"
            )
        trimmed.append(copied)
    return system + trimmed


def _attach_commander_results(
    session: dict[str, Any],
    *,
    home: Path | None,
) -> list[dict[str, Any]]:
    seen = {
        str(item)
        for item in session.get("seen_commander_results", [])
        if isinstance(item, str)
    }
    applied: list[dict[str, Any]] = []
    for result in reversed(fusion_mode.commander_results(
        home=home,
        limit=20,
        session_id=str(session.get("session_id") or ""),
    )):
        identity = f"{result.get('task_id', '')}:{result.get('finished_at', '')}"
        if identity in seen:
            continue
        seen.add(identity)
        applied.append(result)
        session.setdefault("messages", []).append({
            "role": "system",
            "kind": "commander_result",
            "content": (
                "A Commander run completed and took over work that may have belonged "
                f"to Fusion: task_id={result.get('task_id', '')}, "
                f"status={result.get('status', '')}. Treat the interrupted Fusion task "
                "as closed and do not resume it unless the main agent explicitly asks."
            ),
        })
    session["seen_commander_results"] = sorted(seen)[-50:]
    return applied


def _tool_schema(
    name: str,
    description: str,
    properties: Mapping[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": dict(properties),
                "required": list(required or []),
                "additionalProperties": False,
            },
        },
    }


def tools_for_mode(tool_mode: str) -> list[dict[str, Any]]:
    tools = [
        _tool_schema(
            "read_file",
            "Read a UTF-8 text file, optionally limited to a line range.",
            {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        _tool_schema(
            "list_files",
            "List workspace files matching a glob pattern.",
            {
                "path": {"type": "string", "description": "Directory, default '.'."},
                "pattern": {"type": "string", "description": "Glob such as '**/*.py'."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
        ),
        _tool_schema(
            "grep",
            "Search text files with a Python regular expression.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "File or directory, default '.'."},
                "file_glob": {"type": "string", "description": "Optional glob such as '*.py'."},
                "case_sensitive": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["pattern"],
        ),
    ]
    if tool_mode == "coding":
        tools.extend([
            _tool_schema(
                "write_file",
                "Create or replace a UTF-8 text file inside the workspace.",
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                ["path", "content"],
            ),
            _tool_schema(
                "run_command",
                "Run a shell command in the workspace and return bounded output.",
                {
                    "command": {"type": "string"},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 120,
                    },
                },
                ["command"],
            ),
        ])
    return tools


def _safe_path(workspace: Path, raw: str) -> Path:
    root = workspace.resolve()
    text = str(raw or ".").strip() or "."
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FusionRunnerError(
            "FUSION_PATH_OUTSIDE_WORKSPACE",
            f"路径超出工作区：{raw}",
        ) from exc
    return resolved


def _bounded_text(value: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n[... truncated {len(value) - limit} characters]"


def _is_probably_binary(data: bytes) -> bool:
    return b"\0" in data[:4096]


def _read_file(workspace: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    path = _safe_path(workspace, str(arguments.get("path") or ""))
    if not path.is_file():
        return {"ok": False, "error": "file_not_found", "path": str(path)}
    if path.stat().st_size > MAX_READ_BYTES:
        return {
            "ok": False,
            "error": "file_too_large",
            "path": str(path),
            "limit_bytes": MAX_READ_BYTES,
        }
    data = path.read_bytes()
    if _is_probably_binary(data):
        return {"ok": False, "error": "binary_file", "path": str(path)}
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    try:
        start = max(1, int(arguments.get("start_line") or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        end = max(start, int(arguments.get("end_line") or min(len(lines), start + 399)))
    except (TypeError, ValueError):
        end = min(len(lines), start + 399)
    end = min(end, start + 1999, len(lines))
    selected = lines[start - 1:end]
    numbered = "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(selected, start=start)
    )
    return {
        "ok": True,
        "path": str(path.relative_to(workspace.resolve())),
        "start_line": start,
        "end_line": end if selected else start - 1,
        "total_lines": len(lines),
        "content": _bounded_text(numbered),
    }


def _iter_files(root: Path, limit: int = 10000) -> Iterator[Path]:
    yielded = 0
    for directory, names, files in os.walk(root):
        names[:] = [
            name for name in names
            if name not in SKIP_DIRECTORIES and not name.startswith(".agentrelay-")
        ]
        for name in files:
            path = Path(directory) / name
            try:
                if path.is_symlink():
                    continue
            except OSError:
                continue
            yield path
            yielded += 1
            if yielded >= limit:
                return


def _list_files(workspace: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    root = _safe_path(workspace, str(arguments.get("path") or "."))
    if not root.is_dir():
        return {"ok": False, "error": "directory_not_found", "path": str(root)}
    pattern = str(arguments.get("pattern") or "**/*")
    try:
        limit = max(1, min(500, int(arguments.get("limit") or 200)))
    except (TypeError, ValueError):
        limit = 200
    matches: list[str] = []
    for path in _iter_files(root):
        relative = path.relative_to(root).as_posix()
        if fnmatch.fnmatch(relative, pattern):
            matches.append(relative)
            if len(matches) >= limit:
                break
    return {
        "ok": True,
        "path": str(root.relative_to(workspace.resolve())),
        "pattern": pattern,
        "matches": matches,
        "truncated": len(matches) >= limit,
    }


def _grep(workspace: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    pattern = str(arguments.get("pattern") or "")
    if not pattern:
        return {"ok": False, "error": "pattern_empty"}
    flags = 0 if bool(arguments.get("case_sensitive", False)) else re.IGNORECASE
    try:
        expression = re.compile(pattern, flags)
    except re.error as exc:
        return {"ok": False, "error": "invalid_regex", "detail": str(exc)}
    root = _safe_path(workspace, str(arguments.get("path") or "."))
    try:
        limit = max(1, min(500, int(arguments.get("limit") or 200)))
    except (TypeError, ValueError):
        limit = 200
    file_glob = str(arguments.get("file_glob") or "*")
    matches: list[dict[str, Any]] = []
    candidates = [root] if root.is_file() else list(_iter_files(root))
    for path in candidates:
        if not path.is_file() or not fnmatch.fnmatch(path.name, file_glob):
            continue
        try:
            if path.stat().st_size > MAX_READ_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if _is_probably_binary(data):
            continue
        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if expression.search(line):
                matches.append({
                    "path": str(path.relative_to(workspace.resolve())),
                    "line": line_number,
                    "text": line[:500],
                })
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break
    return {
        "ok": True,
        "pattern": pattern,
        "matches": matches,
        "truncated": len(matches) >= limit,
    }


def _write_file(workspace: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    path = _safe_path(workspace, str(arguments.get("path") or ""))
    content = arguments.get("content")
    if not isinstance(content, str):
        return {"ok": False, "error": "content_must_be_string"}
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        return {
            "ok": False,
            "error": "content_too_large",
            "limit_bytes": MAX_WRITE_BYTES,
        }
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.fusion-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "ok": True,
        "path": str(path.relative_to(workspace.resolve())),
        "bytes": len(encoded),
        "created": not existed,
    }


def _run_command(workspace: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
    command = str(arguments.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "command_empty"}
    try:
        timeout = max(1, min(120, int(arguments.get("timeout_seconds") or 60)))
    except (TypeError, ValueError):
        timeout = 60
    child_env = os.environ.copy()
    child_env["AGENTRELAY_FUSION_CHILD"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=child_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": "command_timeout",
            "timeout_seconds": timeout,
            "stdout": _bounded_text(str(exc.stdout or "")),
            "stderr": _bounded_text(str(exc.stderr or "")),
        }
    except OSError as exc:
        return {"ok": False, "error": "command_failed_to_start", "detail": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": _bounded_text(completed.stdout or ""),
        "stderr": _bounded_text(completed.stderr or ""),
    }


def execute_tool(
    name: str,
    arguments: Mapping[str, Any],
    *,
    workspace: Path,
    tool_mode: str,
) -> dict[str, Any]:
    readonly = {
        "read_file": _read_file,
        "list_files": _list_files,
        "grep": _grep,
    }
    coding = {
        "write_file": _write_file,
        "run_command": _run_command,
    }
    handler = readonly.get(name)
    if handler is None and tool_mode == "coding":
        handler = coding.get(name)
    if handler is None:
        return {
            "ok": False,
            "error": "tool_not_allowed",
            "tool": name,
            "tool_mode": tool_mode,
        }
    try:
        return handler(workspace, arguments)
    except FusionRunnerError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message, **exc.extra}
    except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
        return {
            "ok": False,
            "error": "tool_execution_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    if isinstance(raw, dict):
        return raw, ""
    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    if not isinstance(value, dict):
        return {}, "tool arguments must be a JSON object"
    return value, ""


def _normalize_message(value: Mapping[str, Any]) -> dict[str, Any]:
    choices = value.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if isinstance(message, dict):
            value = message
    content = value.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": str(content or ""),
    }
    calls = value.get("tool_calls")
    if isinstance(calls, list) and calls:
        normalized_calls = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            normalized_calls.append({
                "id": str(call.get("id") or f"fusion-call-{index}"),
                "type": "function",
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": (
                        function.get("arguments")
                        if isinstance(function.get("arguments"), str)
                        else json.dumps(function.get("arguments") or {}, ensure_ascii=False)
                    ),
                },
            })
        if normalized_calls:
            message["tool_calls"] = normalized_calls
    return message


def _activity_entry(
    step: int,
    name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "step": step,
        "tool": name,
        "args": dict(arguments),
        "ok": bool(result.get("ok")),
        "path": str(result.get("path") or ""),
        "exit_code": result.get("exit_code"),
        "truncated": bool(result.get("truncated")),
    }


def _write_handoff(
    *,
    session: Mapping[str, Any],
    run: Mapping[str, Any],
    activity: list[dict[str, Any]],
    reason: str,
    home: Path | None,
) -> Path:
    directory = fusion_mode.fusion_runtime_directory(home) / "handoffs"
    path = directory / f"handoff-{run.get('run_id', '')}.json"
    changed_files = sorted({
        str(item.get("path") or "")
        for item in activity
        if item.get("tool") == "write_file" and item.get("ok") and item.get("path")
    })
    commands = [
        item for item in activity
        if item.get("tool") == "run_command"
    ]
    _atomic_json(path, {
        "run_id": str(run.get("run_id") or ""),
        "session_id": str(session.get("session_id") or ""),
        "workspace": str(session.get("workspace") or ""),
        "task": str(run.get("task") or ""),
        "reason": reason,
        "phase": str(run.get("phase") or ""),
        "tool_mode": str(session.get("tool_mode") or ""),
        "changed_files": changed_files,
        "commands": commands[-10:],
        "recent_activity": activity[-20:],
        "created_at": _now(),
    })
    return path


def _check_state(
    *,
    home: Path | None,
    lock_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    state = fusion_mode.fusion_state(
        home=home,
        lock_dir=lock_dir,
        environ=environ,
    )
    if not state.get("delegable"):
        raise fusion_mode.FusionPreempted(str(state.get("reason") or ""), state)
    return state


def _check_run(
    run_id: str,
    *,
    home: Path | None,
    lock_dir: Path | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    if fusion_mode.fusion_run_interrupt_requested(run_id, home=home):
        raise fusion_mode.FusionPreempted(
            "COMMANDER_ACTIVE",
            fusion_mode.fusion_state(home=home, lock_dir=lock_dir, environ=environ),
        )
    return _check_state(home=home, lock_dir=lock_dir, environ=environ)


def run_sidekick(
    prompt: str,
    *,
    context: str = "",
    workspace: Path,
    session_id: str = "",
    provider_ref: str,
    provider_kind: str,
    tool_mode: str,
    max_steps: int,
    timeout: int,
    home: Path | None,
    lock_dir: Path | None,
    environ: Mapping[str, str] | None,
    call_model: MODEL_CALL,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise FusionRunnerError(
            "FUSION_WORKSPACE_NOT_FOUND",
            f"Fusion 工作区不存在：{workspace}",
        )
    tool_capable = provider_kind in {"local", "api"}
    if tool_mode == "coding" and not tool_capable:
        raise FusionRunnerError(
            "FUSION_TOOL_MODE_UNSUPPORTED",
            "coding 模式需要支持工具调用的 local 或 api Provider",
            provider=provider_ref,
        )
    key = session_key(workspace, session_id)
    path = session_path(workspace, session_id=session_id, home=home)
    with _session_lock(path):
        session, path = load_session(
            workspace,
            session_id=session_id,
            provider_ref=provider_ref,
            tool_mode=tool_mode,
            tool_capable=tool_capable,
            home=home,
        )
        applied_results = _attach_commander_results(session, home=home)
        if context.strip():
            task_text = prompt.strip() + "\n\n## Context from the main agent\n" + context.strip()
        else:
            task_text = prompt.strip()
        session.setdefault("messages", []).append({
            "role": "user",
            "content": task_text,
        })
        session["messages"] = _trim_messages(session["messages"])
        session["state"] = "running"
        session["updated_at"] = _now()
        _atomic_json(path, session)

        run = fusion_mode.start_fusion_run(
            session_id=key,
            workspace=workspace,
            task=prompt,
            tool_mode=tool_mode,
            home=home,
        )
        run_id = str(run["run_id"])
        tools = tools_for_mode(tool_mode) if tool_capable else []
        activity: list[dict[str, Any]] = []
        final_answer = ""
        steps_used = 0
        try:
            for step in range(1, max(1, min(20, int(max_steps))) + 1):
                steps_used = step
                _check_run(
                    run_id,
                    home=home,
                    lock_dir=lock_dir,
                    environ=environ,
                )
                fusion_mode.update_fusion_run(
                    run_id,
                    home=home,
                    phase="model",
                    step=step,
                )
                response = call_model(session["messages"], tools, timeout)
                _check_run(
                    run_id,
                    home=home,
                    lock_dir=lock_dir,
                    environ=environ,
                )
                message = _normalize_message(response)
                session["messages"].append(message)
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    final_answer = str(message.get("content") or "").strip()
                    if not final_answer:
                        raise FusionRunnerError(
                            "FUSION_EMPTY_ANSWER",
                            "Fusion Provider 返回了空回答",
                        )
                    break
                for call in tool_calls:
                    _check_run(
                        run_id,
                        home=home,
                        lock_dir=lock_dir,
                        environ=environ,
                    )
                    function = call.get("function") if isinstance(call, dict) else {}
                    name = str(function.get("name") or "")
                    arguments, parse_error = _parse_arguments(function.get("arguments"))
                    fusion_mode.update_fusion_run(
                        run_id,
                        home=home,
                        phase=f"tool:{name}",
                        step=step,
                    )
                    if parse_error:
                        result = {
                            "ok": False,
                            "error": "invalid_tool_arguments",
                            "detail": parse_error,
                        }
                    else:
                        with fusion_mode.fusion_execution_gate(
                            home=home,
                            lock_dir=lock_dir,
                            environ=environ,
                        ):
                            result = execute_tool(
                                name,
                                arguments,
                                workspace=workspace,
                                tool_mode=tool_mode,
                            )
                    activity.append(_activity_entry(step, name, arguments, result))
                    session["messages"].append({
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": _bounded_text(
                            json.dumps(result, ensure_ascii=False, sort_keys=True)
                        ),
                    })
                    fusion_mode.update_fusion_run(
                        run_id,
                        home=home,
                        phase="tool_complete",
                        step=step,
                        recent_activity=activity[-10:],
                    )
                session["messages"] = _trim_messages(session["messages"])
                _atomic_json(path, session)
            else:
                raise FusionRunnerError(
                    "FUSION_STEP_LIMIT",
                    "Fusion 在限定步数内没有完成",
                    max_steps=max_steps,
                    recent_activity=activity[-10:],
                )
        except fusion_mode.FusionPreempted as exc:
            phase = "commander_preempt" if exc.reason in {
                "COMMANDER_ACTIVE",
                "COMMANDER_CHILD",
                "FUSION_CHILD",
            } else "interrupted"
            handoff = _write_handoff(
                session=session,
                run={**run, "phase": phase},
                activity=activity,
                reason=exc.reason,
                home=home,
            )
            session["state"] = (
                "commander_handoff"
                if exc.reason in {"COMMANDER_ACTIVE", "COMMANDER_CHILD"}
                else "idle"
            )
            session["last_handoff"] = {
                "reason": exc.reason,
                "handoff_path": str(handoff),
                "created_at": _now(),
            }
            session["messages"].append({
                "role": "system",
                "kind": "commander_handoff",
                "content": (
                    "The previous task was interrupted because Commander has priority. "
                    "Treat that task as handed off and closed. Wait for a new explicit "
                    "task from the main agent."
                ),
            })
            session["updated_at"] = _now()
            _atomic_json(path, session)
            return {
                "status": "rejected",
                "error": exc.reason,
                "message": "Commander 已抢占 Fusion，当前任务已交接并关闭",
                "session_id": key,
                "tool_mode": tool_mode,
                "steps": steps_used,
                "handoff_path": str(handoff),
                "state": exc.state,
            }
        except Exception:
            session["state"] = "idle"
            session["updated_at"] = _now()
            _atomic_json(path, session)
            raise
        else:
            session["state"] = "idle"
            session["updated_at"] = _now()
            session["last_result"] = {
                "answer": final_answer,
                "steps": steps_used,
                "finished_at": _now(),
            }
            session["messages"] = _trim_messages(session["messages"])
            _atomic_json(path, session)
            return {
                "status": "success",
                "answer": final_answer,
                "session_id": key,
                "session_file": str(path),
                "persistent": True,
                "tool_mode": tool_mode,
                "read_only": tool_mode == "readonly",
                "tool_capable": tool_capable,
                "steps": steps_used,
                "tools_used": activity,
                "commander_results_applied": applied_results,
            }
        finally:
            fusion_mode.finish_fusion_run(run_id, home=home)
