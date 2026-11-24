#!/usr/bin/env python3
"""Controlled API/local model call for a Commander child.

Keys stay in the parent-controlled process and are never copied into child
environments. Web providers remain outside Commander execution.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from .stdio_encoding import configure_utf8_stdio as _configure_utf8_stdio
except ImportError:
    from stdio_encoding import configure_utf8_stdio as _configure_utf8_stdio

try:
    from .agent_adapter import OpenAICompatibleAdapter
    from .commander_provider_pool import CommanderProviderCoordinator, ProviderProfile
except ImportError:
    from agent_adapter import OpenAICompatibleAdapter
    from commander_provider_pool import CommanderProviderCoordinator, ProviderProfile


def _profiles() -> list[ProviderProfile]:
    profiles = []
    try:
        from .config_manager import load_config
    except ImportError:
        from config_manager import load_config
    config = load_config()
    for item in config.get("local_providers", []) if isinstance(config.get("local_providers"), list) else []:
        if isinstance(item, dict) and item.get("endpoint") and item.get("enabled", True):
            profiles.append(ProviderProfile(
                "local-llm", "local",
                enabled=bool(item.get("enabled", True)),
            ))
            break
    for item in config.get("api_providers", []) if isinstance(config.get("api_providers"), list) else []:
        provider_id = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
        if provider_id:
            profiles.append(ProviderProfile(
                provider_id,
                "api",
                enabled=bool(item.get("enabled", True)) and bool(item.get("endpoint") and item.get("api_key")),
                max_concurrency=int(item.get("max_concurrency", 10) or 10),
            ))
    return profiles


def _request_provider(provider_id: str, prompt: str) -> str:
    """Make one already-leased provider request.

    Leasing and completion stay in ``call`` so every request, including the
    post-recovery comparison request, uses the same single-concurrency rules.
    """
    if provider_id != "local-llm":
        try:
            from .config_manager import load_config
        except ImportError:
            from config_manager import load_config
        for item in load_config().get("api_providers", []):
            if isinstance(item, dict) and item.get("id") == provider_id and item.get("enabled", True):
                endpoint = str(item["endpoint"])
                model = str(item.get("model", ""))
                api_key = str(item.get("api_key", ""))
                if str(item.get("format") or "chat") != "chat":
                    try:
                        from .api_format_router import ensure_router
                    except ImportError:
                        from api_format_router import ensure_router
                    endpoint = ensure_router([{
                        "id": provider_id,
                        "endpoint": endpoint,
                        "api_key": api_key,
                        "model": model or provider_id,
                        "format": str(item.get("format") or "chat"),
                        "timeout": int(item.get("timeout", 120) or 120),
                    }])
                    model = provider_id
                    api_key = ""
                adapter = OpenAICompatibleAdapter(
                    endpoint,
                    model=model,
                    timeout=int(item.get("timeout", 120) or 120),
                    api_key=api_key,
                )
                raw = adapter.send({"messages": [{"role": "user", "content": prompt}]})
                if not isinstance(raw, dict) or raw.get("error"):
                    raise RuntimeError(f"API Provider 返回错误：{raw.get('error', raw) if isinstance(raw, dict) else raw}")
                choices = raw.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise RuntimeError("API Provider 没有返回有效 choices")
                answer = str(choices[0].get("message", {}).get("content", "")).strip()
                if not answer:
                    raise RuntimeError("API Provider 返回空回答")
                return answer
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
        if not isinstance(raw, dict) or raw.get("error"):
            raise RuntimeError(f"本地模型返回错误：{raw.get('error', raw) if isinstance(raw, dict) else raw}")
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RuntimeError("本地模型没有返回有效 choices")
        answer = str(choices[0].get("message", {}).get("content", "")).strip()
        if not answer:
            raise RuntimeError("本地模型返回空回答")
        return answer
    raise RuntimeError("COMMANDER_PROVIDER_NOT_CONFIGURED")


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
    _configure_utf8_stdio()
    raise SystemExit(main())
