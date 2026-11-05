"""Atomic, locked state storage shared by page and visual verification."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


T = TypeVar("T")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def state_path(home: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    if not safe:
        raise ValueError("verification requires a session id")
    return (
        home.expanduser().resolve()
        / "agent_relay_tracker"
        / "page-verification"
        / f"{safe}.json"
    )


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def session_lock(home: Path, session_id: str):
    path = state_path(home, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".json.lock")
    with _thread_lock(lock_path):
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows fallback
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows fallback
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def read_state_unlocked(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state_unlocked(path: Path, state: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def read_state(home: Path, session_id: str) -> dict[str, Any]:
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        return read_state_unlocked(path)


def write_state(home: Path, session_id: str, state: dict[str, Any]) -> Path:
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        return write_state_unlocked(path, state)


def mutate_state(
    home: Path,
    session_id: str,
    mutator: Callable[[dict[str, Any]], T],
) -> T:
    """Apply one locked read-modify-write transaction."""
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        state = read_state_unlocked(path)
        result = mutator(state)
        write_state_unlocked(path, state)
        return result
