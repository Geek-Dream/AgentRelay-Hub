#!/usr/bin/env python3
"""CLI bridge from AgentRelay Skill to the unified Dispatcher."""

from __future__ import annotations

import argparse
import json
import sys

try:
    from .orchestrator_dispatcher import Dispatcher
    from .orchestrator_store import DispatchRequest
except ImportError:
    from orchestrator_dispatcher import Dispatcher
    from orchestrator_store import DispatchRequest


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Orchestrator Dispatcher")
    parser.add_argument("--request", help="JSON 工作单；不提供时从 stdin 读取")
    args = parser.parse_args()
    raw = args.request if args.request is not None else sys.stdin.read()
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("工作单必须是 JSON 对象")
        request = DispatchRequest(
            task_id=str(value["task_id"]),
            request_id=value.get("request_id"),
            level=str(value.get("mode", value.get("level", "expert"))),
            model=value.get("provider_id", value.get("model", "deepseek-web")),
            title=str(value.get("title", "")),
            prompt=str(value["prompt"]),
            workspace=value.get("workspace"),
            allowed_files=tuple(value.get("allowed_files", ())),
            timeout_seconds=int(value.get("timeout_seconds", 300)),
            read_only=bool(value.get("constraints", {}).get("read_only", True)),
            allow_file_write=bool(value.get("constraints", {}).get("allow_file_write", False)),
            allow_shell=bool(value.get("constraints", {}).get("allow_shell", False)),
            allow_network=bool(value.get("constraints", {}).get("allow_network", False)),
            parent_task_id=value.get("parent_task_id"),
            depth=int(value.get("depth", 0)),
            max_calls=int(value.get("budget", {}).get("max_calls", 1)),
        )
        result = Dispatcher(enable_external=True).dispatch(request)
        print(json.dumps({
            "request_id": result.request_id,
            "task_id": result.task_id,
            "status": result.status,
            "mode": result.mode,
            "provider_id": result.provider_id,
            "content": result.output,
            "changed_files": list(result.changed_files),
            "duration_seconds": result.duration_seconds,
            "error": ({"code": result.error.code, "message": result.error.message, "retryable": result.error.retryable} if result.error else None),
        }, ensure_ascii=False))
        return 0 if result.status == "success" else 1
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
