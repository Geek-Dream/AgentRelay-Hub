#!/usr/bin/env python3
"""Offline task confirmation and execution CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

try:
    from .orchestrator_core import ConfirmationStore, Orchestrator
    from .agentrelay_console import render_confirmation_card
except ImportError:
    from orchestrator_core import ConfirmationStore, Orchestrator
    from agentrelay_console import render_confirmation_card


def _store(root: str | None) -> ConfirmationStore:
    base = Path(root or os.environ.get("AGENT_ORCHESTRATOR_HOME", ".orchestrator"))
    return ConfirmationStore(base / "tasks")


def _public(card):
    return {"task_id": card.task_id, "status": card.status, "action": card.action,
            "scope": list(card.scope), "request": card.request,
            "workspace": card.workspace, "allowed_files": list(card.allowed_files),
            "edits": card.edits, "verification_commands": list(card.verification_commands),
            "attempts": card.attempts, "last_error": card.last_error, "recovery_reason": card.recovery_reason,
            "attempt_history": card.attempt_history, "next_action": card.next_action}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agent Orchestrator task management")
    parser.add_argument("--root", help="runtime directory (default .orchestrator)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    sub = parser.add_subparsers(dest="command", required=True)
    task = sub.add_parser("task")
    actions = task.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    for name in ("show", "status", "approve", "reject", "cancel", "execute", "recover", "retry"):
        cmd = actions.add_parser(name); cmd.add_argument("task_id")
        if name == "recover": cmd.add_argument("--action", choices=("fail", "retry"), required=True)
    args = parser.parse_args(argv)
    store = _store(args.root)
    cards = store.cards
    action = args.action
    task_id = getattr(args, "task_id", None)
    if action == "list":
        value = [_public(card) for card in cards.values()]
        return _emit(value, args.as_json)
    card = cards.get(task_id)
    if card is None:
        return _error(f"Task {task_id} not found.", args.as_json, 1)
    if action in {"show", "status"}:
        if args.as_json:
            return _emit(_public(card), True)
        print(render_confirmation_card(card))
        return 0
    if action in {"approve", "reject", "cancel"}:
        try:
            updated = (store.advance_requirement(task_id) if action == "approve" and card.action == "confirm_requirement"
                       else store.transition(task_id, {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[action]))
        except (KeyError, ValueError) as exc:
            return _error(f"Task {task_id} cannot {action}: {exc}", args.as_json, 1)
        return _emit({"task_id": task_id, "status": updated.status}, args.as_json)
    if action == "recover":
        try: updated = store.recover(task_id, args.action, "manual CLI recovery")
        except (KeyError, ValueError) as exc: return _error(str(exc), args.as_json, 1)
        return _emit({"task_id": task_id, "status": updated.status}, args.as_json)
    if action == "retry":
        try: updated = store.retry_failed(task_id)
        except (KeyError, ValueError) as exc: return _error(str(exc), args.as_json, 1)
        return _emit({"task_id": task_id, "status": updated.status}, args.as_json)
    try:
        result = Orchestrator(confirmations=store).execute_confirmed_task(task_id)
        return _emit({"task_id": task_id, "status": result["status"]}, args.as_json,
                     0 if result["status"] == "completed" else 1)
    except (PermissionError, KeyError, ValueError, OSError) as exc:
        return _error(f"Task {task_id} cannot execute: {exc}", args.as_json, 1)


def _emit(value, as_json: bool, code: int = 0) -> int:
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list):
        for item in value:
            print(f"{item['task_id']}\t{item['status']}\t{item.get('request', '')[:80]}")
    elif "status" in value and len(value) <= 2:
        print(f"Task {value['task_id']} {value['status']}.")
    else:
        for key, item in value.items():
            print(f"{key}: {item}")
    return code


def _error(message: str, as_json: bool, code: int) -> int:
    if as_json:
        print(json.dumps({"error": message}, ensure_ascii=False))
    else:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
