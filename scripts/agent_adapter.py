"""External intelligence adapters; disabled by default."""
from __future__ import annotations
from abc import ABC, abstractmethod
import json
from urllib.request import Request, urlopen


class AgentAdapter(ABC):
    @abstractmethod
    def send(self, request): ...


class OpenAICompatibleAdapter(AgentAdapter):
    """Adapter for local llama.cpp/vLLM/OpenAI-compatible servers."""
    def __init__(self, endpoint="http://127.0.0.1:8846/v1", model="", timeout=60,
                 api_key: str | None = None):
        self.endpoint = endpoint.rstrip("/"); self.model = model; self.timeout = timeout; self.api_key = api_key

    def send(self, request):
        from urllib.request import Request, urlopen
        import json
        # 不默认注入 temperature：部分上游只允许 temperature=1，
        # 擅自带 0.2 会被拒绝（503）。需要温控的调用方自己在 request 里传。
        payload = dict(request); payload.setdefault("model", self.model)
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        target = self.endpoint if self.endpoint.endswith("/chat/completions") else self.endpoint + "/chat/completions"
        req = Request(target, data=data,
                      headers=headers, method="POST")
        with urlopen(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class DeepSeekAdapter(AgentAdapter):
    def __init__(self, *, enabled: bool = False, mode: str = "api", endpoint: str = "",
                 api_key: str | None = None, model: str = "deepseek", timeout: int = 30):
        if mode not in {"api", "playwright"}:
            raise ValueError("DeepSeek mode 必须是 api 或 playwright")
        self.enabled, self.mode, self.endpoint = enabled, mode, endpoint
        self.api_key, self.model, self.timeout = api_key, model, timeout

    def send(self, request):
        if not self.enabled:
            raise RuntimeError("DeepSeekAdapter 未启用")
        if self.mode == "playwright":
            prompt = request.get("prompt", request) if isinstance(request, dict) else request
            try:
                from .agent_relay import run_provider
            except ImportError:
                from agent_relay import run_provider
            result = run_provider(str(prompt), "expert", provider_name="deepseek",
                                  timeout=self.timeout)
            if not isinstance(result, dict):
                raise RuntimeError("DeepSeek 网页 Provider 返回格式无效")
            return {"answer": result.get("answer", result.get("content", "")),
                    "raw": result}
        if not self.endpoint:
            raise RuntimeError("DeepSeek endpoint 未配置")
        payload = request if isinstance(request, dict) else {"prompt": str(request)}
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with urlopen(Request(self.endpoint, data=data, headers=headers, method="POST"), timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class PromptTemplate:
    analyze_requirement = "请分析以下任务的复杂度、影响模块、风险和验收标准：\n{request}"
    plan_change = "请为以下任务生成最小修改计划。若确实需要改文件，只返回 JSON {{\"edits\":{{\"相对路径\":\"完整文件内容\"}}}}，不要 Markdown、shell 或解释；否则返回普通文字建议：\n{request}"
    explain_result = "请用大白话解释以下任务结果：\n{result}"

    @classmethod
    def render(cls, name: str, **values) -> str:
        template = getattr(cls, name)
        return template.format(**values)
