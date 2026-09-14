#!/usr/bin/env python3
"""Cross-platform Codex Hook wrapper for AgentRelay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


CODEX_HOME = Path(
    os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
).expanduser()
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
TRACKER = Path(
    os.environ.get(
        "AGENT_RELAY_TRACKER_PATH",
        str(SCRIPT_DIR / "agent_relay_tracker.py"),
    )
).expanduser()
LOG_DIR = CODEX_HOME / "agent_relay_tracker" / "logs"
ERROR_LOG = LOG_DIR / "error.log"


def commander_child_runtime() -> tuple[Path, str] | None:
    """Return the explicitly assigned Commander child runtime, if any.

    A child must never infer its task identity from its Codex session. The
    parent assigns it before the process starts, so a missing value is unsafe
    to bridge into Commander state.
    """
    if os.environ.get("AGENTRELAY_COMMANDER_CHILD") != "1":
        return None
    runtime_dir = os.environ.get("AGENTRELAY_COMMANDER_RUNTIME_DIR", "").strip()
    task_id = os.environ.get("AGENTRELAY_COMMANDER_TASK_ID", "").strip()
    if not runtime_dir or not task_id:
        log_error("Commander child Hook missing assigned runtime or task id; event ignored")
        return None
    return Path(runtime_dir), task_id


def _append_commander_notice(runtime_dir: Path, context, payload: dict) -> None:
    """Persist a one-time advisory notice for the parent Commander."""
    notices_path = runtime_dir / "commander-events.jsonl"
    notices_path.parent.mkdir(parents=True, exist_ok=True)
    notice = {
        "type": "CHILD_ESCALATION_REQUIRED",
        "task_id": context.task_id,
        "parent_task_id": os.environ.get("AGENTRELAY_COMMANDER_PARENT_TASK_ID", ""),
        "role": os.environ.get("AGENTRELAY_COMMANDER_ROLE", ""),
        "retry_count": context.retry_count,
        "effective_time_seconds": round(context.effective_time_seconds, 3),
        "weight": context.weight,
        "event_type": payload.get("event_type", "TRACKER_SNAPSHOT"),
        "continue_work": True,
        "message": "子 Agent 已达到自己的升级条件；请 Commander 告知用户并决定是否求援，子 Agent 继续工作。",
    }
    with notices_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(notice, ensure_ascii=False) + "\n")


def ingest_commander_child_event(payload: dict) -> None:
    """Ingest a Hook event under its parent-assigned child task id only."""
    configuration = commander_child_runtime()
    if configuration is None:
        return
    runtime_dir, task_id = configuration
    event = dict(payload.get("task_event") or payload)
    # The tracker snapshot owns a Codex-session task id. Replace it so sibling
    # children cannot ever share retry/time state.
    event["task_id"] = task_id
    event["request"] = os.environ.get("AGENTRELAY_COMMANDER_REQUEST", event.get("request", ""))
    event["workspace"] = os.environ.get("AGENTRELAY_COMMANDER_WORKSPACE", event.get("workspace", ""))
    event["event_type"] = str(event.get("event_type") or event.get("event") or "TRACKER_SNAPSHOT")
    # A failed tool is an observation, never an instruction to stop this child.
    event.pop("status", None)
    try:
        from scripts.runtime_manager import RuntimeManager
        manager = RuntimeManager(runtime_dir / "child-tasks")
        try:
            was_escalated = manager.load_context(task_id).need_escalation
        except (OSError, ValueError, KeyError):
            was_escalated = False
        context = manager.ingest_tracker_event(event)
        if context.need_escalation and not was_escalated:
            _append_commander_notice(runtime_dir, context, event)
    except (ImportError, OSError, ValueError, TypeError, KeyError) as exc:
        log_error(f"Unable to ingest Commander child event: {exc}")


def run_orchestrator_task(payload: dict) -> dict | None:
    """Route an explicit task envelope through the offline Orchestrator.

    Normal PostToolUse events remain observation-only.  Integrations that have
    a task request can provide ``orchestrator_task``; this keeps the Hook as a
    thin adapter while ensuring task decisions use the single workflow API.
    """
    envelope = payload.get("orchestrator_task")
    if not isinstance(envelope, dict) or not envelope.get("request"):
        return None
    try:
        from scripts.orchestrator_core import Orchestrator
        task_id = str(envelope.get("task_id") or "hook-task")
        result = Orchestrator().execute_task(
            task_id, str(envelope["request"]),
            complexity=envelope.get("complexity"),
            modules=tuple(envelope.get("modules", ())),
            allowed_files=tuple(envelope.get("allowed_files", ())),
            workspace=envelope.get("workspace"),
            edits=dict(envelope.get("edits", {})),
            verification_commands=tuple(envelope.get("verification_commands", ())),
        )
        return {"task_id": task_id, "task_type": result["task_type"],
                "status": "awaiting_confirmation" if "confirmation" in result else "completed"}
    except (TypeError, ValueError, OSError, KeyError) as exc:
        log_error(f"Unable to route orchestrator task: {exc}")
        return None


def ingest_tracker_context(payload: dict) -> None:
    """Best-effort bridge from Hook state to RuntimeManager when configured."""
    if os.environ.get("AGENTRELAY_COMMANDER_CHILD") == "1":
        if isinstance(payload, dict):
            ingest_commander_child_event(payload)
        return
    runtime_dir = os.environ.get("AGENT_ORCHESTRATOR_RUNTIME")
    if not runtime_dir or not isinstance(payload, dict):
        return
    event = payload.get("task_event")
    if event is None and payload.get("task_id"):
        event = {key: payload[key] for key in (
            "task_id", "request", "workspace", "retry_count", "weight",
            "need_escalation", "effective_time_seconds", "elapsed_seconds",
            "error", "stderr", "stdout", "timestamp") if key in payload}
        event_name = (payload.get("event_type") or payload.get("event_name") or
                      payload.get("event") or payload.get("hook_event_name") or
                      payload.get("hookEventName"))
        if event_name:
            event["event_type"] = str(event_name).upper()
        if event.get("event_type") in {"INSTALL_FAILED", "BUILD_FAILED", "TEST_FAILED", "COMMAND_FAILED"}:
            event["status"] = "FAILED"
    if not isinstance(event, dict):
        return
    try:
        from scripts.runtime_manager import RuntimeManager
        RuntimeManager(Path(runtime_dir) / "tasks").ingest_tracker_event(event)
    except (ImportError, OSError, ValueError, TypeError, KeyError) as exc:
        log_error(f"Unable to ingest task event: {exc}")
        return None


def ingest_tracker_snapshot() -> None:
    """Bridge the tracker state produced by this Hook invocation to Runtime."""
    child_configuration = commander_child_runtime()
    if os.environ.get("AGENTRELAY_COMMANDER_CHILD") == "1" and child_configuration is None:
        return
    runtime_dir = os.environ.get("AGENT_ORCHESTRATOR_RUNTIME")
    if child_configuration is not None:
        runtime_dir = str(child_configuration[0])
    if not runtime_dir:
        return
    try:
        snapshot = subprocess.run(
            [sys.executable, str(TRACKER), "snapshot"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=4, check=False,
        )
        if snapshot.returncode != 0 or not snapshot.stdout.strip():
            return
        event = json.loads(snapshot.stdout)
        if not isinstance(event, dict):
            return
        if child_configuration is not None:
            ingest_commander_child_event(event)
            return
        if not event.get("task_id"):
            return
        from scripts.runtime_manager import RuntimeManager
        RuntimeManager(Path(runtime_dir) / "tasks").ingest_tracker_event(event)
    except (ImportError, OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        log_error(f"Unable to ingest tracker snapshot: {exc}")


def log_error(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().isoformat()
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def valid_hook_output(value: str) -> bool:
    if not value.strip():
        return False
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return False
    output = payload.get("hookSpecificOutput")
    return (
        isinstance(output, dict)
        and output.get("hookEventName") == "PostToolUse"
        and isinstance(output.get("additionalContext"), str)
    )


def main() -> int:
    raw_payload = sys.stdin.read()
    if not raw_payload.strip():
        return 0
    if not TRACKER.is_file():
        log_error(f"Tracker not found: {TRACKER}")
        return 0

    try:
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            payload = {}
        ingest_tracker_context(payload)
        task_result = run_orchestrator_task(payload) if isinstance(payload, dict) else None
        result = subprocess.run(
            [sys.executable, str(TRACKER)],
            input=raw_payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log_error(f"Unable to run Tracker: {exc}")
        return 0

    if result.returncode != 0:
        log_error(
            f"Tracker exited with code {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    ingest_tracker_snapshot()
    if valid_hook_output(result.stdout):
        if task_result is not None:
            output = json.loads(result.stdout)
            output["hookSpecificOutput"]["orchestratorTask"] = task_result
            result.stdout = json.dumps(output, ensure_ascii=False)
        sys.stdout.write(result.stdout.rstrip() + "\n")
        sys.stdout.flush()
    elif result.stdout.strip():
        log_error("Tracker output was not valid PostToolUse hook JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
