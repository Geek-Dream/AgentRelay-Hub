"""Explicit, revocable permission to close one verified application."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

PROCESS_STOP_REASON = "权限或系统级操作"


def permission_path(codex_home: Path) -> Path:
    return codex_home / "agent_relay_tracker" / "app-close-permissions.json"


def load_permissions(codex_home: Path) -> dict[str, Any]:
    try:
        data = json.loads(permission_path(codex_home).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": 1, "applications": []}
    entries = data.get("applications")
    return {"version": 1, "applications": entries if isinstance(entries, list) else []}


def _target(command: str) -> str | None:
    """Return a literal executable/image name, never a PID or wildcard."""
    try:
        import shlex
        words = shlex.split(command, posix=False)
    except ValueError:
        return None
    if not words:
        return None
    command_name = Path(words[0].strip('"')).name.lower()
    target = None
    if command_name == "taskkill":
        for index, word in enumerate(words):
            if word.lower() in {"/pid", "/f", "/t"}:
                return None
            if word.lower() == "/im" and index + 1 < len(words):
                target = words[index + 1]
                break
    elif command_name == "stop-process":
        for index, word in enumerate(words):
            lowered = word.lower().lstrip("-")
            if lowered in {"id", "inputobject"}:
                return None
            if lowered == "name" and index + 1 < len(words):
                target = words[index + 1]
                break
            if not word.startswith("-") and index > 0 and target is None:
                target = word
    else:
        return None
    target = str(target or "").strip('"').strip()
    if not target or re.search(r"[*?\[\];|&]", target):
        return None
    if Path(target).name.lower() in {
        "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
        "winlogon.exe", "services.exe", "lsass.exe", "explorer.exe",
    }:
        return None
    return target.casefold()


def can_remember(command: str) -> bool:
    return _target(command) is not None


def remember(command: str, codex_home: Path) -> dict[str, Any]:
    target = _target(command)
    if target is None:
        raise ValueError("只有明确指定应用名称的 taskkill /IM 或 Stop-Process -Name 才能记住")
    data = load_permissions(codex_home)
    entry = {"target": target, "fingerprint": hashlib.sha256(target.encode("utf-8")).hexdigest()}
    if entry not in data["applications"]:
        data["applications"].append(entry)
    path = permission_path(codex_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return entry


def allows(command: str, codex_home: Path) -> bool:
    target = _target(command)
    if target is None:
        return False
    fingerprint = hashlib.sha256(target.encode("utf-8")).hexdigest()
    return any(
        isinstance(item, dict) and item.get("fingerprint") == fingerprint
        for item in load_permissions(codex_home)["applications"]
    )


def forget(target: str, codex_home: Path) -> bool:
    data = load_permissions(codex_home)
    normalized = str(target).strip().casefold()
    remaining = [item for item in data["applications"] if item.get("target") != normalized]
    changed = len(remaining) != len(data["applications"])
    if changed:
        data["applications"] = remaining
        permission_path(codex_home).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    return changed
