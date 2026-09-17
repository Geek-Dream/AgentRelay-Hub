#!/usr/bin/env python3
"""Controlled model call for a Commander child.

This is the only supported child-to-model bridge. It permits one local model
or one authenticated web conversation at a time and rejects API providers.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from .agent_adapter import OpenAICompatibleAdapter
    from .commander_provider_pool import CommanderProviderCoordinator, ProviderProfile
except ImportError:
    from agent_adapter import OpenAICompatibleAdapter
    from commander_provider_pool import CommanderProviderCoordinator, ProviderProfile


def _profiles() -> list[ProviderProfile]:
    profiles = [
        ProviderProfile("deepseek-web", "web", login_command="agent_relay_login.py --provider deepseek",
                        proxy_hint=os.environ.get("AGENTRELAY_DEEPSEEK_PROXY", "")),
        ProviderProfile("qianwen-web", "web", login_command="agent_relay_login.py --provider qianwen",
                        proxy_hint=os.environ.get("AGENTRELAY_QIANWEN_PROXY", "")),
        ProviderProfile("kimi-web", "web", login_command="agent_relay_login.py --provider kimi",
                        proxy_hint=os.environ.get("AGENTRELAY_KIMI_PROXY", "")),
        ProviderProfile("local-llm", "local"),
        ProviderProfile("gpt-api", "api"),
    ]
    try:
        try:
            from .config_manager import load_config
        except ImportError:
            from config_manager import load_config
        configured = load_config().get("web_providers", [])
    except Exception:
        configured = []
    known = {item.provider_id for item in profiles}
    for item in configured if isinstance(configured, list) else []:
        provider_id = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
        if not provider_id or provider_id in known:
            continue
        provider_name = provider_id.removesuffix("-web")
        profiles.append(ProviderProfile(
            provider_id,
            "web",
            enabled=bool(item.get("enabled", True)),
            login_command=f"agent_relay_login.py --provider {provider_name}",
            proxy_hint=os.environ.get(
                f"AGENTRELAY_{provider_name.upper().replace('-', '_')}_PROXY", ""
            ),
        ))
        known.add(provider_id)
    return profiles


def _configured_web_provider(provider_id: str) -> str | None:
    """Resolve either `qianwen` or `qianwen-web` to its runtime name."""
    try:
        try:
            from .config_manager import load_config
        except ImportError:
            from config_manager import load_config
        entries = load_config().get("web_providers", [])
    except Exception:
        entries = []
    requested = provider_id.removesuffix("-web")
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        configured_id = str(item.get("id", "")).strip()
        if configured_id and configured_id.removesuffix("-web") == requested:
            return configured_id.removesuffix("-web")
    return "deepseek" if requested == "deepseek" else None


def _request_provider(provider_id: str, prompt: str) -> str:
    """Make one already-leased provider request.

    Leasing and completion stay in ``call`` so every request, including the
    post-recovery comparison request, uses the same single-concurrency rules.
    """
    if provider_id == "local-llm":
        endpoint = os.environ.get("AGENTRELAY_LOCAL_LLM_ENDPOINT")
        if not endpoint:
            raise RuntimeError("本地模型 endpoint 未配置")
        adapter = OpenAICompatibleAdapter(
            endpoint,
            model=os.environ.get("AGENTRELAY_LOCAL_LLM_MODEL", ""),
            timeout=int(os.environ.get("AGENTRELAY_LOCAL_LLM_TIMEOUT", "120")),
        )
        raw = adapter.send({"messages": [{"role": "user", "content": prompt}]})
        return str(raw.get("choices", [{}])[0].get("message", {}).get("content", ""))
    web_provider = _configured_web_provider(provider_id)
    if web_provider:
        # 后台调用：默认无头运行，不在用户面前弹出浏览器窗口；
        # 调试时可设 AGENT_RELAY_WEB_HEADFUL=1 强制有头
        os.environ.setdefault("AGENT_RELAY_WEB_HEADFUL", "0")
        try:
            from .agent_relay import run_provider
        except ImportError:
            from agent_relay import run_provider
        raw = run_provider(
            prompt,
            "expert",
            provider_name=web_provider,
            timeout=int(os.environ.get("AGENTRELAY_WEB_TIMEOUT", "120")),
        )
        return str(raw.get("answer", "") if isinstance(raw, dict) else raw)
    raise RuntimeError("COMMANDER_CHILD_API_DENIED")


def call(provider: str, prompt: str, task_id: str, role: str, coordinator: Path,
         fallback_providers=()) -> dict:
    try:
        from .config_manager import apply_config_to_environment
    except ImportError:
        from config_manager import apply_config_to_environment
    apply_config_to_environment()
    pool = CommanderProviderCoordinator(coordinator, _profiles())
    candidates = []
    for item in (provider, *tuple(fallback_providers)):
        if item and item not in candidates:
            candidates.append(item)
    last_result = {"status": "failed", "provider": provider, "error": "没有可用 Provider"}
    notices = []
    pending_recovery = pool.pending_recovery(task_id, role, provider)
    while candidates:
        requested = candidates[0]
        lease, error = pool.acquire(
            task_id, role, candidates, depth=1,
            simple_task=requested == "local-llm",
            wait_timeout=float(os.environ.get("AGENTRELAY_LOCAL_QUEUE_TIMEOUT", "30"))
            if requested == "local-llm" else 0,
            preferred_provider=provider if pending_recovery else None,
        )
        if lease is None:
            return {"status": "rejected", "provider": requested, "error": error,
                    "fallbacks_tried": list(notices)}
        actual = lease.provider_id
        try:
            answer = _request_provider(actual, prompt)
            pool.complete(lease, success=True, quality=1.0)
            comparison = None
            if pending_recovery and actual == pending_recovery.get("restored"):
                # Re-ask the same meaningful task to the fallback after the
                # restored provider is healthy.  This is a real comparison,
                # never a synthetic "test the model" prompt.
                fallback_id = str(pending_recovery.get("fallback") or "")
                fallback_answer = str(pending_recovery.get("fallback_output", ""))
                fallback_error = ""
                fallback_lease, fallback_acquire_error = pool.acquire(
                    task_id,
                    f"{role}:recovery-compare",
                    [fallback_id],
                    depth=1,
                    wait_timeout=0,
                )
                if fallback_lease is not None:
                    try:
                        fallback_answer = _request_provider(fallback_id, str(pending_recovery.get("prompt") or prompt))
                        pool.complete(fallback_lease, success=True, quality=1.0)
                    except Exception as exc:
                        fallback_error = str(exc)[:500]
                        pool.complete(fallback_lease, success=False, error=fallback_error)
                else:
                    fallback_error = str(fallback_acquire_error or "备用 Provider 忙或不可用")
                comparison = pool.compare_outputs(
                    task_id, role, actual, str(pending_recovery.get("fallback")),
                    str(pending_recovery.get("prompt") or prompt),
                     {actual: str(answer),
                     str(pending_recovery.get("fallback")): fallback_answer},
                    context_summary=(str(pending_recovery.get("context_summary") or "")
                                     + (f" 恢复比较时备用 Provider 未能重新回答：{fallback_error}"
                                        if fallback_error else " 恢复后已用同一真实任务重新取得备用回答。")),
                )
                pending_recovery = None
            elif notices:
                pool.record_fallback(
                    task_id, role, notices[0]["provider"], actual, prompt, str(answer),
                    context_summary=f"{actual} 在 {notices[0]['provider']} 暂时不可用期间完成了原任务，"
                                    f"后续恢复时需要把这份回答与原 Provider 对比：{str(answer)[:800]}",
                )
            return {"status": "success", "provider": actual, "answer": str(answer),
                    "fallbacks_tried": list(notices), "comparison": comparison,
                    "selected_provider": (comparison or {}).get("selected") if comparison else actual}
        except Exception as exc:
            notice = pool.complete(lease, success=False, error=str(exc))
            failure = {"provider": actual, "error": str(exc)[:500]}
            notices.append(failure)
            last_result = {"status": "failed", **failure, "fallbacks_tried": list(notices)}
            candidates = [item for item in candidates if item != actual]
            if notice:
                last_result["notice"] = {"code": notice.code, "message": notice.message,
                                          "login_command": notice.login_command,
                                          "proxy_hint": notice.proxy_hint, "count": notice.count}
    return last_result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Commander controlled Provider call")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--coordinator", required=True)
    parser.add_argument("--fallback-provider", action="append", default=[])
    parser.add_argument("prompt")
    args = parser.parse_args(argv)
    result = call(args.provider, args.prompt, args.task_id, args.role, Path(args.coordinator),
                  args.fallback_provider)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
