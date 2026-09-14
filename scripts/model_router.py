"""Provider-neutral model routing with offline fallback and route memory."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re

@dataclass(frozen=True)
class ModelChoice:
    provider: str
    reason: str
    fallback: str = "local"
    score: float = 0.0


class ProviderRouteMemory:
    """Persist verified provider choices without treating model output as truth.

    A record is created only after the workflow has independently verified an
    execution result.  A successful route is retained with priority ``1`` as
    requested: it is enough to make the same provider the first candidate on a
    recurrence, but never bypasses normal availability, confirmation, or
    verification checks.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None

    @staticmethod
    def problem_key(request: str) -> str:
        words = re.findall(r"[a-z0-9_+#.-]+|[\u4e00-\u9fff]{2,}", request.lower())
        return " ".join(words[:24])

    def _read(self) -> dict:
        if self.path is None:
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write(self, value: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def preferred(self, request: str) -> str | None:
        key = self.problem_key(request)
        record = self._read().get(key, {})
        provider = record.get("provider") if isinstance(record, dict) else None
        return provider if isinstance(provider, str) and provider else None

    def record(self, request: str, provider: str, *, success: bool) -> None:
        if not provider or provider == "local":
            return
        key = self.problem_key(request)
        values = self._read()
        current = values.get(key, {}) if isinstance(values.get(key), dict) else {}
        if success:
            values[key] = {
                "provider": provider,
                "priority": 1,
                "success_count": int(current.get("success_count", 0)) + 1,
                "failure_count": int(current.get("failure_count", 0)),
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
        elif current.get("provider") == provider:
            failures = int(current.get("failure_count", 0)) + 1
            values[key] = {**current, "priority": min(3, max(1, failures)),
                           "failure_count": failures,
                           "last_failure_at": datetime.now(timezone.utc).isoformat()}
        self._write(values)

    def status(self) -> list[dict]:
        values = self._read()
        return [dict(problem=problem, **record) for problem, record in values.items()
                if isinstance(record, dict)]

class ModelRouter:
    def __init__(self, providers: dict | None = None, *, route_memory: ProviderRouteMemory | None = None):
        self.providers = providers or {}
        self.route_memory = route_memory or ProviderRouteMemory()

    def choose(self, task_type: str, difficulty: int, *, needs_web=False,
               request: str = "") -> ModelChoice:
        preferred = self.route_memory.preferred(request) if request else None
        if preferred and self.available(preferred):
            return ModelChoice(preferred, "命中已验证的相似问题 Provider", score=9.0)
        if needs_web:
            choices = self._available_by_kind("web")
            if choices:
                return ModelChoice(choices[0], "需要联网研究", score=8.0)
        # Difficulty 2 is the ordinary technical-problem tier. Any configured
        # web model is eligible; DeepSeek is only a backwards-compatible name.
        if difficulty == 2:
            choices = self._available_by_kind("web")
            if choices:
                return ModelChoice(choices[0], "普通技术问题优先已配置网页模型咨询", score=7.5)
        if difficulty <= 1:
            choices = self._available_by_kind("local")
            if choices:
                return ModelChoice(choices[0], "低复杂度任务优先本地模型", score=7.0)
        if difficulty >= 3:
            choices = self._available_by_kind("api")
            if choices:
                return ModelChoice(choices[0], "高复杂度任务使用已配置 API 模型做分析", score=8.5)
        return ModelChoice("local", "无可靠外部模型时使用离线规则", score=5.0)

    def rank(self, required: set[str], *, budget: float | None = None) -> list[tuple[str, float]]:
        ranked=[]
        for name, value in self.providers.items():
            data = value if isinstance(value, dict) else {}
            capabilities=set(data.get("capabilities", ()))
            if required - capabilities: continue
            cost=float(data.get("cost", 0))
            if budget is not None and cost > budget: continue
            ranked.append((name, float(data.get("score", 0)) - cost))
        return sorted(ranked, key=lambda item: item[1], reverse=True)

    def register(self, name: str, provider, *, capabilities=(), cost: float = 0.0) -> None:
        self.providers[name] = {"provider": provider, "capabilities": set(capabilities), "cost": cost}

    def available(self, name: str) -> bool:
        value = self.providers.get(name)
        if value is None: return False
        provider = value.get("provider") if isinstance(value, dict) else value
        return not hasattr(provider, "check_available") or bool(provider.check_available())

    def _available_by_kind(self, kind: str) -> list[str]:
        matches = []
        for name, value in self.providers.items():
            metadata = value if isinstance(value, dict) else {}
            configured_kind = str(metadata.get("kind", "")).lower()
            # Existing integrations passed provider objects directly. Infer a
            # kind for them so old DeepSeek/local/gpt configuration still works.
            if not configured_kind:
                lower = name.lower()
                if name in {"deepseek", "deepseek-web", "web"} or lower.endswith("-web"):
                    configured_kind = "web"
                elif name in {"local", "local-llm", "local-model"} or lower.startswith("local-"):
                    configured_kind = "local"
                elif name in {"api", "gpt-api"} or lower.endswith("-api"):
                    configured_kind = "api"
            if configured_kind == kind and self.available(name):
                matches.append(name)
        # Prefer a concrete configured provider over the built-in generic
        # local fallback. This preserves the low-risk local-model route.
        return sorted(matches, key=lambda name: (name in {"local", "web", "api"}, name))

    def explain_choice(self, choice: ModelChoice) -> dict:
        """Return an audit-friendly, provider-neutral routing decision."""
        return {"provider": choice.provider, "reason": choice.reason,
                "fallback": choice.fallback, "score": choice.score,
                "available": self.available(choice.provider)}
