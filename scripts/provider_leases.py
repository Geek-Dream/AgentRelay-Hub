"""Cross-process Provider concurrency leases with FIFO waiting and recovery.

The lease covers one model request, not an entire Fusion task. State lives in
small JSON files under ``$CODEX_HOME`` so independent Codex sessions can share
the same Provider limits without a permanently running broker.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl
    msvcrt = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def default_lease_directory(home: Path | None = None) -> Path:
    base = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        if home is None
        else home.expanduser()
    )
    return base / "skills" / "agent-relay" / "runtime" / "provider-leases"


def _provider_key(provider_id: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", provider_id.strip().lower()).strip("-")
    slug = slug[:48] or "provider"
    digest = hashlib.sha256(provider_id.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
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
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


class ProviderLeaseTimeout(TimeoutError):
    """The Provider queue did not grant a slot before its deadline."""

    def __init__(self, provider_id: str, status: Mapping[str, Any]):
        self.provider_id = provider_id
        self.status = dict(status)
        super().__init__(
            f"Provider {provider_id} 排队超时："
            f"{status.get('active', 0)}/{status.get('limit', 0)} 使用中，"
            f"{status.get('queued', 0)} 个请求等待"
        )


@dataclass(frozen=True)
class ProviderLease:
    provider_id: str
    lease_id: str
    slot: int
    manager: "ProviderLeaseManager"


class ProviderLeaseManager:
    """Coordinate a fixed number of slots for one Provider."""

    def __init__(
        self,
        provider_id: str,
        *,
        kind: str = "",
        limit: int = 1,
        queue_timeout: float = 30,
        lease_ttl: float = 90,
        directory: str | Path | None = None,
        poll_interval: float = 0.1,
    ):
        self.provider_id = str(provider_id or "").strip()
        if not self.provider_id:
            raise ValueError("provider_id 不能为空")
        self.kind = str(kind or "").strip().lower()
        self.limit = max(1, min(100, int(limit)))
        self.queue_timeout = max(0.0, float(queue_timeout))
        self.lease_ttl = max(5.0, float(lease_ttl))
        self.directory = Path(directory) if directory else default_lease_directory()
        self.poll_interval = max(0.01, float(poll_interval))
        self.path = self.directory / f"{_provider_key(self.provider_id)}.json"
        self.lock_path = self.path.with_suffix(".lock")

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "provider_id": self.provider_id,
            "kind": self.kind,
            "limit": self.limit,
            "lease_ttl": self.lease_ttl,
            "next_ticket": 1,
            "active": {},
            "queue": [],
            "updated_at": _now(),
        }

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(value, dict):
            return self._empty_state()
        value.setdefault("version", 1)
        value.setdefault("provider_id", self.provider_id)
        value.setdefault("kind", self.kind)
        value.setdefault("next_ticket", 1)
        value.setdefault("active", {})
        value.setdefault("queue", [])
        if not isinstance(value.get("active"), dict):
            value["active"] = {}
        if not isinstance(value.get("queue"), list):
            value["queue"] = []
        value["limit"] = self.limit
        value["lease_ttl"] = self.lease_ttl
        return value

    def _recover(self, value: dict[str, Any]) -> bool:
        changed = False
        now = time.time()
        active = value.get("active", {})
        for lease_id, entry in list(active.items()):
            if not isinstance(entry, dict):
                active.pop(lease_id, None)
                changed = True
                continue
            pid = int(entry.get("pid") or 0)
            heartbeat = _parse_time(entry.get("heartbeat_at"))
            if not _pid_alive(pid) or now - heartbeat > self.lease_ttl:
                active.pop(lease_id, None)
                changed = True
        queue = value.get("queue", [])
        kept = []
        for entry in queue:
            if not isinstance(entry, dict):
                changed = True
                continue
            if not _pid_alive(int(entry.get("pid") or 0)):
                changed = True
                continue
            kept.append(entry)
        if kept != queue:
            value["queue"] = kept
            changed = True
        value["queue"].sort(key=lambda item: int(item.get("ticket") or 0))
        return changed

    def _mutate(self, callback):
        with _file_lock(self.lock_path):
            value = self._load()
            changed = self._recover(value)
            result = callback(value)
            value["updated_at"] = _now()
            if result is not None or changed:
                _atomic_write(self.path, value)
            return result

    def _public_status(self, value: Mapping[str, Any]) -> dict[str, Any]:
        active = value.get("active") if isinstance(value.get("active"), dict) else {}
        queue = value.get("queue") if isinstance(value.get("queue"), list) else []
        active_slots = sorted(
            int(item.get("slot") or 0)
            for item in active.values()
            if isinstance(item, dict)
        )
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "active": len(active),
            "limit": self.limit,
            "queued": len(queue),
            "active_slots": active_slots,
            "degraded": bool(queue),
            "fallback_reason": "PROVIDER_QUEUE_ACTIVE" if queue else "",
        }

    def status(self) -> dict[str, Any]:
        return self._mutate(lambda value: self._public_status(value))

    def _register_waiter(self, value: dict[str, Any], lease_id: str) -> dict[str, Any]:
        queue = value.setdefault("queue", [])
        for entry in queue:
            if isinstance(entry, dict) and entry.get("lease_id") == lease_id:
                return entry
        ticket = int(value.get("next_ticket") or 1)
        value["next_ticket"] = ticket + 1
        entry = {
            "lease_id": lease_id,
            "ticket": ticket,
            "pid": os.getpid(),
            "created_at": _now(),
        }
        queue.append(entry)
        queue.sort(key=lambda item: int(item.get("ticket") or 0))
        return entry

    def _try_grant(self, value: dict[str, Any], lease_id: str) -> int | None:
        active = value.setdefault("active", {})
        queue = value.setdefault("queue", [])
        if queue and queue[0].get("lease_id") != lease_id:
            return None
        if len(active) >= self.limit:
            return None
        used_slots = {
            int(item.get("slot") or 0)
            for item in active.values()
            if isinstance(item, dict)
        }
        slot = next(
            (candidate for candidate in range(1, self.limit + 1)
             if candidate not in used_slots),
            None,
        )
        if slot is None:
            return None
        value["queue"] = [
            entry for entry in queue
            if not (isinstance(entry, dict) and entry.get("lease_id") == lease_id)
        ]
        now = _now()
        active[lease_id] = {
            "slot": slot,
            "pid": os.getpid(),
            "created_at": now,
            "heartbeat_at": now,
        }
        return slot

    def acquire(self, *, wait_timeout: float | None = None) -> ProviderLease:
        lease_id = uuid.uuid4().hex
        timeout = self.queue_timeout if wait_timeout is None else max(0.0, float(wait_timeout))
        deadline = time.monotonic() + timeout
        while True:
            def attempt(value: dict[str, Any]) -> dict[str, int | None]:
                self._register_waiter(value, lease_id)
                return {"slot": self._try_grant(value, lease_id)}

            slot = self._mutate(attempt)["slot"]
            if slot is not None:
                return ProviderLease(self.provider_id, lease_id, slot, self)
            if time.monotonic() >= deadline:
                status = self._mutate(lambda value: self._public_status(value))
                self._mutate(
                    lambda value: {"removed": self._remove_waiter(value, lease_id)}
                )
                raise ProviderLeaseTimeout(self.provider_id, status)
            time.sleep(self.poll_interval)

    @staticmethod
    def _remove_waiter(value: dict[str, Any], lease_id: str) -> bool:
        queue = value.get("queue", [])
        kept = [
            entry for entry in value.get("queue", [])
            if not (isinstance(entry, dict) and entry.get("lease_id") == lease_id)
        ]
        value["queue"] = kept
        return kept != queue

    def renew(self, lease: ProviderLease) -> bool:
        def update(value: dict[str, Any]) -> bool:
            entry = value.get("active", {}).get(lease.lease_id)
            if not isinstance(entry, dict):
                return False
            entry["heartbeat_at"] = _now()
            return True

        return bool(self._mutate(update))

    def release(self, lease: ProviderLease) -> bool:
        def remove(value: dict[str, Any]) -> bool:
            existed = value.get("active", {}).pop(lease.lease_id, None) is not None
            removed = self._remove_waiter(value, lease.lease_id)
            return existed or removed

        return bool(self._mutate(remove))

    @contextmanager
    def lease(
        self,
        *,
        wait_timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[ProviderLease]:
        acquired = self.acquire(wait_timeout=wait_timeout)
        stopped = threading.Event()

        def heartbeat() -> None:
            interval = max(1.0, self.lease_ttl / 3.0)
            while not stopped.wait(interval):
                try:
                    if not self.renew(acquired):
                        return
                except OSError:
                    return

        thread = threading.Thread(
            target=heartbeat,
            name=f"agentrelay-lease-{self.provider_id}",
            daemon=True,
        )
        if metadata:
            def add_metadata(value: dict[str, Any]) -> bool:
                entry = value.get("active", {}).get(acquired.lease_id)
                if isinstance(entry, dict):
                    entry["metadata"] = {
                        str(key): str(item)
                        for key, item in metadata.items()
                    }
                    return True
                return False
            self._mutate(add_metadata)
        thread.start()
        try:
            yield acquired
        finally:
            stopped.set()
            thread.join(timeout=1.0)
            self.release(acquired)
