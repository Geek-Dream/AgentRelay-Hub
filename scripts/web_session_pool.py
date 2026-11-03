"""Stable numbered web conversation slots shared across local Agent processes."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

try:
    from . import config_manager, provider_leases
    from .agent_relay_runtime import SessionRef
except ImportError:
    import config_manager
    import provider_leases
    from agent_relay_runtime import SessionRef


def web_provider_pool_id(provider_id: str) -> str:
    """Use the same pool key for aliases such as deepseek and deepseek-web."""
    return str(provider_id or "").strip().lower().removesuffix("-web")


def session_pool_title(provider_id: str, slot: int) -> str:
    pool_id = web_provider_pool_id(provider_id)
    return f"AgentRelay-{config_manager.provider_title_stem(pool_id)}-{int(slot)}"


def session_pool_scope(provider_id: str, slot: int, business: str = "default") -> str:
    provider = web_provider_pool_id(provider_id)
    normalized_business = str(business or "default").strip().lower() or "default"
    return f"pool:{provider}:{normalized_business}:{int(slot)}"


def find_pool_session(
    sessions: Iterable[SessionRef],
    title: str,
    session_id: str = "",
) -> SessionRef | None:
    wanted_session_id = str(session_id or "").strip()
    if wanted_session_id:
        for session in sessions:
            if str(session.session_id or "") == wanted_session_id:
                return session
        return None
    wanted = " ".join(str(title or "").split()).casefold()
    if not wanted:
        return None
    for session in sessions:
        normalized = " ".join(str(session.title or "").split()).casefold()
        if normalized == wanted:
            return session
    return None


@dataclass(frozen=True)
class WebSessionSlot:
    provider_id: str
    slot: int
    title: str
    binding_scope: str
    lease: provider_leases.ProviderLease
    business: str


class WebSessionPool:
    """Use numbered, isolated web conversations with one lease per slot."""

    def __init__(
        self,
        provider_id: str,
        *,
        limit: int = 1,
        queue_timeout: float = 30,
        lease_ttl: float = 90,
        home: Path | None = None,
        business: str = "default",
    ):
        self.provider_id = web_provider_pool_id(provider_id)
        self.limit = max(1, min(100, int(limit)))
        self.business = str(business or "default").strip().lower() or "default"
        self.manager = provider_leases.ProviderLeaseManager(
            f"web-session:{self.provider_id}",
            kind="web",
            limit=self.limit,
            queue_timeout=queue_timeout,
            lease_ttl=lease_ttl,
            directory=provider_leases.default_lease_directory(home) / "web-sessions",
        )
        self.owners_path = self.manager.path.with_suffix(".owners.json")
        self.owners_lock_path = self.owners_path.with_suffix(".lock")

    def _empty_owner_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "provider_id": self.provider_id,
            "slots": {},
            "legacy_adoption_consumed": False,
            "updated_at": "",
        }

    def _load_owner_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.owners_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return self._empty_owner_state()
        if not isinstance(value, dict):
            return self._empty_owner_state()
        value.setdefault("version", 1)
        value.setdefault("provider_id", self.provider_id)
        if not isinstance(value.get("slots"), dict):
            value["slots"] = {}
        value.setdefault("legacy_adoption_consumed", False)
        value.setdefault("updated_at", "")
        return value

    def _mutate_owner_state(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        with provider_leases._file_lock(self.owners_lock_path):
            value = self._load_owner_state()
            result = callback(value)
            value["updated_at"] = datetime.now(timezone.utc).isoformat()
            provider_leases._atomic_write(self.owners_path, value)
            return result

    def slot_owner_record(self, slot: int) -> dict[str, str]:
        value = self._load_owner_state()
        record = value.get("slots", {}).get(str(int(slot)))
        if not isinstance(record, Mapping):
            return {}
        return {
            "business": str(record.get("business") or ""),
            "session_id": str(record.get("session_id") or ""),
            "href": str(record.get("href") or ""),
        }

    def slot_owner(self, slot: int) -> str:
        return self.slot_owner_record(slot).get("business", "")

    def set_slot_owner(
        self,
        slot: int,
        *,
        session_id: str = "",
        href: str = "",
    ) -> None:
        def update(value: dict[str, Any]) -> None:
            slots = value.setdefault("slots", {})
            slots[str(int(slot))] = {
                "business": self.business,
                "session_id": str(session_id or ""),
                "href": str(href or ""),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        self._mutate_owner_state(update)

    def legacy_adoption_available(self) -> bool:
        return not bool(self._load_owner_state().get("legacy_adoption_consumed"))

    def consume_legacy_adoption(self) -> bool:
        def update(value: dict[str, Any]) -> bool:
            if value.get("legacy_adoption_consumed"):
                return False
            value["legacy_adoption_consumed"] = True
            return True

        return bool(self._mutate_owner_state(update))

    @contextmanager
    def claim(self) -> Iterator[WebSessionSlot]:
        with self.manager.lease(metadata={
            "provider": self.provider_id,
            "business": self.business,
        }) as lease:
            title = session_pool_title(self.provider_id, lease.slot)
            yield WebSessionSlot(
                provider_id=self.provider_id,
                slot=lease.slot,
                title=title,
                binding_scope=session_pool_scope(
                    self.provider_id,
                    lease.slot,
                    self.business,
                ),
                lease=lease,
                business=self.business,
            )

    def status(self) -> dict[str, object]:
        return self.manager.status()
