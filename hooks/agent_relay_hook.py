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
TRACKER = Path(
    os.environ.get(
        "AGENT_RELAY_TRACKER_PATH",
        str(SCRIPT_DIR / "agent_relay_tracker.py"),
    )
).expanduser()
LOG_DIR = CODEX_HOME / "agent_relay_tracker" / "logs"
ERROR_LOG = LOG_DIR / "error.log"


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
    if valid_hook_output(result.stdout):
        sys.stdout.write(result.stdout.rstrip() + "\n")
        sys.stdout.flush()
    elif result.stdout.strip():
        log_error("Tracker output was not valid PostToolUse hook JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
