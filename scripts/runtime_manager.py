"""Persistence and state coordination for offline task runtimes."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timezone
import json
import uuid
try:
    from .orchestrator_core import TaskContext, TaskExecutionState
except ImportError:
    from orchestrator_core import TaskContext, TaskExecutionState


class RuntimeManager:
    def __init__(self, directory: str | Path = "runtime/tasks"):
        self.directory = Path(directory)
        self.events_directory = self.directory.parent / "events"
        self.queue_path = self.events_directory / "queue.jsonl"
        self.processed_queue_path = self.events_directory / "queue.processed"

    def create_context(self, task_id: str, request: str, workspace=None) -> TaskContext:
        context = TaskContext(task_id, str(workspace) if workspace else None, request)
        self.save_context(context)
        return context

    def context_path(self, task_id: str) -> Path:
        return self.directory / f"{task_id}.json"

    def load_context(self, task_id: str) -> TaskContext:
        return TaskContext.load(self.context_path(task_id))

    def save_context(self, context: TaskContext) -> Path:
        return context.save(self.directory)

    def update_state(self, context: TaskContext, state: TaskExecutionState) -> TaskContext:
        before = context.status
        context.transition(state)
        self.save_context(context)
        self.record_change(context, before, state)
        return context

    def record_change(self, context: TaskContext, before, after) -> dict:
        change = {"task_id": context.task_id, "before_state": str(before.value if hasattr(before, "value") else before),
                "after_state": str(after.value if hasattr(after, "value") else after),
                "changed_files": list(context.changed_files), "rollback_available": bool(context.changed_files)}
        self.record_event(context, {"event_type": "STATE_CHANGE", **change})
        return change

    def record_event(self, context: TaskContext, event: dict) -> None:
        self.events_directory.mkdir(parents=True, exist_ok=True)
        payload = {"at": datetime.now(timezone.utc).isoformat(), **event}
        path = self.events_directory / f"{context.task_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def enqueue_event(self, event: dict) -> str:
        """Append a Hook event to the durable, process-wide event queue."""
        if not isinstance(event, dict):
            raise TypeError("事件必须是对象")
        event_id = str(event.get("event_id") or uuid.uuid4())
        payload = {"event_id": event_id, "queued_at": datetime.now(timezone.utc).isoformat(), **event}
        self.events_directory.mkdir(parents=True, exist_ok=True)
        with self.queue_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return event_id

    def _processed_event_ids(self) -> set[str]:
        try:
            return {line.strip() for line in self.processed_queue_path.read_text(encoding="utf-8").splitlines() if line.strip()}
        except OSError:
            return set()

    def _mark_event_processed(self, event_id: str) -> None:
        self.events_directory.mkdir(parents=True, exist_ok=True)
        with self.processed_queue_path.open("a", encoding="utf-8") as handle:
            handle.write(event_id + "\n")

    def process_event_queue(self, limit: int | None = None) -> list[TaskContext]:
        """Apply queued events that were not fully processed before a crash."""
        try:
            lines = self.queue_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        processed = self._processed_event_ids()
        contexts = []
        for line in lines:
            if limit is not None and len(contexts) >= limit:
                break
            try:
                event = json.loads(line)
                event_id = str(event.get("event_id") or "")
                if not event_id or event_id in processed:
                    continue
                context = self._apply_tracker_event(event)
                self._mark_event_processed(event_id)
                processed.add(event_id)
                contexts.append(context)
            except (TypeError, ValueError, KeyError, OSError, json.JSONDecodeError):
                continue
        return contexts

    def ingest_tracker_event(self, event: dict) -> TaskContext:
        event_id = self.enqueue_event(event)
        context = self._apply_tracker_event({**event, "event_id": event_id})
        self._mark_event_processed(event_id)
        return context

    def _apply_tracker_event(self, event: dict) -> TaskContext:
        task_id = str(event.get("task_id") or "")
        if not task_id:
            raise ValueError("Tracker 事件缺少 task_id")
        try:
            context = self.load_context(task_id)
        except (OSError, ValueError, KeyError):
            context = self.create_context(task_id, str(event.get("request") or event.get("problem") or task_id), event.get("workspace"))
        context.retry_count = max(context.retry_count, int(event.get("retry_count", context.retry_count) or 0))
        event_elapsed = event.get("effective_time_seconds", event.get("elapsed_seconds"))
        if event_elapsed is not None:
            try:
                context.effective_time_seconds = max(context.effective_time_seconds, float(event_elapsed))
            except (TypeError, ValueError):
                pass
        event_weight = int(event.get("weight", 0) or 0)
        time_weight = min(3, 1 + int(context.effective_time_seconds // 300))
        context.weight = min(3, max(context.weight, event_weight, time_weight))
        context.need_escalation = context.retry_count >= 3 or context.effective_time_seconds >= 900 or bool(event.get("need_escalation", False))
        event_type = str(event.get("event_type") or event.get("event") or "TRACKER_SNAPSHOT")
        context.last_event_type = event_type
        context.last_event_at = str(event.get("timestamp") or datetime.now(timezone.utc).isoformat())
        requested = event.get("status")
        if requested:
            try:
                target = TaskExecutionState(str(requested).upper())
                if target != context.status:
                    # A first failure event may arrive before a submit call
                    # created lifecycle states. Establish the minimal legal
                    # path instead of bypassing the state machine.
                    if target == TaskExecutionState.FAILED and context.status == TaskExecutionState.CREATED:
                        context.transition(TaskExecutionState.ANALYZING)
                        context.transition(TaskExecutionState.EXECUTING)
                    context.transition(target)
            except (ValueError, TypeError):
                # A stale/out-of-order Hook event must not bypass the state machine.
                pass
        self.save_context(context)
        self.record_event(context, {"event_type": event_type, **event})
        return context
