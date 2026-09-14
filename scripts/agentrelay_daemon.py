"""Small polling daemon for TaskContext monitoring (offline by default)."""
from __future__ import annotations
import argparse, time
from pathlib import Path
try:
 from .runtime_manager import RuntimeManager
 from .task_monitor import TaskMonitor
 from .orchestrator_core import TaskContext, WorkflowEngine
except ImportError:
 from runtime_manager import RuntimeManager
 from task_monitor import TaskMonitor
 from orchestrator_core import TaskContext, WorkflowEngine

class AgentRelayDaemon:
    def __init__(self, runtime_dir="runtime/tasks", workflow=None):
        self.runtime = RuntimeManager(runtime_dir); self.monitor = TaskMonitor()
        # The CLI daemon must be a real orchestration entry point.  Build the
        # offline-first engine automatically, while allowing tests/integrators
        # to inject a configured engine.
        self.workflow = workflow or WorkflowEngine.from_environment(
            runtime_dir=self.runtime.directory.parent)
    def poll_once(self):
        updated=[]
        for context in self.runtime.process_event_queue():
            updated.append(context.task_id)
            self._evaluate_escalation(context)
        if not self.runtime.directory.exists(): return updated
        for path in self.runtime.directory.glob("*.json"):
            try:
                context=TaskContext.load(path)
                if context.status.value in {"EXECUTING","VERIFYING","FAILED"}:
                    self.monitor.update(context); self.runtime.save_context(context); self._evaluate_escalation(context); updated.append(context.task_id)
            except (OSError, ValueError, TypeError, KeyError):
                continue
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
    parser.add_argument("--runtime", default="runtime/tasks"); parser.add_argument("--interval", type=float, default=5); parser.add_argument("--once", action="store_true")
    args=parser.parse_args(); AgentRelayDaemon(args.runtime).run(args.interval, args.once)
if __name__ == "__main__": main()
