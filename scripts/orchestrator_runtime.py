#!/usr/bin/env python3
"""Provider-neutral model registry and routing primitives for AgentRelay."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    scores: Mapping[str, float] = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class RouteDecision:
    level: str
    model: str | None
    reason: str
    requires_review: bool = True


class ModelRegistry:
    def __init__(self, models: Iterable[ModelSpec] = ()):
        self.models = {model.name: model for model in models if model.enabled}

    def suitable(self, required: Iterable[str]) -> list[ModelSpec]:
        required_set = set(required)
        return [m for m in self.models.values() if required_set.issubset(m.capabilities)]

    @classmethod
    def from_json(cls, path: str | Path) -> "ModelRegistry":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        models = []
        for item in value.get("models", []):
            models.append(ModelSpec(name=item["name"], kind=item["kind"],
                                    capabilities=frozenset(item.get("capabilities", [])),
                                    scores=item.get("scores", {}), enabled=item.get("enabled", True)))
        return cls(models)

    def best(self, required: Iterable[str], prefer_kinds: Iterable[str] = ()) -> ModelSpec | None:
        candidates = self.suitable(required)
        kind_order = {kind: index for index, kind in enumerate(prefer_kinds)}
        return max(candidates, key=lambda m: (m.scores.get("reasoning", 0), -kind_order.get(m.kind, 99)), default=None)


def route_task(*, complexity: int, needs_web: bool = False, needs_edit: bool = False,
               registry: ModelRegistry) -> RouteDecision:
    if complexity <= 1 and needs_edit:
        model = registry.best(("simple_code_edit",), ("local", "api"))
        return RouteDecision("worker", model.name if model else None, "低复杂度修改")
    if needs_web:
        model = registry.best(("web_search",), ("web", "api", "local"))
        return RouteDecision("expert", model.name if model else None, "需要联网检索")
    if complexity >= 4:
        return RouteDecision("commander", None, "跨模块或高复杂度任务")
    return RouteDecision("direct", None, "由 Codex 直接处理")
