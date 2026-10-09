#!/usr/bin/env python3
"""AgentRelay Hook shim：为不同 Agent 适配 stdin 事件名与 stdout 输出协议。

用法（由安装器注册到各 Agent 的 Hook 配置中）：

    agentrelay_hook_shim.py --target hermes --event UserPromptSubmit

职责：
1. 读取 stdin 的 Hook payload，注入缺失的 hook_event_name（Hermes 的 payload
   不带事件名，事件来自注册时的 --event 参数）。
2. 调用主 Hook（$CODEX_HOME/hooks/agent_relay_hook.py）透传 payload。
3. 按目标协议翻译 stdout：Hermes 需要 {"context": ...}，其余原样透传。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main_hook_path() -> Path:
    override = os.environ.get("AGENTRELAY_MAIN_HOOK", "").strip()
    if override:
        return Path(override).expanduser()
    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    return codex_home / "hooks" / "agent_relay_hook.py"


def normalize_payload(payload_text: str, target: str, event: str) -> str:
    """按目标 Agent 补齐 payload 缺失字段，返回可直接传给主 Hook 的 JSON。"""
    try:
        payload = json.loads(payload_text) if payload_text.strip() else {}
    except json.JSONDecodeError:
        return payload_text
    if not isinstance(payload, dict):
        return payload_text

    has_event = any(
        payload.get(key)
        for key in ("hook_event_name", "hookEventName", "event", "event_name", "eventName")
    )
    if not has_event and event:
        payload["hook_event_name"] = event

    # Kimi Code 的 PostToolUseFailure 只带 error、不带 tool_output；
    # 主 Tracker 通过 tool_output 判定失败，这里把 error 桥接过去。
    if (
        target == "kimi"
        and str(payload.get("hook_event_name") or "") == "PostToolUseFailure"
        and payload.get("error") is not None
        and not any(payload.get(key) for key in ("tool_output", "toolOutput", "output", "result", "tool_response", "toolResponse"))
    ):
        payload["tool_output"] = payload["error"]

    return json.dumps(payload, ensure_ascii=False)


def inject_event(payload_text: str, event: str) -> str:
    """payload 缺少事件名时，用注册时的事件补齐（Hermes 场景）。"""
    if not event:
        return payload_text
    try:
        payload = json.loads(payload_text) if payload_text.strip() else {}
    except json.JSONDecodeError:
        return payload_text
    if not isinstance(payload, dict):
        return payload_text
    has_event = any(
        payload.get(key)
        for key in ("hook_event_name", "hookEventName", "event", "event_name", "eventName")
    )
    if not has_event:
        payload["hook_event_name"] = event
    return json.dumps(payload, ensure_ascii=False)


def translate_output(stdout_text: str, target: str) -> str:
    """把 Tracker 输出翻译为目标 Agent 的协议。"""
    if target != "hermes" or not stdout_text.strip():
        return stdout_text
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        return stdout_text
    if not isinstance(payload, dict):
        return stdout_text
    output = payload.get("hookSpecificOutput")
    if isinstance(output, dict) and isinstance(output.get("additionalContext"), str):
        return json.dumps(
            {"context": output["additionalContext"]}, ensure_ascii=False
        )
    # Stop 的 {"decision": "block", "reason": ...} 与 Hermes 兼容，原样透传。
    return stdout_text


def render_for_target(stdout_text: str, target: str) -> tuple[str, str, int]:
    """返回 (stdout, stderr, exit_code)。

    Kimi Code 的约定：Stop 事件用退出码 2 + stderr 作为阻断原因写回上下文；
    其余事件的 stdout 文本只进入 transcript，不会发给模型，因此非阻断输出直接丢弃，
    避免把 JSON 原文当普通文本展示给用户。
    """
    if target != "kimi":
        return translate_output(stdout_text, target), "", 0
    if not stdout_text.strip():
        return "", "", 0
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        return stdout_text, "", 0
    if not isinstance(payload, dict):
        return stdout_text, "", 0
    if payload.get("decision") == "block" and isinstance(payload.get("reason"), str):
        return "", payload["reason"], 2
    return "", "", 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentRelay Hook shim")
    parser.add_argument("--target", default="", help="目标 Agent，例如 hermes、pi")
    parser.add_argument("--event", default="", help="payload 缺失时注入的事件名")
    args = parser.parse_args(argv)

    hook = main_hook_path()
    raw = sys.stdin.read()
    if not hook.is_file():
        # Hook 缺失时静默放行，绝不阻塞宿主 Agent。
        return 0

    payload = normalize_payload(raw, args.target, args.event)
    env = dict(os.environ)
    if args.target:
        env["AGENTRELAY_TARGET"] = args.target
    try:
        result = subprocess.run(
            [sys.executable, str(hook)],
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=12,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    stdout_text, stderr_text, exit_code = render_for_target(
        result.stdout, args.target
    )
    if stdout_text.strip():
        sys.stdout.write(stdout_text.rstrip() + "\n")
        sys.stdout.flush()
    if stderr_text.strip():
        sys.stderr.write(stderr_text.rstrip() + "\n")
        sys.stderr.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
