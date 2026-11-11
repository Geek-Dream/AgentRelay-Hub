"""Small structured diagnostics writer for AgentFusion timeout analysis."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping


TRACE_PATH_ENV = "AGENTRELAY_FUSION_TRACE"
TRACE_ID_ENV = "AGENTRELAY_FUSION_TRACE_ID"
TRACE_SESSION_ENV = "AGENTRELAY_FUSION_TRACE_SESSION"
MAX_FIELD_LENGTH = 500


def default_trace_path(home: Path | None = None) -> Path:
    base = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        if home is None
        else home.expanduser()
    )
    return base / "agent_relay_tracker" / "logs" / "fusion-trace.jsonl"


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_FIELD_LENGTH]
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, Mapping):
        return {
            str(key)[:80]: _safe_value(item)
            for key, item in list(value.items())[:20]
        }
    return str(value)[:MAX_FIELD_LENGTH]


def emit(
    event: str,
    *,
    environ: Mapping[str, str] | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    """Append one bounded JSON record when tracing is enabled."""
    env = environ if environ is not None else os.environ
    raw_path = str(env.get(TRACE_PATH_ENV) or "").strip()
    if not raw_path:
        return None
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "timestamp_unix": time.time(),
        "pid": os.getpid(),
        "trace_id": str(env.get(TRACE_ID_ENV) or ""),
        "session_id": str(env.get(TRACE_SESSION_ENV) or ""),
        "event": str(event)[:120],
        **{str(key): _safe_value(value) for key, value in fields.items()},
    }
    path = Path(raw_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(
                descriptor,
                (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            )
        finally:
            os.close(descriptor)
    except OSError:
        return None
    return record


def recent_events(
    path: Path,
    trace_id: str = "",
    *,
    limit: int = 20,
    max_bytes: int = 256 * 1024,
) -> list[dict[str, Any]]:
    """Read a bounded tail of valid trace records for one request."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max(1, int(max_bytes))))
            data = handle.read()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for raw_line in data.splitlines():
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            continue
        if not isinstance(item, dict):
            continue
        if trace_id and str(item.get("trace_id") or "") != trace_id:
            continue
        events.append(item)
    return events[-max(1, int(limit)):]


def last_stage(
    events: list[dict[str, Any]],
    *,
    exclude_events: set[str] | None = None,
) -> str:
    excluded = exclude_events or set()
    for item in reversed(events):
        if str(item.get("event") or "").strip() in excluded:
            continue
        stage = str(item.get("stage") or "").strip()
        if stage:
            return stage
        event = str(item.get("event") or "").strip()
        if event:
            return event
    return ""


def diagnostic_stage(
    events: list[dict[str, Any]],
    *,
    exclude_events: set[str] | None = None,
) -> str:
    """Return the failure cause before routine cleanup events."""
    excluded = exclude_events or set()
    filtered = [
        item
        for item in events
        if str(item.get("event") or "").strip() not in excluded
    ]
    stages = {
        str(item.get("stage") or "").strip()
        for item in filtered
    }
    for stage in ("provider_queue_timeout", "provider_error", "model_error"):
        if stage in stages:
            return stage
    return last_stage(filtered)
