#!/usr/bin/env python3
"""Run a safe, offline end-to-end AgentRelay task demonstration."""
from __future__ import annotations
import json
import tempfile
from pathlib import Path
from dataclasses import asdict

try:
    from .orchestrator_core import Orchestrator, WorkflowEngine
    from .agent_provider import DeepSeekProvider
except ImportError:
    from orchestrator_core import Orchestrator, WorkflowEngine
    from agent_provider import DeepSeekProvider


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentrelay-demo-") as workspace:
        root = Path(workspace)
        target = root / "UserDto.java"
        target.write_text("public class UserDto {\n}\n", encoding="utf-8")
        runtime = root / "runtime"
        orchestrator = Orchestrator()
        engine = WorkflowEngine(orchestrator=orchestrator, runtime_dir=runtime,
                                agent_provider=DeepSeekProvider())
        outcome = engine.submit(
            "demo-user-dto", "重构 UserDto 并增加 age 字段", complexity=5,
            modules=("backend",), allowed_files=("UserDto.java",),
            workspace=root, edits={"UserDto.java": "public class UserDto {\n    private int age;\n}\n"},
            verification_commands=("python3 -m compileall -q scripts",))
        card = outcome["card"]
        confirmation = outcome["confirmation"]
        engine.approve_and_execute("demo-user-dto")
        completed = engine.approve_and_execute("demo-user-dto")
        context = engine.runtime_manager.load_context("demo-user-dto")
        print(json.dumps({
            "workspace": str(root),
            "requirement_card": asdict(card),
            "confirmation_status": confirmation.status,
            "execution_status": completed["status"],
            "file_content": target.read_text(encoding="utf-8"),
            "task_context": asdict(context) | {"status": context.status.value},
            "changelog": [asdict(item) for item in orchestrator.changelog],
        }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
