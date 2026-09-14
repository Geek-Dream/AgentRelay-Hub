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
    return [
        ProviderProfile("deepseek-web", "web", login_command="agent_relay_login.py --provider deepseek",
                        proxy_hint=os.environ.get("AGENTRELAY_DEEPSEEK_PROXY", "")),
        ProviderProfile("qianwen-web", "web", login_command="agent_relay_login.py --provider qianwen",
                        proxy_hint=os.environ.get("AGENTRELAY_QIANWEN_PROXY", "")),
        ProviderProfile("kimi-web", "web", login_command="agent_relay_login.py --provider kimi",
                        proxy_hint=os.environ.get("AGENTRELAY_KIMI_PROXY", "")),
        ProviderProfile("local-llm", "local"),
        ProviderProfile("gpt-api", "api"),
    ]


def call(provider: str, prompt: str, task_id: str, role: str, coordinator: Path) -> dict:
    pool = CommanderProviderCoordinator(coordinator, _profiles())
    lease, error = pool.acquire(task_id, role, [provider], depth=1,
                                simple_task=provider == "local-llm")
    if lease is None:
        return {"status": "rejected", "provider": provider, "error": error}
    try:
        if provider == "local-llm":
            endpoint = os.environ.get("AGENTRELAY_LOCAL_LLM_ENDPOINT")
            if not endpoint:
                raise RuntimeError("本地模型 endpoint 未配置")
            adapter = OpenAICompatibleAdapter(endpoint,
                model=os.environ.get("AGENTRELAY_LOCAL_LLM_MODEL", ""),
                timeout=int(os.environ.get("AGENTRELAY_LOCAL_LLM_TIMEOUT", "120")))
            raw = adapter.send({"messages": [{"role": "user", "content": prompt}]})
            answer = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        elif provider.endswith("-web"):
            if provider != "deepseek-web":
                raise RuntimeError(f"网页 Provider 尚未安装适配器: {provider}")
            try:
                from .agent_relay import run_provider
            except ImportError:
                from agent_relay import run_provider
            raw = run_provider(prompt, "expert", provider_name="deepseek",
                               timeout=int(os.environ.get("AGENTRELAY_DEEPSEEK_TIMEOUT", "120")))
            answer = raw.get("answer", "") if isinstance(raw, dict) else str(raw)
        else:
            return {"status": "rejected", "provider": provider,
                    "error": "COMMANDER_CHILD_API_DENIED"}
        pool.complete(lease, success=True, quality=1.0)
        return {"status": "success", "provider": provider, "answer": str(answer)}
    except Exception as exc:
        notice = pool.complete(lease, success=False, error=str(exc))
        result = {"status": "failed", "provider": provider, "error": str(exc)[:500]}
        if notice:
            result["notice"] = {"code": notice.code, "message": notice.message,
                                "login_command": notice.login_command,
                                "proxy_hint": notice.proxy_hint, "count": notice.count}
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Commander controlled Provider call")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--coordinator", required=True)
    parser.add_argument("prompt")
    args = parser.parse_args(argv)
    result = call(args.provider, args.prompt, args.task_id, args.role, Path(args.coordinator))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
