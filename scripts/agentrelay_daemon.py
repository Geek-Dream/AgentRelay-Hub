"""Resident scheduler/recovery loop for TaskContext and Provider leases."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import time

try:
    from . import config_manager, fusion_mode
    from .runtime_manager import RuntimeManager
    from .task_monitor import TaskMonitor
    from .orchestrator_core import TaskContext, WorkflowEngine
except ImportError:
    import config_manager
    import fusion_mode
    from runtime_manager import RuntimeManager
    from task_monitor import TaskMonitor
    from orchestrator_core import TaskContext, WorkflowEngine


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

class AgentRelayDaemon:
    def __init__(self, runtime_dir="runtime/tasks", workflow=None, home: Path | None = None):
        self.runtime = RuntimeManager(runtime_dir)
        self.monitor = TaskMonitor()
        self.home = home
        self.status_path = self.runtime.directory.parent / "provider-scheduler.json"
        # The CLI daemon must be a real orchestration entry point.  Build the
        # offline-first engine automatically, while allowing tests/integrators
        # to inject a configured engine.
        self.workflow = workflow or WorkflowEngine.from_environment(
            runtime_dir=self.runtime.directory.parent)

    def provider_status(self) -> dict:
        config = config_manager.load_config(self.home)
        providers = []
        for kind, field in (
            ("web", "web_providers"),
            ("local", "local_providers"),
            ("api", "api_providers"),
        ):
            for entry in config.get(field, []) if isinstance(config.get(field), list) else []:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                defaults = config_manager.provider_concurrency_defaults(kind)
                provider = {
                    "kind": kind,
                    "id": str(entry["id"]),
                    "ref": f"{kind}:{entry['id']}",
                    "entry": entry,
                }
                manager = fusion_mode.provider_lease_manager(
                    provider,
                    home=self.home,
                )
                providers.append(manager.status())
        active = sum(int(item.get("active", 0)) for item in providers)
        limit = sum(int(item.get("limit", 0)) for item in providers)
        queued = sum(int(item.get("queued", 0)) for item in providers)
        degraded = any(bool(item.get("degraded")) for item in providers)
        return {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "active": active,
            "limit": limit,
            "queued": queued,
            "degraded": degraded,
            "fallback_reason": "PROVIDER_QUEUE_ACTIVE" if degraded else "",
            "providers": providers,
        }

    def write_provider_status(self) -> dict:
        status = self.provider_status()
        _atomic_json(self.status_path, status)
        return status

    def poll_once(self):
        updated=[]
        for context in self.runtime.process_event_queue():
            updated.append(context.task_id)
            self._evaluate_escalation(context)
        if self.runtime.directory.exists():
            for path in self.runtime.directory.glob("*.json"):
                try:
                    context=TaskContext.load(path)
                    if context.status.value in {"EXECUTING","VERIFYING","FAILED"}:
                        self.monitor.update(context)
                        self.runtime.save_context(context)
                        self._evaluate_escalation(context)
                        updated.append(context.task_id)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
        self.write_provider_status()
        return updated

    def _evaluate_escalation(self, context):
        if self.workflow is not None and context.need_escalation:
            self.workflow.evaluate_escalation(context)

    def ingest_event(self, event: dict):
        return self.runtime.ingest_tracker_event(event)
    def run(self, interval=5, once=False):
        while True:
            self.poll_once()
            if once: return
            time.sleep(interval)

def main():
    parser=argparse.ArgumentParser(description="AgentRelay offline monitor daemon")
    parser.add_argument("--runtime", default="runtime/tasks")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--home", default="")
    parser.add_argument("--once", action="store_true")
    args=parser.parse_args()
    home = Path(args.home).expanduser() if args.home else None
    AgentRelayDaemon(args.runtime, home=home).run(args.interval, args.once)
if __name__ == "__main__": main()
