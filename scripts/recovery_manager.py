"""Discovery of interrupted offline task contexts."""
from __future__ import annotations
from pathlib import Path
try:
    from .orchestrator_core import TaskContext, TaskExecutionState
except ImportError:
    from orchestrator_core import TaskContext, TaskExecutionState


class RecoveryManager:
    RECOVERABLE = {TaskExecutionState.EXECUTING, TaskExecutionState.VERIFYING, TaskExecutionState.FAILED}

    def __init__(self, runtime_manager):
        self.runtime_manager = runtime_manager

    def scan(self) -> list[TaskContext]:
        found = []
        directory = self.runtime_manager.directory
        if not directory.exists():
            return found
        for path in directory.glob("*.json"):
            try:
                context = TaskContext.load(path)
                if context.status in self.RECOVERABLE:
                    found.append(context)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return found
