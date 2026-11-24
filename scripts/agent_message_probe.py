#!/usr/bin/env python3
"""User-initiated connectivity probes over the local AgentRelay message bus."""
from __future__ import annotations

import uuid
from typing import Any

try:
    from .agent_message_bus import (
        ACTIVE_MESSAGE_STATES,
        MessageBus,
        MessageBusError,
        validate_address,
    )
except ImportError:
    from agent_message_bus import (
        ACTIVE_MESSAGE_STATES,
        MessageBus,
        MessageBusError,
        validate_address,
    )


MAX_TARGETS = 8
PROBE_TTL_SECONDS = 120


def _public_peer(peer: dict[str, Any]) -> dict[str, Any]:
    """Expose addressing and display data, never transport credentials."""
    return {
        key: peer.get(key)
        for key in (
            "address", "host", "session_id", "name", "workspace", "state",
            "capabilities", "adapter", "last_seen",
        )
    }


def list_peers(bus: MessageBus) -> list[dict[str, Any]]:
    """List registered sessions that are fresh enough to receive a real probe."""
    return [
        _public_peer(peer)
        for peer in bus.list_agents(include_offline=False)
        if not peer.get("stale")
    ]


def _message_ids(message_ids: list[str]) -> list[str]:
    if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= MAX_TARGETS:
        raise MessageBusError("message_ids must contain between 1 and 8 IDs")
    if any(not isinstance(value, str) for value in message_ids):
        raise MessageBusError("message_ids must be strings")
    if len(set(message_ids)) != len(message_ids):
        raise MessageBusError("Duplicate probe message ID")
    return message_ids


def _public_probe(message: dict[str, Any]) -> dict[str, Any]:
    status = str(message.get("status") or "")
    return {
        "message_id": message["message_id"],
        "task_id": message["task_id"],
        "from": message["from"],
        "to": message["to"],
        "status": status,
        "connected": status == "replied",
        "last_error": message.get("last_error") or "",
        "created_at": message.get("created_at"),
        "updated_at": message.get("updated_at"),
        "expires_at": message.get("expires_at"),
    }


def _result(probes: list[dict[str, Any]]) -> dict[str, Any]:
    connected = sum(item["connected"] for item in probes)
    pending = sum(item["status"] in ACTIVE_MESSAGE_STATES for item in probes)
    return {
        "probes": probes,
        "summary": {
            "total": len(probes),
            "connected": connected,
            "pending": pending,
            "failed": len(probes) - connected - pending,
        },
    }


def start_probe(bus: MessageBus, sender: str, targets: list[str]) -> dict[str, Any]:
    """Validate the complete fan-out before sending any correlated probe."""
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise MessageBusError("Probe targets must contain between 1 and 8 agents")
    source = validate_address(sender)
    recipients = [validate_address(target) for target in targets]
    if len(set(recipients)) != len(recipients):
        raise MessageBusError("Duplicate probe target")
    if source in recipients:
        raise MessageBusError("An agent cannot probe itself")

    live = {peer["address"]: peer for peer in list_peers(bus)}
    for address in (source, *recipients):
        if address not in live:
            raise MessageBusError(f"Agent is not live: {address}")

    probe_id = "probe_" + uuid.uuid4().hex
    message_ids = []
    for recipient in recipients:
        body = (
            "AgentRelay 配置页连通检测。不要修改文件、执行命令或扩大权限。"
            f"请仅以当前消息的 message_id 作为 reply_to 回复 PROBE_OK {probe_id}；"
            "不要继续追问或转发。"
        )
        message = bus.send(
            sender=source,
            recipient=recipient,
            body=body,
            kind="task",
            delivery="safe_boundary",
            context_mode="current",
            task_id=probe_id,
            review_action="new",
            review_reason="User explicitly started an Agent connectivity probe",
            ttl=PROBE_TTL_SECONDS,
            max_hops=2,
        )
        message_ids.append(message["message_id"])
    result = probe_status(bus, message_ids)
    result["probe_id"] = probe_id
    return result


def probe_status(bus: MessageBus, message_ids: list[str]) -> dict[str, Any]:
    """Return real bus receipts; delivery alone is never connectivity."""
    ids = _message_ids(message_ids)
    return _result([_public_probe(bus.get(message_id)) for message_id in ids])
