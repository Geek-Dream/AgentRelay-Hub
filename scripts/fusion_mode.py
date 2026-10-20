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
from typing import Any, Iterator, Mapping

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


def _write_commander_result(
    directory: Path,
    task_id: str,
    *,
    status: str,
    error: str = "",
    affected_sessions: list[str] | None = None,
    affected_runs: list[str] | None = None,
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
    for path in paths[:max(1, limit)]:
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
    preempted_runs: list[dict[str, Any]] = []
    result_status = "finished"
    result_error = ""
    try:
        with _execution_gate(Path(directory)):
            marker = _write_lock_marker(Path(directory), str(task_id))
            for run in active_fusion_runs(home=home):
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
                preempted_runs.append(update)
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
            with _execution_gate(Path(directory)):
                marker.unlink(missing_ok=True)
        if marker is not None:
            try:
                _write_commander_result(
                    fusion_results_directory(home),
                    str(task_id),
                    status=result_status,
                    error=result_error,
                    affected_sessions=[
                        str(item.get("session_id") or "") for item in preempted_runs
                    ],
                    affected_runs=[
                        str(item.get("run_id") or "") for item in preempted_runs
                    ],
                )
            except OSError:
                pass
