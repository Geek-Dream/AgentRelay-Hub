"""Explicit, revocable permission to open or close one verified application."""
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


def _entry_allows(entry: dict[str, Any], capability: str) -> bool:
    """Read one capability, keeping legacy close-only entries valid."""
    if not isinstance(entry, dict):
        return False
    if "allow_close" not in entry and "allow_open" not in entry:
        return capability == "close"
    return bool(entry.get(f"allow_{capability}", False))


def _save_permissions(codex_home: Path, data: dict[str, Any]) -> None:
    path = permission_path(codex_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _upsert(
    target: str,
    codex_home: Path,
    *,
    close: bool | None,
    open_app: bool | None,
    background_only: bool | None,
) -> dict[str, Any]:
    data = load_permissions(codex_home)
    fingerprint = hashlib.sha256(target.encode("utf-8")).hexdigest()
    entry = next(
        (
            item for item in data["applications"]
            if isinstance(item, dict) and item.get("fingerprint") == fingerprint
        ),
        None,
    )
    if entry is None:
        entry = {
            "target": target,
            "fingerprint": fingerprint,
            "allow_close": False,
            "allow_open": False,
            "background_only": True,
        }
        data["applications"].append(entry)
    if close is not None:
        entry["allow_close"] = bool(close)
    if open_app is not None:
        entry["allow_open"] = bool(open_app)
    if background_only is not None:
        entry["background_only"] = bool(background_only)
    if not entry.get("allow_close") and not entry.get("allow_open"):
        data["applications"] = [
            item for item in data["applications"]
            if not (isinstance(item, dict) and item.get("fingerprint") == fingerprint)
        ]
    _save_permissions(codex_home, data)
    return entry


def _words(command: str) -> list[str] | None:
    """Parse only one literal command; never authorize a shell chain."""
    text = str(command or "").strip()
    if not text or re.search(r"(?:&&|\|\||[;|<>`\n\r])", text):
        return None
    # An escaped space is the same app name as a quoted one; fold it into a
    # placeholder so tokenization keeps `Google Chrome` as a single operand.
    sentinel = "\x00"
    text = text.replace("\\ ", sentinel)
    try:
        words = shlex.split(text, posix=False)
    except ValueError:
        return None
    return [
        word.strip('"\'').replace(sentinel, " ")
        for word in words
    ] or None


def _normalized_target(value: str) -> str:
    """Collapse quoting and spacing so one app name has exactly one fingerprint."""
    text = str(value or "").strip()
    text = text.strip('"').strip("'").strip()
    text = re.sub(r"\\(.)", r"\1", text)
    return text.strip().casefold()


def _valid_target(value: str) -> str | None:
    target = _normalized_target(value)
    if not target or re.search(r"[*?\[\]{};|&/\\]", target):
        return None
    if target in {
        "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
        "winlogon.exe", "services.exe", "lsass.exe", "explorer.exe",
        "kernel", "launchd", "windowserver", "loginwindow",
    }:
        return None
    return target


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
    elif command_name == "killall":
        # macOS/BSD: `killall Slack` or `killall -TERM Slack`; the last bare word
        # is the app name. A signal option is accepted, a pattern is not.
        operands = [
            word for word in words[1:]
            if not word.startswith("-")
        ]
        flags = [word for word in words[1:] if word.startswith("-")]
        if len(operands) != 1:
            return None
        if flags and not all(
            flag in {"-TERM", "-term", "-KILL", "-kill", "-HUP", "-hup", "-INT", "-int", "-q"}
            for flag in flags
        ):
            return None
        target = operands[0]
    else:
        return None
    return _valid_target(target or "")


def can_remember(command: str) -> bool:
    return _target(command) is not None


def can_remember_open(command: str) -> bool:
    """Whether an `open`-style invocation names one unambiguous application."""
    return app_target(command) is not None


def app_target(command: str) -> str | None:
    """Return the app named by an open/launch command, never a URL."""
    words = _words(command)
    if not words:
        return None
    name = Path(words[0]).name.lower()
    if name not in {"open", "xdg-open"}:
        return None
    arguments = words[1:]
    if name == "xdg-open":
        operands = [word for word in arguments if not word.startswith("-")]
        if len(operands) != 1:
            return None
        value = operands[0].strip()
        if value.startswith(("http://", "https://", "file://")):
            return None
        return _valid_target(Path(value).stem)

    target = None
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in {"-a", "-b"}:
            if index + 1 >= len(arguments):
                return None
            if target is not None:
                return None
            target = arguments[index + 1]
            index += 2
            continue
        if word in {"-g", "--background", "-n", "--no-focus"}:
            index += 1
            continue
        return None
    return _valid_target(Path(str(target)).stem) if target else None


def remember(command: str, codex_home: Path) -> dict[str, Any]:
    """Remember close permission for one explicit app-close command."""
    target = _target(command)
    if target is None:
        raise ValueError(
            "只有明确指定应用名称的 taskkill /IM、Stop-Process -Name、"
            "pkill -x 或 killall 才能记住"
        )
    return _upsert(target, codex_home, close=True, open_app=None, background_only=None)


def remember_open(command: str, codex_home: Path, *, background_only: bool = True) -> dict[str, Any]:
    """Remember open permission for one app, background-only unless stated."""
    target = app_target(command)
    if target is None:
        raise ValueError(
            "只有明确 -a 指定应用名的 open 命令才能记住；打开网址不记录"
        )
    return _upsert(
        target,
        codex_home,
        close=None,
        open_app=True,
        background_only=background_only,
    )


def allows(command: str, codex_home: Path, *, capability: str = "close") -> bool:
    target = _target(command) if capability == "close" else app_target(command)
    if target is None:
        return False
    fingerprint = hashlib.sha256(target.encode("utf-8")).hexdigest()
    return any(
        isinstance(item, dict)
        and item.get("fingerprint") == fingerprint
        and _entry_allows(item, capability)
        for item in load_permissions(codex_home)["applications"]
    )


def open_background_only(target: str, codex_home: Path) -> bool:
    """A remembered app may still be launch-restricted to not steal focus."""
    normalized = _normalized_target(target)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    for item in load_permissions(codex_home)["applications"]:
        if (
            isinstance(item, dict)
            and item.get("fingerprint") == fingerprint
            and _entry_allows(item, "open")
        ):
            return bool(item.get("background_only", True))
    return True


def forget(target: str, codex_home: Path) -> bool:
    data = load_permissions(codex_home)
    normalized = _normalized_target(target)
    remaining = [
        item for item in data["applications"]
        if _normalized_target(str(item.get("target") or "")) != normalized
    ]
    changed = len(remaining) != len(data["applications"])
    if changed:
        data["applications"] = remaining
        _save_permissions(codex_home, data)
    return changed
