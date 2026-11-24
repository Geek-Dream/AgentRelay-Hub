#!/usr/bin/env python3
"""Native transport adapters for the local AgentRelay message bus."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class DeliveryError(RuntimeError):
    """A native host transport could not accept a prepared message."""


def _claude_socket(
    endpoint: Mapping[str, Any],
    message: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
        raise DeliveryError("Claude named-pipe delivery is not implemented; leave the message queued")
    raw_path = str(endpoint.get("socket_path") or "").strip()
    if raw_path.startswith("uds:"):
        raw_path = raw_path[4:]
    if not raw_path:
        raise DeliveryError("Claude registration has no messaging socket")
    path = Path(raw_path).expanduser()
    if path.is_symlink():
        raise DeliveryError("Refusing a symlinked Claude messaging socket")

    records = []
    token = str(endpoint.get("token") or "")
    if token:
        records.append({"type": "auth", "token": token})
    records.append({
        "type": "user",
        "message": {"role": "user", "content": str(message["envelope"])},
    })
    payload = b"".join(
        json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        for record in records
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            connection.sendall(payload)
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                # The host may close immediately after consuming complete lines.
                pass
    except OSError as exc:
        raise DeliveryError(f"Claude inbox socket submission failed: {exc}") from exc
    return {
        "status": "dispatched",
        "transport": "claude_socket",
        "detail": "Written to the Claude inbox socket; model receipt is not yet acknowledged",
    }


def _opencode_http(
    endpoint: Mapping[str, Any],
    message: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    base_url = str(endpoint.get("base_url") or "").strip().rstrip("/")
    session_id = str(endpoint.get("session_id") or "").strip()
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise DeliveryError("OpenCode delivery only accepts a loopback HTTP server")
    if not session_id:
        raise DeliveryError("OpenCode registration has no session_id")
    body = {
        "id": str(message["message_id"]),
        "prompt": {"text": str(message["envelope"])},
        "delivery": "queue" if message.get("delivery") == "when_idle" else "steer",
        "resume": True,
    }
    request = Request(
        f"{base_url}/api/session/{quote(session_id, safe='')}/prompt",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            status = int(response.status)
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        raise DeliveryError(f"OpenCode HTTP {exc.code}: {detail}") from exc
    except (URLError, OSError) as exc:
        raise DeliveryError(f"OpenCode prompt submission failed: {exc}") from exc
    if status < 200 or status >= 300:
        raise DeliveryError(f"OpenCode returned HTTP {status}")
    try:
        response_body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise DeliveryError("OpenCode returned invalid JSON") from exc
    host_message = response_body.get("data") if isinstance(response_body, dict) else None
    if not isinstance(host_message, dict) or not host_message.get("id"):
        raise DeliveryError("OpenCode response did not contain a durable message receipt")
    return {
        "status": "delivered",
        "transport": "opencode_http",
        "detail": "Accepted by the durable OpenCode session inbox",
        "host_message_id": str(host_message["id"]),
    }


def deliver(
    registration: Mapping[str, Any],
    message: Mapping[str, Any],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Submit one prepared envelope using the recipient's native host API."""
    adapter = str(registration.get("adapter") or "pull")
    endpoint = registration.get("endpoint") or {}
    if not isinstance(endpoint, Mapping):
        raise DeliveryError("Recipient endpoint must be an object")
    if adapter == "claude_socket":
        return _claude_socket(endpoint, message, timeout)
    if adapter == "opencode_http":
        return _opencode_http(endpoint, message, timeout)
    if adapter == "pull":
        return {
            "status": "queued",
            "transport": "pull",
            "detail": "The recipient will pull this message at its next safe host event",
        }
    raise DeliveryError(f"Unsupported message adapter: {adapter}")
