"""Shared, portable symbol layout for every user-visible AgentRelay card."""
from __future__ import annotations

import unicodedata
from collections.abc import Iterable


def display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in str(value))


def values(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value if str(item)] or ["无"]
    else:
        raw = [str(value)] if value not in (None, "") else ["无"]
    return [line for item in raw for line in (item.splitlines() or [""])]


def frame(title: str) -> list[str]:
    return [f"╭─ ◎  「{title}」", "│"]


def field(label: str, value, width: int | None = None, *, section: bool = False) -> list[str]:
    items = values(value)
    label_width = width if width is not None else display_width(label)
    padding = " " * max(0, label_width - display_width(label))
    prefix = f"│     - {label}：{padding} " if section else f"│  -| {label}："
    lines = [prefix + items[0]]
    indent = label_width + 5 if section else label_width + 6
    lines.extend("│" + " " * indent + item for item in items[1:])
    return lines


def section(title: str, fields: Iterable[tuple[str, object]]) -> list[str]:
    visible = [(label, value) for label, value in fields
               if value not in (None, "", (), []) or label in {"最近错误", "恢复原因"}]
    if not visible:
        return []
    width = max(display_width(label) for label, _ in visible)
    lines = ["│", f"│  ┄ {title} ▼"]
    for label, value in visible:
        lines.extend(field(label, value, width, section=True))
    return lines


def finish(lines: list[str], status: str | None = None) -> str:
    if status:
        lines.extend(["│", f"│  ◇ {status}"])
    lines.extend(["│", "╰" + "─" * 87 + " §"])
    return "\n".join(lines)
