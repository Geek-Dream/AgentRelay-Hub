"""AgentRelay Fusion configuration, local state, and Commander coordination."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


SCRIPT_DIR = Path(__file__).resolve().parent
_RELAY_SCRIPT_DIR = SCRIPT_DIR.parent.parent / "agent-relay" / "scripts"
if _RELAY_SCRIPT_DIR.is_dir() and str(_RELAY_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_RELAY_SCRIPT_DIR))

try:
    from . import config_manager  # noqa: E402
except ImportError:
    import config_manager  # noqa: E402


FUSION_DEFAULTS = {
    "enabled": True,
    "provider_id": "",
    "timeout": 30,
    "tool_mode": "readonly",
    "max_steps": 8,
}
PROVIDER_KINDS = ("web", "local", "api")
TOOL_MODES = ("readonly", "coding")
TAKEOVER_OPEN_STATES = {"pending", "assigned"}
TAKEOVER_CLOSED_STATES = {"completed", "failed"}


def normalize_fusion_config(value: object) -> dict[str, Any]:
    """Return a stable Fusion config while preserving forward-compatible keys."""
    raw = dict(value) if isinstance(value, dict) else {}
    try:
        timeout = int(raw.get("timeout", FUSION_DEFAULTS["timeout"]) or 30)
    except (TypeError, ValueError):
        timeout = int(FUSION_DEFAULTS["timeout"])
    try:
        max_steps = int(raw.get("max_steps", FUSION_DEFAULTS["max_steps"]) or 8)
    except (TypeError, ValueError):
        max_steps = int(FUSION_DEFAULTS["max_steps"])
    tool_mode = str(raw.get("tool_mode") or "readonly").strip().lower()
    if tool_mode not in TOOL_MODES:
        tool_mode = "readonly"
    return {
        **raw,
        "enabled": bool(raw.get("enabled", True)),
        "provider_id": str(raw.get("provider_id") or "").strip(),
        "timeout": max(1, min(120, timeout)),
        "tool_mode": tool_mode,
        "max_steps": max(1, min(20, max_steps)),
    }


def fusion_runtime_directory(home: Path | None = None) -> Path:
    base = config_manager.codex_home() if home is None else home.expanduser()
    return base / "skills" / "agent-relay" / "runtime" / "fusion"


def fusion_sessions_directory(home: Path | None = None) -> Path:
    return fusion_runtime_directory(home) / "sessions"


def fusion_runs_directory(home: Path | None = None) -> Path:
    return fusion_runtime_directory(home) / "runs"


def fusion_results_directory(home: Path | None = None) -> Path:
    return fusion_runtime_directory(home) / "commander-results"


def fusion_handoffs_directory(home: Path | None = None) -> Path:
    return fusion_runtime_directory(home) / "handoffs"


def fusion_takeovers_directory(home: Path | None = None) -> Path:
    return fusion_runtime_directory(home) / "takeovers"


def _provider_entries(config: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for kind, field in (
        ("web", "web_providers"),
        ("local", "local_providers"),
        ("api", "api_providers"),
    ):
        items = config.get(field)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip()
            if not provider_id:
                continue
            yield {
                "kind": kind,
                "id": provider_id,
                "ref": f"{kind}:{provider_id}",
                "entry": item,
            }


def _web_id_candidates(provider_id: str) -> tuple[str, ...]:
    value = provider_id.strip()
    if not value:
        return ()
    if value.endswith("-web"):
        return (value, value.removesuffix("-web"))
    return (value, f"{value}-web")


def resolve_fusion_provider(
    config: Mapping[str, Any],
    reference: str,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve an exact Provider ID without consulting display names or defaults."""
    requested = str(reference or "").strip()
    if not requested:
        return None, "FUSION_PROVIDER_NOT_CONFIGURED"

    entries = list(_provider_entries(config))
    prefix, separator, remainder = requested.partition(":")
    kind = prefix.lower() if separator and prefix.lower() in PROVIDER_KINDS else ""
    provider_id = remainder.strip() if kind else requested
    if not provider_id:
        return None, "FUSION_PROVIDER_NOT_CONFIGURED"

    if kind:
        candidates = _web_id_candidates(provider_id) if kind == "web" else (provider_id,)
        for candidate in candidates:
            for item in entries:
                if item["kind"] == kind and item["id"] == candidate:
                    return item, ""
        return None, "FUSION_PROVIDER_NOT_FOUND"

    exact = [item for item in entries if item["id"] == provider_id]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, "FUSION_PROVIDER_AMBIGUOUS"

    web_candidates = set(_web_id_candidates(provider_id))
    web_matches = [item for item in entries
                   if item["kind"] == "web" and item["id"] in web_candidates]
    if len(web_matches) == 1:
        return web_matches[0], ""
    return None, "FUSION_PROVIDER_NOT_FOUND"


def provider_is_ready(provider: dict[str, Any]) -> bool:
    entry = provider.get("entry") if isinstance(provider.get("entry"), dict) else {}
    if not bool(entry.get("enabled", True)):
        return False
    if provider.get("kind") == "web":
        return bool(str(entry.get("base_url") or entry.get("url") or "").strip())
    return bool(
        str(entry.get("endpoint") or "").strip()
        and str(entry.get("model") or "").strip()
    )


def public_provider(provider: dict[str, Any] | None) -> dict[str, str] | None:
    if not provider:
        return None
    return {
        "kind": str(provider.get("kind") or ""),
        "id": str(provider.get("id") or ""),
        "ref": str(provider.get("ref") or ""),
    }


def available_providers(config: Mapping[str, Any]) -> list[dict[str, str]]:
    """List locally configured Provider references without probing them."""
    return [
        public_provider(provider)
        for provider in _provider_entries(config)
        if public_provider(provider) is not None
    ]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Use a Win32 process handle because os.kill(pid, 0) is not a liveness probe."""
    try:
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return False
        try:
            return True
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _active_commander_locks(lock_dir: Path) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    try:
        paths = list(lock_dir.glob("commander-*.json"))
    except OSError:
        return active
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            pid = int(value.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if _pid_alive(pid):
            active.append({
                "task_id": str(value.get("task_id") or ""),
                "pid": pid,
            })
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return active


def commander_activity(
    *,
    home: Path | None = None,
    lock_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    if str(env.get("AGENTRELAY_FUSION_CHILD", "")).strip() == "1":
        return {"active": True, "reason": "FUSION_CHILD", "locks": []}
    if str(env.get("AGENTRELAY_COMMANDER_CHILD", "")).strip() == "1":
        return {"active": True, "reason": "COMMANDER_CHILD", "locks": []}
    directory = lock_dir or (fusion_runtime_directory(home) / "locks")
    locks = _active_commander_locks(Path(directory))
    return {
        "active": bool(locks),
        "reason": "COMMANDER_ACTIVE" if locks else "",
        "locks": locks,
    }


def fusion_state(
    *,
    config: Mapping[str, Any] | None = None,
    home: Path | None = None,
    lock_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate Fusion using local state only; this function never opens a network connection."""
    loaded = config if config is not None else config_manager.load_config(home)
    fusion = normalize_fusion_config(loaded.get("fusion"))
    activity = commander_activity(home=home, lock_dir=lock_dir, environ=environ)
    active_runs = active_fusion_runs(home=home)
    state: dict[str, Any] = {
        "enabled": fusion["enabled"],
        "requested_enabled": fusion["enabled"],
        "effective_enabled": fusion["enabled"] and not activity["active"],
        "provider_id": fusion["provider_id"],
        "timeout": fusion["timeout"],
        "tool_mode": fusion["tool_mode"],
        "max_steps": fusion["max_steps"],
        "commander_active": activity["active"],
        "commander_reason": activity["reason"],
        "active_commander_tasks": activity["locks"],
        "active_runs": active_runs,
        "available_providers": available_providers(loaded),
        "provider": None,
        "tool_capable": False,
        "delegable": False,
        "reason": "",
    }
    if activity["active"]:
        state["reason"] = activity["reason"]
        return state
    if not fusion["enabled"]:
        state["reason"] = "FUSION_DISABLED"
        return state
    if not fusion["provider_id"]:
        state["reason"] = "FUSION_PROVIDER_NOT_CONFIGURED"
        return state

    provider, error = resolve_fusion_provider(loaded, fusion["provider_id"])
    if provider is None:
        state["reason"] = error
        return state
    state["provider"] = public_provider(provider)
    state["tool_capable"] = provider["kind"] in {"local", "api"}
    if fusion["tool_mode"] == "coding" and not state["tool_capable"]:
        state["reason"] = "FUSION_TOOL_MODE_UNSUPPORTED"
        return state
    entry = provider["entry"]
    if not bool(entry.get("enabled", True)):
        state["reason"] = "FUSION_PROVIDER_DISABLED"
        return state
    if not provider_is_ready(provider):
        state["reason"] = "FUSION_PROVIDER_NOT_READY"
        return state
    state["delegable"] = True
    state["reason"] = "READY"
    return state


def _write_lock_marker(directory: Path, task_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    identity = f"{task_id}:{os.getpid()}:{time.time_ns()}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:20]
    target = directory / f"commander-{suffix}.json"
    payload = {
        "task_id": task_id,
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return target


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


def active_fusion_runs(
    *,
    home: Path | None = None,
    run_dir: Path | None = None,
) -> list[dict[str, Any]]:
    directory = run_dir or fusion_runs_directory(home)
    active: list[dict[str, Any]] = []
    try:
        paths = list(directory.glob("fusion-*.json"))
    except OSError:
        return active
    for path in paths:
        value = _read_json(path)
        if not value:
            path.unlink(missing_ok=True)
            continue
        try:
            pid = int(value.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and _pid_alive(pid):
            active.append({**value, "marker_path": str(path)})
            continue
        path.unlink(missing_ok=True)
    return active


def start_fusion_run(
    *,
    session_id: str,
    workspace: Path,
    task: str,
    tool_mode: str,
    home: Path | None = None,
) -> dict[str, Any]:
    identity = f"{session_id}:{os.getpid()}:{time.time_ns()}".encode("utf-8")
    run_id = hashlib.sha256(identity).hexdigest()[:24]
    payload = {
        "run_id": run_id,
        "session_id": session_id,
        "pid": os.getpid(),
        "workspace": str(workspace),
        "task": str(task)[:4000],
        "tool_mode": str(tool_mode),
        "phase": "starting",
        "interrupt_requested": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = fusion_runs_directory(home) / f"fusion-{run_id}.json"
    _atomic_json(path, payload)
    return {**payload, "marker_path": str(path)}


def update_fusion_run(
    run_id: str,
    *,
    home: Path | None = None,
    **changes: Any,
) -> dict[str, Any] | None:
    path = fusion_runs_directory(home) / f"fusion-{run_id}.json"
    payload = _read_json(path)
    if payload is None:
        return None
    payload.update(changes)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(path, payload)
    return {**payload, "marker_path": str(path)}


def finish_fusion_run(run_id: str, *, home: Path | None = None) -> None:
    path = fusion_runs_directory(home) / f"fusion-{run_id}.json"
    path.unlink(missing_ok=True)


def _handoff_arguments(arguments: object, limit: int = 2000) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"raw": arguments[:limit]}
    if not isinstance(arguments, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            normalized[str(key)] = value[:limit]
        elif isinstance(value, (int, float, bool)) or value is None:
            normalized[str(key)] = value
        else:
            normalized[str(key)] = json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            )[:limit]
    return normalized


def _activity_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "step": item.get("step"),
        "tool": str(item.get("tool") or ""),
        "args": _handoff_arguments(item.get("args")),
        "ok": bool(item.get("ok")),
        "path": str(item.get("path") or ""),
        "exit_code": item.get("exit_code"),
        "truncated": bool(item.get("truncated")),
    }


def build_fusion_handoff(
    *,
    session: Mapping[str, Any],
    run: Mapping[str, Any],
    activity: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    """Build a deterministic handoff from recorded Fusion progress.

    This does not ask the provider for another answer after Commander takes
    the lock. The summary is derived only from persisted messages and tool
    activity, so it cannot block on a model call or invent unobserved work.
    """
    normalized_activity = [_activity_summary(item) for item in activity[-40:]]
    messages = session.get("messages")
    message_items = [
        item for item in messages if isinstance(item, dict)
    ] if isinstance(messages, list) else []
    last_assistant = next(
        (
            item for item in reversed(message_items)
            if item.get("role") == "assistant"
        ),
        {},
    )
    pending_tool_calls = []
    calls = last_assistant.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else {}
            if not isinstance(function, dict):
                continue
            pending_tool_calls.append({
                "name": str(function.get("name") or ""),
                "arguments": _handoff_arguments(function.get("arguments")),
            })

    completed_work = []
    failed_work = []
    for item in normalized_activity:
        description = {
            "step": item["step"],
            "tool": item["tool"],
            "path": item["path"],
            "exit_code": item["exit_code"],
        }
        if item["ok"]:
            completed_work.append(description)
        else:
            failed_work.append(description)

    changed_files = sorted({
        item["path"]
        for item in normalized_activity
        if item["tool"] == "write_file" and item["ok"] and item["path"]
    })
    commands = [
        item for item in normalized_activity
        if item["tool"] == "run_command"
    ][-10:]
    remaining_work = [
        {
            "type": "pending_tool_call",
            "tool": item["name"],
            "arguments": item["arguments"],
        }
        for item in pending_tool_calls
    ]
    if not remaining_work:
        phase = str(run.get("phase") or "")
        if phase.startswith("tool:"):
            remaining_work.append({
                "type": "verify_or_resume_tool",
                "tool": phase.partition(":")[2],
                "arguments": {},
            })
        elif phase == "model":
            remaining_work.append({
                "type": "review_inflight_model_step",
                "tool": "",
                "arguments": {},
            })
        else:
            remaining_work.append({
                "type": "complete_original_task",
                "tool": "",
                "arguments": {},
            })

    risks = []
    if failed_work:
        risks.append("Fusion 有失败的工具步骤，接管者必须核对是否产生部分改动。")
    if changed_files:
        risks.append("Fusion 已改动文件，接管者必须检查差异并重新验证。")
    if str(session.get("tool_mode") or run.get("tool_mode") or "") == "coding":
        risks.append("该任务启用了 coding 模式，不能仅凭文字报告认定完成。")

    original_task = str(
        run.get("task")
        or next(
            (
                item.get("content")
                for item in reversed(message_items)
                if item.get("role") == "user"
            ),
            "",
        )
        or ""
    )
    identity = (
        f"{run.get('run_id', '')}:{reason}:{run.get('updated_at', '')}"
    ).encode("utf-8")
    return {
        "version": 1,
        "handoff_id": hashlib.sha256(identity).hexdigest()[:20],
        "run_id": str(run.get("run_id") or ""),
        "session_id": str(
            run.get("session_id") or session.get("session_id") or ""
        ),
        "workspace": str(
            run.get("workspace") or session.get("workspace") or ""
        ),
        "original_task": original_task[:4000],
        "task": original_task[:4000],
        "reason": str(reason),
        "phase": str(run.get("phase") or ""),
        "tool_mode": str(
            session.get("tool_mode") or run.get("tool_mode") or "readonly"
        ),
        "changed_files": changed_files,
        "commands": commands,
        "completed_work": completed_work,
        "failed_work": failed_work,
        "remaining_work": remaining_work,
        "recent_activity": normalized_activity[-20:],
        "risks": risks,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "takeover_message": (
            "Commander 已接管该 Fusion 任务。Fusion 现在必须停止，不要自行恢复；"
            "Commander 的子 Agent 会从本快照继续，并在结束时返回完成或失败回执。"
        ),
    }


def _fusion_session_for_run(
    run: Mapping[str, Any],
    *,
    home: Path | None,
) -> dict[str, Any]:
    session_id = str(run.get("session_id") or "")
    if session_id:
        value = _read_json(
            fusion_sessions_directory(home) / f"{session_id}.json"
        )
        if value is not None:
            return value
    return {
        "session_id": session_id,
        "workspace": str(run.get("workspace") or ""),
        "tool_mode": str(run.get("tool_mode") or "readonly"),
        "messages": [],
    }


def _takeover_path(task_id: str, home: Path | None = None) -> Path:
    identity = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:24]
    return fusion_takeovers_directory(home) / f"takeover-{identity}.json"


def _new_takeover_manifest(task_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": 1,
        "task_id": str(task_id),
        "status": "open",
        "result_status": "",
        "created_at": now,
        "updated_at": now,
        "closed_at": "",
        "close_reason": "",
        "items": [],
    }


def takeover_manifest(
    task_id: str,
    *,
    home: Path | None = None,
) -> dict[str, Any] | None:
    return _read_json(_takeover_path(task_id, home))


def _save_takeover_manifest(
    manifest: dict[str, Any],
    *,
    home: Path | None,
) -> Path:
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _takeover_path(str(manifest["task_id"]), home)
    _atomic_json(path, manifest)
    return path


def _snapshot_takeover_item(
    run: Mapping[str, Any],
    *,
    task_id: str,
    home: Path | None,
) -> dict[str, Any]:
    session = _fusion_session_for_run(run, home=home)
    activity = run.get("recent_activity")
    handoff = build_fusion_handoff(
        session=session,
        run=run,
        activity=activity if isinstance(activity, list) else [],
        reason="COMMANDER_ACTIVE",
    )
    now = datetime.now(timezone.utc).isoformat()
    return {
        "item_id": str(run.get("run_id") or handoff["run_id"]),
        "source": "runtime_snapshot",
        "handoff_ready": False,
        "status": "pending",
        "takeover_task_id": str(task_id),
        "run_id": str(run.get("run_id") or ""),
        "session_id": str(run.get("session_id") or ""),
        "workspace": str(run.get("workspace") or ""),
        "handoff_path": str(
            fusion_handoffs_directory(home)
            / f"handoff-{run.get('run_id', '')}.json"
        ),
        "handoff": handoff,
        "assigned_agent_id": "",
        "assigned_role": "",
        "assigned_at": "",
        "resolution": None,
        "created_at": now,
        "updated_at": now,
    }


def fusion_run_interrupt_requested(
    run_id: str,
    *,
    home: Path | None = None,
) -> bool:
    path = fusion_runs_directory(home) / f"fusion-{run_id}.json"
    value = _read_json(path)
    return bool(value and value.get("interrupt_requested"))


def _gate_path(lock_dir: Path) -> Path:
    return lock_dir / ".fusion-execution.lock"


@contextmanager
def _execution_gate(lock_dir: Path) -> Iterator[None]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(_gate_path(lock_dir), "a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


@contextmanager
def fusion_execution_gate(
    *,
    home: Path | None = None,
    lock_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Serialize Fusion mutations with Commander lock creation.

    Commander creation and each Fusion tool call use the same gate. Once a
    Commander marker exists, no Fusion tool can start, including the race
    between a state check and the following filesystem mutation or command.
    """
    directory = lock_dir or (fusion_runtime_directory(home) / "locks")
    with _execution_gate(Path(directory)):
        state = fusion_state(
            home=home,
            lock_dir=directory,
            environ=environ,
        )
        if not state["effective_enabled"]:
            raise FusionPreempted(state["reason"], state)
        yield state


class FusionPreempted(RuntimeError):
    def __init__(self, reason: str, state: Mapping[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.state = dict(state)


def _refresh_takeover_handoffs(
    manifest: dict[str, Any],
) -> bool:
    changed = False
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        path = Path(str(item.get("handoff_path") or ""))
        value = _read_json(path) if path.is_file() else None
        if value is None:
            continue
        if value != item.get("handoff"):
            item["handoff"] = value
            item["source"] = "fusion_handoff"
            item["handoff_ready"] = True
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
    return changed


def prepare_fusion_takeover(
    task_id: str,
    *,
    home: Path | None = None,
    wait_timeout: float | None = None,
) -> dict[str, Any]:
    """Capture interrupted Fusion work and wait briefly for native handoffs.

    Commander holds the execution gate, so the Fusion process cannot perform
    another tool mutation while this snapshot is collected. The wait is
    bounded; a live runtime snapshot is used when the model is still in-flight
    or the provider does not return promptly.
    """
    if wait_timeout is None:
        raw_timeout = os.environ.get(
            "AGENTRELAY_FUSION_HANDOFF_WAIT_SECONDS",
            "3",
        )
        try:
            wait_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            wait_timeout = 3.0
    timeout = max(0.0, min(30.0, float(wait_timeout)))
    directory = fusion_runtime_directory(home) / "locks"
    with _execution_gate(directory):
        manifest = takeover_manifest(task_id, home=home)
        if manifest is None:
            manifest = _new_takeover_manifest(task_id)
        existing = {
            str(item.get("run_id") or ""): item
            for item in manifest.get("items", [])
            if isinstance(item, dict)
        }
        for run in active_fusion_runs(home=home):
            run_id = str(run.get("run_id") or "")
            if not run_id:
                continue
            claimed_by = str(run.get("takeover_task_id") or "")
            if claimed_by and claimed_by != str(task_id):
                continue
            item = existing.get(run_id)
            if item is None:
                item = _snapshot_takeover_item(
                    run,
                    task_id=task_id,
                    home=home,
                )
                manifest.setdefault("items", []).append(item)
                existing[run_id] = item
            update_fusion_run(
                run_id,
                home=home,
                takeover_task_id=str(task_id),
                takeover_item_id=str(item["item_id"]),
            )
        _refresh_takeover_handoffs(manifest)
        _save_takeover_manifest(manifest, home=home)

    if timeout:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = [
                item for item in manifest.get("items", [])
                if isinstance(item, dict)
                and item.get("status") in TAKEOVER_OPEN_STATES
                and not item.get("handoff_ready")
            ]
            if not pending:
                break
            time.sleep(0.1)
            with _execution_gate(directory):
                latest = takeover_manifest(task_id, home=home)
                if latest is None:
                    break
                changed = _refresh_takeover_handoffs(latest)
                manifest = latest
                if changed:
                    _save_takeover_manifest(manifest, home=home)

    with _execution_gate(directory):
        latest = takeover_manifest(task_id, home=home)
        if latest is not None:
            changed = _refresh_takeover_handoffs(latest)
            for item in latest.get("items", []):
                if not isinstance(item, dict):
                    continue
                item.setdefault("handoff_ready", item.get("source") == "fusion_handoff")
                item.setdefault("source", "runtime_snapshot")
                item.setdefault("updated_at", latest.get("created_at", ""))
            if changed or latest:
                _save_takeover_manifest(latest, home=home)
            manifest = latest
    return manifest


def assign_fusion_takeover(
    task_id: str,
    assignments: Mapping[str, Iterable[str]],
    *,
    agent_roles: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, Any] | None:
    directory = fusion_runtime_directory(home) / "locks"
    with _execution_gate(directory):
        manifest = takeover_manifest(task_id, home=home)
        if manifest is None:
            return None
        requested: dict[str, set[str]] = {
            str(agent_id): {str(item_id) for item_id in item_ids if str(item_id)}
            for agent_id, item_ids in assignments.items()
        }
        for item in manifest.get("items", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("item_id") or "")
            for agent_id, item_ids in requested.items():
                if item_id not in item_ids or item.get("status") != "pending":
                    continue
                item["status"] = "assigned"
                item["assigned_agent_id"] = agent_id
                item["assigned_role"] = str(
                    (agent_roles or {}).get(agent_id)
                    or item.get("assigned_role")
                    or ""
                )
                item["assigned_at"] = datetime.now(timezone.utc).isoformat()
                item["updated_at"] = item["assigned_at"]
        _save_takeover_manifest(manifest, home=home)
        return manifest


def record_fusion_takeover_result(
    task_id: str,
    agent_id: str,
    *,
    succeeded: bool,
    report: str = "",
    changed_files: Iterable[str] = (),
    verification: Iterable[str] = (),
    error: str = "",
    home: Path | None = None,
) -> dict[str, Any] | None:
    directory = fusion_runtime_directory(home) / "locks"
    with _execution_gate(directory):
        manifest = takeover_manifest(task_id, home=home)
        if manifest is None:
            return None
        _refresh_takeover_handoffs(manifest)
        matched = False
        for item in manifest.get("items", []):
            if (
                not isinstance(item, dict)
                or str(item.get("assigned_agent_id") or "") != str(agent_id)
                or item.get("status") not in TAKEOVER_OPEN_STATES
            ):
                continue
            matched = True
            item["status"] = "completed" if succeeded else "failed"
            item["resolution"] = {
                "status": item["status"],
                "agent_id": str(agent_id),
                "role": str(item.get("assigned_role") or ""),
                "report": str(report)[:8000],
                "changed_files": sorted({
                    str(path) for path in changed_files if str(path)
                }),
                "verification": [
                    str(value) for value in verification if str(value)
                ][:40],
                "error": str(error)[:4000],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            item["updated_at"] = item["resolution"]["recorded_at"]
        if matched:
            _save_takeover_manifest(manifest, home=home)
        return manifest


def _finalize_takeover_locked(
    manifest: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    _refresh_takeover_handoffs(manifest)
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("status") in TAKEOVER_OPEN_STATES:
            item["status"] = "failed"
            item["resolution"] = {
                "status": "failed",
                "agent_id": str(item.get("assigned_agent_id") or ""),
                "role": str(item.get("assigned_role") or ""),
                "report": "",
                "changed_files": [],
                "verification": [],
                "error": (
                    str(reason)
                    or "Commander 未为该 Fusion 遗留任务提交完成证据"
                )[:4000],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            item["updated_at"] = item["resolution"]["recorded_at"]
    items = [
        item for item in manifest.get("items", [])
        if isinstance(item, dict)
    ]
    if not items:
        result_status = "not_required"
    elif all(item.get("status") == "completed" for item in items):
        result_status = "completed"
    else:
        result_status = "failed"
    manifest["status"] = "closed"
    manifest["result_status"] = result_status
    manifest["close_reason"] = str(reason)[:4000]
    manifest["closed_at"] = datetime.now(timezone.utc).isoformat()
    return manifest


def finalize_fusion_takeover(
    task_id: str,
    *,
    reason: str = "",
    home: Path | None = None,
) -> dict[str, Any] | None:
    directory = fusion_runtime_directory(home) / "locks"
    with _execution_gate(directory):
        manifest = takeover_manifest(task_id, home=home)
        if manifest is None:
            return None
        manifest = _finalize_takeover_locked(manifest, reason=reason)
        _save_takeover_manifest(manifest, home=home)
        return manifest


def takeover_receipt(
    manifest: Mapping[str, Any] | None,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    if not manifest:
        return {
            "status": "not_required",
            "items": [],
            "manifest_path": "",
        }
    items = []
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        handoff = item.get("handoff")
        handoff_values = handoff if isinstance(handoff, dict) else {}
        resolution = item.get("resolution")
        resolution_values = resolution if isinstance(resolution, dict) else {}
        source_changed_files = [
            str(path) for path in handoff_values.get("changed_files") or []
            if str(path)
        ]
        commander_changed_files = [
            str(path) for path in resolution_values.get("changed_files") or []
            if str(path)
        ]
        items.append({
            "item_id": str(item.get("item_id") or ""),
            "run_id": str(item.get("run_id") or ""),
            "session_id": str(item.get("session_id") or ""),
            "workspace": str(item.get("workspace") or ""),
            "status": str(item.get("status") or ""),
            "source": str(item.get("source") or ""),
            "original_task": str(handoff_values.get("original_task") or "")[:1000],
            "changed_files": commander_changed_files or source_changed_files,
            "source_changed_files": source_changed_files,
            "report": str(resolution_values.get("report") or "")[:2000],
            "verification": [
                str(value) for value in resolution_values.get("verification") or []
                if str(value)
            ][:40],
            "error": str(resolution_values.get("error") or "")[:1000],
        })
    return {
        "manifest_path": str(
            _takeover_path(str(manifest.get("task_id") or ""), home)
        ),
        "status": str(manifest.get("result_status") or "open"),
        "items": items,
    }


def _write_commander_result(
    directory: Path,
    task_id: str,
    *,
    status: str,
    error: str = "",
    affected_sessions: list[str] | None = None,
    affected_runs: list[str] | None = None,
    takeover: Mapping[str, Any] | None = None,
) -> Path:
    identity = f"{task_id}:{os.getpid()}:{time.time_ns()}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:20]
    path = directory / f"commander-result-{suffix}.json"
    _atomic_json(path, {
        "task_id": task_id,
        "status": status,
        "error": error[:800],
        "affected_sessions": sorted({
            str(item) for item in (affected_sessions or []) if str(item)
        }),
        "affected_runs": sorted({
            str(item) for item in (affected_runs or []) if str(item)
        }),
        "takeover": dict(takeover or {}),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    return path


def commander_results(
    *,
    home: Path | None = None,
    limit: int = 10,
    session_id: str = "",
) -> list[dict[str, Any]]:
    directory = fusion_results_directory(home)
    try:
        paths = sorted(
            directory.glob("commander-result-*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    results: list[dict[str, Any]] = []
    for path in paths:
        value = _read_json(path)
        if not value:
            continue
        affected = value.get("affected_sessions")
        if session_id and (
            not isinstance(affected, list)
            or str(session_id) not in {str(item) for item in affected}
        ):
            continue
        results.append(value)
        if len(results) >= max(1, limit):
            break
    return results


@contextmanager
def fusion_commander_lock(
    task_id: str,
    *,
    home: Path | None = None,
    lock_dir: Path | None = None,
) -> Iterator[Path]:
    """Mark one running Commander without changing the user's Fusion preference."""
    directory = lock_dir or (fusion_runtime_directory(home) / "locks")
    if (
        os.environ.get("AGENTRELAY_FUSION_CHILD") == "1"
        or os.environ.get("AGENTRELAY_COMMANDER_CHILD") == "1"
    ):
        raise PermissionError("FUSION_CHILD_COMMANDER_DENIED: Fusion 子会话不能启动 Commander")
    marker = None
    claimed_runs: list[dict[str, Any]] = []
    result_status = "finished"
    result_error = ""
    try:
        with _execution_gate(Path(directory)):
            marker = _write_lock_marker(Path(directory), str(task_id))
            manifest = takeover_manifest(task_id, home=home)
            for run in active_fusion_runs(home=home):
                claimed_by = str(run.get("takeover_task_id") or "")
                if claimed_by and claimed_by != str(task_id):
                    continue
                run_path = Path(str(run.get("marker_path") or ""))
                if not run_path.is_file():
                    continue
                update = _read_json(run_path)
                if update is None:
                    continue
                update.update({
                    "interrupt_requested": True,
                    "interrupt_reason": "COMMANDER_ACTIVE",
                    "interrupt_task_id": str(task_id),
                    "phase": "commander_preempt",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                _atomic_json(run_path, update)
                claimed_runs.append(update)
                if manifest is None:
                    manifest = _new_takeover_manifest(task_id)
                run_id = str(update.get("run_id") or "")
                existing = {
                    str(item.get("run_id") or "")
                    for item in manifest.get("items", [])
                    if isinstance(item, dict)
                }
                if run_id and run_id not in existing:
                    manifest.setdefault("items", []).append(
                        _snapshot_takeover_item(
                            update,
                            task_id=task_id,
                            home=home,
                        )
                    )
                update["takeover_task_id"] = str(task_id)
                update["takeover_item_id"] = str(
                    update.get("takeover_item_id") or run_id
                )
                _atomic_json(run_path, update)
            if manifest is not None:
                _save_takeover_manifest(manifest, home=home)
    except OSError as exc:
        raise ValueError(f"FUSION_COMMANDER_LOCK_FAILED: {exc}") from exc
    except Exception as exc:
        result_status = "failed"
        result_error = str(exc)
        raise
    try:
        yield marker
    except Exception as exc:
        result_status = "failed"
        result_error = str(exc)
        raise
    finally:
        if marker is not None:
            try:
                with _execution_gate(Path(directory)):
                    manifest = takeover_manifest(task_id, home=home)
                    close_reason = result_error
                    if manifest is not None:
                        if manifest.get("status") != "closed":
                            if not close_reason:
                                close_reason = (
                                    "Commander 未在结束前确认 Fusion 接管清单已全部收尾"
                                )
                            manifest = _finalize_takeover_locked(
                                manifest,
                                reason=close_reason,
                            )
                            _save_takeover_manifest(manifest, home=home)
                        result_status = str(
                            manifest.get("result_status") or result_status
                        )
                    receipt = takeover_receipt(manifest, home=home)
                    affected_sessions = [
                        str(item.get("session_id") or "")
                        for item in (manifest or {}).get("items", [])
                        if isinstance(item, dict)
                    ]
                    affected_runs = [
                        str(item.get("run_id") or "")
                        for item in (manifest or {}).get("items", [])
                        if isinstance(item, dict)
                    ]
                    if not affected_sessions:
                        affected_sessions = [
                            str(item.get("session_id") or "")
                            for item in claimed_runs
                        ]
                    if not affected_runs:
                        affected_runs = [
                            str(item.get("run_id") or "") for item in claimed_runs
                        ]
                    try:
                        _write_commander_result(
                            fusion_results_directory(home),
                            str(task_id),
                            status=(
                                "failed"
                                if result_status == "failed" or result_error
                                else result_status
                            ),
                            error=result_error,
                            affected_sessions=affected_sessions,
                            affected_runs=affected_runs,
                            takeover=receipt,
                        )
                    except OSError:
                        pass
                marker.unlink(missing_ok=True)
            except OSError:
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    pass
