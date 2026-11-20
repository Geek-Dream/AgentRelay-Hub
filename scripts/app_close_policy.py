"""Explicit, revocable permission to close one verified application."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
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


def _words(command: str) -> list[str] | None:
    """Parse only one literal command; never authorize a shell chain."""
    text = str(command or "").strip()
    if not text or re.search(r"(?:&&|\|\||[;|<>`\n\r])", text):
        return None
    try:
        words = shlex.split(text, posix=False)
    except ValueError:
        return None
    return [word.strip('"') for word in words] or None


def _valid_target(value: str) -> str | None:
    target = str(value or "").strip().strip('"').strip()
    if not target or re.search(r"[*?\[\];|&/\\]", target):
        return None
    if target.casefold() in {
        "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
        "winlogon.exe", "services.exe", "lsass.exe", "explorer.exe",
        "kernel", "launchd", "windowserver", "loginwindow",
    }:
        return None
    return target.casefold()


def _target(command: str) -> str | None:
    """Return a target only for a single safe app-close invocation."""
    words = _words(command)
    if not words:
        return None
    command_name = Path(words[0]).name.lower()
    target = None
    if command_name == "taskkill":
        index = 1
        while index < len(words):
            option = words[index].lower()
            if option == "/im" and index + 1 < len(words):
                if target is not None:
                    return None
                target = words[index + 1]
                index += 2
                continue
            if option in {"/f"}:
                index += 1
                continue
            if option in {"/fi", "/pid", "/t"}:
                return None
            return None
    elif command_name == "stop-process":
        index = 1
        while index < len(words):
            option = words[index].lower()
            if option == "-name" and index + 1 < len(words):
                if target is not None:
                    return None
                target = words[index + 1]
                index += 2
                continue
            if option in {"-force", "-confirm:$false", "-passthru"}:
                index += 1
                continue
            return None
    elif command_name == "pkill":
        if len(words) == 3 and words[1] == "-x":
            target = words[2]
        else:
            return None
    else:
        return None
    return _valid_target(target or "")


def can_remember(command: str) -> bool:
    return _target(command) is not None


def remember(command: str, codex_home: Path) -> dict[str, Any]:
    target = _target(command)
    if target is None:
        raise ValueError("只有明确指定应用名称的 taskkill /IM、Stop-Process -Name 或 pkill -x 才能记住")
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
