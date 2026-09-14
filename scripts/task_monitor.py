"""Offline task time/retry monitor."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class MonitorSnapshot:
    elapsed_seconds: float
    weight: int
    retry_count: int
    need_escalation: bool


class TaskMonitor:
    def observe_tracker_state(self, state: dict, *, elapsed_seconds: float | None = None) -> MonitorSnapshot:
        """Update a Tracker-compatible state dict without requiring TaskContext."""
        elapsed = float(elapsed_seconds if elapsed_seconds is not None else state.get("round_effective_time_seconds", state.get("effective_time_seconds", 0)) or 0)
        retry_count = int(state.get("retry_count", 0) or 0)
        weight = min(3, max(int(state.get("weight", 1) or 1), 1 + int(elapsed // 300)))
        state["weight"] = weight
        state["need_escalation"] = retry_count >= 3 or elapsed >= 900
        return MonitorSnapshot(elapsed, weight, retry_count, state["need_escalation"])

    def update(self, context, *, elapsed_seconds: float | None = None) -> MonitorSnapshot:
        if elapsed_seconds is None:
            elapsed = float(getattr(context, "effective_time_seconds", 0) or 0)
            if elapsed <= 0 and getattr(getattr(context, "status", None), "value", None) in {"EXECUTING", "VERIFYING"}:
                try:
                    started = datetime.fromisoformat(context.created_time)
                    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
                except (AttributeError, TypeError, ValueError):
                    elapsed = 0.0
        else:
            elapsed = float(elapsed_seconds)
        # Weight is the number of five-minute thresholds reached, capped at 3.
        context.effective_time_seconds = elapsed
        context.weight = min(3, 1 + int(elapsed // 300))
        context.need_escalation = context.retry_count >= 3 or elapsed >= 900
        return MonitorSnapshot(elapsed, context.weight, context.retry_count, context.need_escalation)

    def record_retry(self, context, *, elapsed_seconds: float = 0) -> MonitorSnapshot:
        context.retry_count += 1
        return self.update(context, elapsed_seconds=elapsed_seconds)
