#!/usr/bin/env python3
"""AgentRelay Fusion: a persistent sidekick with optional coding tools."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
_RELAY_SCRIPT_DIR = SCRIPT_DIR.parent.parent / "agent-relay" / "scripts"
for _candidate in (SCRIPT_DIR, _RELAY_SCRIPT_DIR):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

try:
    from . import config_manager, fusion_mode, fusion_runner  # noqa: E402
except ImportError:
    import config_manager  # noqa: E402
    import fusion_mode  # noqa: E402
    import fusion_runner  # noqa: E402


RESULT_PREFIX = "AGENT_FUSION_RESULT="
CODING_WARNING = (
    "coding 模式允许 Fusion 修改工作区文件并执行 shell 命令。"
    "只有在你确认要委派实际修改和命令执行时才启用。"
)


def _emit(result: Mapping[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(dict(result), ensure_ascii=False, sort_keys=True))


def _error(status: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "error": code,
        "message": message,
        **extra,
    }


def _update_fusion(home: Path | None, changes: Mapping[str, Any]) -> dict[str, Any]:
    current = config_manager.load_config(home)
    fusion = fusion_mode.normalize_fusion_config(current.get("fusion"))
    fusion.update(changes)
    config_manager.update_config({"fusion": fusion}, home)
    return fusion_mode.fusion_state(home=home)


def _api_key(entry: Mapping[str, Any]) -> str:
    direct = str(entry.get("api_key") or "")
    if direct:
        return direct
    env_name = str(entry.get("api_key_env") or "").strip()
    return os.environ.get(env_name, "") if env_name else ""


def _assistant_message(response: Mapping[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    return dict(message) if isinstance(message, dict) else {}


def _chat_answer(response: Mapping[str, Any]) -> str:
    message = _assistant_message(response)
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text") or "") for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    return str(content or "").strip()


def _call_chat_model(
    entry: Mapping[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    try:
        from .agent_adapter import OpenAICompatibleAdapter
    except ImportError:
        from agent_adapter import OpenAICompatibleAdapter
    adapter = OpenAICompatibleAdapter(
        endpoint=str(entry.get("endpoint") or ""),
        model=str(entry.get("model") or ""),
        timeout=timeout,
        api_key=_api_key(entry) or None,
    )
    payload: dict[str, Any] = {
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    response = adapter.send(payload)
    if not isinstance(response, dict) or not _assistant_message(response):
        raise RuntimeError("Provider 返回了无效的 Chat Completions 响应")
    return response


def _call_formatted_model(
    entry: Mapping[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    """Return a Chat Completions-shaped response for every supported API format."""
    try:
        from . import api_format_router as router
    except ImportError:
        import api_format_router as router
    fmt = str(entry.get("format") or router.FMT_CHAT).strip().lower()
    if fmt == router.FMT_CHAT:
        return _call_chat_model(entry, messages, tools, timeout)
    if fmt not in router.SERIALIZERS:
        raise RuntimeError(f"不支持的 API 消息格式：{fmt}")
    upstream = {
        "id": str(entry.get("id") or ""),
        "endpoint": str(entry.get("endpoint") or ""),
        "api_key": _api_key(entry),
        "model": str(entry.get("model") or entry.get("id") or ""),
        "format": fmt,
        "timeout": timeout,
    }
    ir = {
        "model": upstream["model"],
        "messages": messages,
        "tools": tools,
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
        "stop": None,
        "stream": False,
    }
    payload = router.SERIALIZERS[fmt](ir, upstream["model"])
    _status, _content_type, raw = router.call_upstream(upstream, payload, timeout)
    response = json.loads(raw.decode("utf-8"))
    chat = router.RESPONSE_TO_CHAT[fmt](response)
    if not isinstance(chat, dict) or not _assistant_message(chat):
        raise RuntimeError("Provider 返回了无效的转换后响应")
    return chat


def _call_chat_provider(entry: Mapping[str, Any], prompt: str, timeout: int) -> str:
    """Compatibility wrapper for a one-shot Chat provider call."""
    response = _call_chat_model(
        entry,
        [{"role": "user", "content": prompt}],
        [],
        timeout,
    )
    answer = _chat_answer(response)
    if not answer:
        raise RuntimeError("Provider 返回了空回答")
    return answer


def _call_formatted_api(entry: Mapping[str, Any], prompt: str, timeout: int) -> str:
    """Compatibility wrapper for a one-shot protocol-converted API call."""
    response = _call_formatted_model(
        entry,
        [{"role": "user", "content": prompt}],
        [],
        timeout,
    )
    answer = _chat_answer(response)
    if not answer:
        raise RuntimeError("Provider 返回了空回答")
    return answer


def _web_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten persistent Chat-style history for a web provider without tools."""
    sections: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        text = str(content or "").strip()
        if not text:
            continue
        label = {
            "system": "System instructions",
            "user": "Main agent task",
            "assistant": "Fusion previous response",
            "tool": "Tool result",
        }.get(role, role)
        sections.append(f"## {label}\n{text}")
    return "\n\n".join(sections)


def _call_web_provider(
    provider: Mapping[str, Any],
    messages: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    try:
        from .agent_relay import run_provider
    except ImportError:
        from agent_relay import run_provider
    raw = run_provider(
        _web_prompt(messages),
        "expert",
        provider_name=str(provider["id"]),
        timeout=timeout,
    )
    answer = str(raw.get("answer", "") if isinstance(raw, dict) else raw or "").strip()
    if not answer:
        raise RuntimeError("网页 Provider 返回了空回答")
    return {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
    }


def _call_provider_model(
    provider: Mapping[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout: int,
    home: Path | None,
) -> dict[str, Any]:
    config_manager.apply_config_to_environment(home)
    kind = str(provider["kind"])
    entry = provider["entry"]
    if kind == "web":
        return _call_web_provider(provider, messages, timeout)
    if kind == "local":
        return _call_chat_model(entry, messages, tools, timeout)
    if kind == "api":
        return _call_formatted_model(entry, messages, tools, timeout)
    raise RuntimeError(f"不支持的 Fusion Provider 类型：{kind}")


def _delegate_error_messages() -> dict[str, str]:
    return {
        "FUSION_DISABLED": "Fusion 当前已关闭",
        "FUSION_PROVIDER_NOT_CONFIGURED": "Fusion 尚未配置 Provider",
        "FUSION_PROVIDER_NOT_FOUND": "Fusion 配置的 Provider 已不存在",
        "FUSION_PROVIDER_DISABLED": "Fusion 配置的 Provider 已停用",
        "FUSION_PROVIDER_NOT_READY": "Fusion 配置的 Provider 缺少必要地址或模型名称",
        "FUSION_PROVIDER_AMBIGUOUS": "Fusion Provider ID 同时匹配多个类型，请使用 web:/local:/api: 前缀",
        "COMMANDER_ACTIVE": "Commander 正在运行，Fusion 已强制互斥",
        "COMMANDER_CHILD": "当前进程是 Commander 子 Agent，不能调用 Fusion",
        "FUSION_CHILD": "当前进程是 Fusion 子会话，不能递归调用 Fusion 或 Commander",
        "FUSION_TOOL_MODE_UNSUPPORTED": "网页 Provider 不支持 coding 模式，请切换为 readonly",
    }


def delegate(
    prompt: str,
    *,
    context: str = "",
    home: Path | None = None,
    lock_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    workspace: str | Path | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    question = str(prompt or "").strip()
    if not question:
        return _error("rejected", "FUSION_PROMPT_EMPTY", "Fusion 问题不能为空")

    config = config_manager.load_config(home)
    state = fusion_mode.fusion_state(
        config=config,
        home=home,
        lock_dir=lock_dir,
        environ=environ,
    )
    if not state["delegable"]:
        messages = _delegate_error_messages()
        return _error(
            "rejected",
            state["reason"],
            messages.get(state["reason"], "Fusion 当前不可调用"),
            state=state,
        )

    provider, error = fusion_mode.resolve_fusion_provider(config, state["provider_id"])
    if provider is None:
        return _error("failed", error, "无法解析 Fusion Provider", state=state)
    provider_public = fusion_mode.public_provider(provider)
    selected_workspace = (
        Path(workspace).expanduser()
        if workspace is not None
        else Path.cwd()
    )

    def call_model(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: int,
    ) -> dict[str, Any]:
        return _call_provider_model(provider, messages, tools, timeout, home)

    try:
        result = fusion_runner.run_sidekick(
            question,
            context=context,
            workspace=selected_workspace,
            session_id=session_id,
            provider_ref=str(provider["ref"]),
            provider_kind=str(provider["kind"]),
            tool_mode=str(state["tool_mode"]),
            max_steps=int(state["max_steps"]),
            timeout=int(state["timeout"]),
            home=home,
            lock_dir=lock_dir,
            environ=environ,
            call_model=call_model,
        )
    except fusion_runner.FusionRunnerError as exc:
        status = "rejected" if exc.code in {
            "FUSION_BUSY",
            "FUSION_WORKSPACE_NOT_FOUND",
            "FUSION_TOOL_MODE_UNSUPPORTED",
        } else "failed"
        return _error(
            status,
            exc.code,
            exc.message,
            provider=provider_public,
            state=state,
            **exc.extra,
        )
    except fusion_mode.FusionPreempted as exc:
        return _error(
            "rejected",
            exc.reason,
            _delegate_error_messages().get(exc.reason, "Fusion 已被中断"),
            provider=provider_public,
            state=exc.state,
        )
    except Exception as exc:
        return _error(
            "failed",
            "FUSION_PROVIDER_CALL_FAILED",
            str(exc)[:800],
            provider=provider_public,
            state=state,
        )

    result.setdefault("provider", provider_public)
    result.setdefault("max_steps", int(state["max_steps"]))
    result.setdefault("state", state)
    return result


def _provider_command(reference: str, home: Path | None) -> tuple[dict[str, Any], int]:
    requested = str(reference or "").strip()
    config = config_manager.load_config(home)
    if not requested:
        return _error(
            "rejected",
            "FUSION_PROVIDER_REQUIRED",
            "请指定 Provider，例如 web:deepseek-web；使用 provider none 可清除",
            available_providers=fusion_mode.available_providers(config),
        ), 2
    if requested.lower() in {"none", "clear", "off"}:
        _update_fusion(home, {"provider_id": ""})
        return {"status": "success", "state": fusion_mode.fusion_state(home=home)}, 0
    provider, error = fusion_mode.resolve_fusion_provider(config, requested)
    if provider is None:
        return _error(
            "rejected",
            error,
            "没有找到完全匹配的 Provider",
            available_providers=fusion_mode.available_providers(config),
        ), 2
    if not bool(provider["entry"].get("enabled", True)):
        return _error(
            "rejected",
            "FUSION_PROVIDER_DISABLED",
            "该 Provider 当前已停用",
            provider=fusion_mode.public_provider(provider),
        ), 2
    if not fusion_mode.provider_is_ready(provider):
        return _error(
            "rejected",
            "FUSION_PROVIDER_NOT_READY",
            "该 Provider 缺少必要地址或模型名称",
            provider=fusion_mode.public_provider(provider),
        ), 2
    current = fusion_mode.normalize_fusion_config(config.get("fusion"))
    if current["tool_mode"] == "coding" and provider["kind"] == "web":
        return _error(
            "rejected",
            "FUSION_TOOL_MODE_UNSUPPORTED",
            "网页 Provider 不支持 coding；请先切换为 readonly 再选择该 Provider",
            state=fusion_mode.fusion_state(config=config, home=home),
        ), 2
    _update_fusion(home, {"provider_id": provider["ref"]})
    return {
        "status": "success",
        "provider": fusion_mode.public_provider(provider),
        "state": fusion_mode.fusion_state(home=home),
    }, 0


def _mode_command(
    requested: str,
    home: Path | None,
    *,
    confirmed: bool = False,
) -> tuple[dict[str, Any], int]:
    tool_mode = str(requested or "").strip().lower()
    if tool_mode not in fusion_mode.TOOL_MODES:
        return _error(
            "rejected",
            "FUSION_TOOL_MODE_INVALID",
            "工具模式只能是 readonly 或 coding",
        ), 2
    if tool_mode == "coding" and not confirmed:
        return _error(
            "rejected",
            "FUSION_CODING_CONFIRMATION_REQUIRED",
            CODING_WARNING,
            requires_confirmation=True,
        ), 2
    if tool_mode == "coding":
        config = config_manager.load_config(home)
        current = fusion_mode.normalize_fusion_config(config.get("fusion"))
        if current["provider_id"]:
            provider, _error_code = fusion_mode.resolve_fusion_provider(
                config,
                current["provider_id"],
            )
            if provider is not None and provider["kind"] == "web":
                return _error(
                    "rejected",
                    "FUSION_TOOL_MODE_UNSUPPORTED",
                    "当前网页 Provider 只支持 readonly；请先选择 local 或 api Provider",
                    state=fusion_mode.fusion_state(config=config, home=home),
                ), 2
    _update_fusion(home, {"tool_mode": tool_mode})
    return {
        "status": "success",
        "tool_mode": tool_mode,
        "warning": CODING_WARNING if tool_mode == "coding" else "",
        "state": fusion_mode.fusion_state(home=home),
    }, 0


def _reset_command(
    workspace: str | Path | None,
    session_id: str,
    home: Path | None,
) -> tuple[dict[str, Any], int]:
    selected_workspace = (
        Path(workspace).expanduser()
        if workspace is not None
        else Path.cwd()
    ).resolve()
    if not selected_workspace.is_dir():
        return _error(
            "rejected",
            "FUSION_WORKSPACE_NOT_FOUND",
            f"Fusion 工作区不存在：{selected_workspace}",
        ), 2
    try:
        existed = fusion_runner.reset_session(
            selected_workspace,
            session_id=session_id,
            home=home,
        )
    except Exception as exc:
        return _error("failed", "FUSION_SESSION_RESET_FAILED", str(exc)[:500]), 1
    return {
        "status": "success",
        "reset": bool(existed),
        "workspace": str(selected_workspace),
        "session_id": fusion_runner.session_key(selected_workspace, session_id),
    }, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentRelay Fusion sidekick")
    parser.add_argument(
        "command",
        choices=("status", "on", "off", "provider", "mode", "reset", "delegate"),
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument("--context", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument(
        "--confirm-coding",
        action="store_true",
        help="明确确认 coding 模式允许写文件并执行命令",
    )
    parser.add_argument("--json", action="store_true", help="兼容脚本化调用的显式 JSON 模式")
    args = parser.parse_args(argv)

    if args.command == "status":
        try:
            result = {"status": "success", "state": fusion_mode.fusion_state()}
            _emit(result)
            return 0
        except Exception as exc:
            _emit(_error("failed", "FUSION_STATUS_FAILED", str(exc)[:500]))
            return 1
    if args.command in {"on", "off"}:
        try:
            _update_fusion(None, {"enabled": args.command == "on"})
            _emit({"status": "success", "state": fusion_mode.fusion_state()})
            return 0
        except Exception as exc:
            _emit(_error("failed", "FUSION_CONFIG_FAILED", str(exc)[:500]))
            return 1
    if args.command == "provider":
        try:
            result, code = _provider_command(args.value or "", None)
        except Exception as exc:
            result, code = _error("failed", "FUSION_CONFIG_FAILED", str(exc)[:500]), 1
        _emit(result)
        return code
    if args.command == "mode":
        try:
            result, code = _mode_command(
                args.value or "",
                None,
                confirmed=args.confirm_coding,
            )
        except Exception as exc:
            result, code = _error("failed", "FUSION_CONFIG_FAILED", str(exc)[:500]), 1
        _emit(result)
        return code
    if args.command == "reset":
        try:
            result, code = _reset_command(
                args.workspace or None,
                args.session_id,
                None,
            )
        except Exception as exc:
            result, code = _error("failed", "FUSION_SESSION_RESET_FAILED", str(exc)[:500]), 1
        _emit(result)
        return code

    result = delegate(
        args.value or "",
        context=args.context,
        workspace=args.workspace or None,
        session_id=args.session_id,
    )
    _emit(result)
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
