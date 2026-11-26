"""自动化授权的询问颗粒度。

用户原话对应的规则：
  - 影响用户的操作必须提前通知，这是 Hook 级别的硬要求；
  - 但"每次都要问"是看情况的：当前会话里刚声明过、没有撤回、参数没变的
    同一个程序，就不该反复弹出来问一遍；
  - 换会话、换功能、换目标、换类型，都要重新告知。

"已经通知过"和"已经同意"是两件事，分开存：通知过不等于同意，不能因为刚弹过
一条通知就把门禁放过去。

三级授权：
  session     当前会话内有效，换会话重新问
  task        当前任务内有效，任务结束失效
  persistent  跨会话有效，只用于用户明确说过"这个程序所有的东西都不用问"；
              即使如此，换会话/换功能仍要通知（只是不拦下来等确认）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


SCOPES = {"session", "task", "persistent"}
REUSE_SECONDS = 7200
ANNOUNCE_SECONDS = 600
STATE_KEY = "automation_consent"
DECLARED = "declared"
ANNOUNCED = "announced"


def notifications_enabled(config: Any) -> bool:
    """Optional system popups are off by default and never replace approval."""
    if not isinstance(config, dict):
        return False
    section = config.get("notifications")
    if isinstance(section, dict):
        return section.get("enabled") is True
    if isinstance(section, bool):
        return section
    return False


def announcement(request: Any) -> str:
    """The one-sentence prior notice the user must see before automation."""
    if not isinstance(request, dict):
        return "即将执行自动化操作。"
    notice = str(request.get("notice") or "").strip()
    if notice:
        return notice
    return "即将执行自动化操作。"


def _bucket(state: Any, name: str) -> dict[str, Any] | None:
    """Return one mutable sub-bucket, or None when state cannot hold one."""
    if not isinstance(state, dict):
        return None
    root = state.get(STATE_KEY)
    if not isinstance(root, dict):
        root = {}
        state[STATE_KEY] = root
    bucket = root.get(name)
    if not isinstance(bucket, dict):
        bucket = {}
        root[name] = bucket
    return bucket


def _key(agent: str, kind: str, target: str) -> str:
    return "\0".join((
        str(agent or ""),
        str(kind or ""),
        str(target or "").strip().lower(),
    ))


def _age_seconds(stamp: Any, now: str) -> float | None:
    """Return the age in seconds, or None when either side is unusable."""

    def parse(value: Any):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    start = parse(stamp)
    end = parse(now)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def record_declaration(
    state: Any,
    agent: str,
    kind: str,
    target: str,
    now: str,
    session: str = "",
    *,
    scope: str = "session",
    persistent: bool = False,
) -> None:
    """Remember that the user already agreed to this exact operation."""
    bucket = _bucket(state, DECLARED)
    if bucket is None:
        return
    if persistent:
        scope = "persistent"
    if scope not in SCOPES:
        scope = "session"
    bucket[_key(agent, kind, target)] = {
        "scope": scope,
        "session": str(session or ""),
        "declared_at": str(now or ""),
    }


def revoke(state: Any, agent: str, kind: str, target: str) -> bool:
    """Withdraw a previous declaration; the next attempt asks again."""
    bucket = _bucket(state, DECLARED)
    if bucket is None:
        return False
    return bucket.pop(_key(agent, kind, target), None) is not None


def _declared(state: Any, agent: str, kind: str, target: str) -> dict[str, Any] | None:
    bucket = _bucket(state, DECLARED)
    if bucket is None:
        return None
    entry = bucket.get(_key(agent, kind, target))
    return entry if isinstance(entry, dict) else None


def should_ask(
    state: Any,
    agent: str,
    kind: str,
    target: str,
    now: str,
    session: str = "",
) -> bool:
    """Whether this operation still needs a fresh user decision."""
    entry = _declared(state, agent, kind, target)
    if entry is None:
        return True
    scope = str(entry.get("scope") or "session")
    if scope == "persistent":
        return False
    if str(entry.get("session") or "") != str(session or ""):
        return True
    age = _age_seconds(entry.get("declared_at"), now)
    if age is None or age > REUSE_SECONDS:
        return True
    return False


def must_announce(
    state: Any,
    agent: str,
    kind: str,
    target: str,
    now: str,
    session: str = "",
) -> bool:
    """Whether the user must be told before this operation runs.

    A persistent declaration removes the question, never the notice: a new
    session or a new feature still gets one heads-up.
    """
    bucket = _bucket(state, ANNOUNCED)
    if bucket is None:
        return True
    entry = bucket.get(_key(agent, kind, target))
    if not isinstance(entry, dict):
        return True
    if str(entry.get("session") or "") != str(session or ""):
        return True
    age = _age_seconds(entry.get("announced_at"), now)
    if age is None or age > ANNOUNCE_SECONDS:
        return True
    return False


def mark_announced(
    state: Any,
    agent: str,
    kind: str,
    target: str,
    now: str,
    session: str = "",
) -> None:
    """Remember the notice so the same heads-up is not repeated to death.

    This never grants consent: announcements live in their own bucket.
    """
    bucket = _bucket(state, ANNOUNCED)
    if bucket is None:
        return
    bucket[_key(agent, kind, target)] = {
        "session": str(session or ""),
        "announced_at": str(now or ""),
    }
