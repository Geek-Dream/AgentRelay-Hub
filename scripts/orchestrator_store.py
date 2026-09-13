#!/usr/bin/env python3
"""Small, dependency-free stores used by the Agent Orchestrator skill."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@dataclass
class KnowledgeRecord:
    problem: str
    solution: list[str]
    environment: dict[str, str] = field(default_factory=dict)
    symptoms: list[str] = field(default_factory=list)
    source: str = "codex"
    confidence: float = 0.5
    used_count: int = 0
    last_used: str | None = None
    record_id: str | None = None


class MemoryStore:
    """JSONL-like knowledge store; matching is deliberately conservative."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def records(self) -> list[KnowledgeRecord]:
        raw = _read_json(self.path, [])
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            if isinstance(item, dict) and item.get("problem") and isinstance(item.get("solution"), list):
                result.append(KnowledgeRecord(**{k: item[k] for k in KnowledgeRecord.__dataclass_fields__ if k in item}))
        return result

    def add(self, record: KnowledgeRecord) -> KnowledgeRecord:
        if not record.record_id:
            record.record_id = datetime.now(timezone.utc).strftime("knowledge-%Y%m%d%H%M%S%f")
        values = [asdict(item) for item in self.records() if item.record_id != record.record_id]
        values.append(asdict(record))
        _write_json(self.path, values)
        return record

    def search(self, query: str, limit: int = 5) -> list[KnowledgeRecord]:
        terms = {part.lower() for part in query.split() if len(part) > 1}
        ranked = []
        for record in self.records():
            haystack = " ".join([record.problem, *record.symptoms, *record.solution]).lower()
            score = sum(term in haystack for term in terms)
            if score:
                ranked.append((score, record))
        return [record for _, record in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]


@dataclass(frozen=True)
class DispatchRequest:
    task_id: str
    level: str
    prompt: str
    model: str | None = None
    request_id: str | None = None
    title: str = ""
    workspace: str | None = None
    allowed_files: tuple[str, ...] = ()
    timeout_seconds: int = 300
    read_only: bool = True
    allow_file_write: bool = False
    allow_shell: bool = False
    allow_network: bool = False
    parent_task_id: str | None = None
    depth: int = 0
    max_calls: int = 1


@dataclass(frozen=True)
class DispatchResult:
    status: str
    task_id: str
    request_id: str | None = None
    mode: str = "expert"
    provider_id: str | None = None
    output: str = ""
    diff: str = ""
    changed_files: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass
class RequirementCard:
    task_id: str
    title: str
    explanation: str
    assessment: dict[str, bool] = field(default_factory=dict)
    affected_modules: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    current_state: dict[str, str] = field(default_factory=dict)
    target_state: dict[str, str] = field(default_factory=dict)
    verification: list[str] = field(default_factory=list)


class CheckpointStore:
    def __init__(self, directory: Path, max_checkpoints: int = 6):
        self.directory = Path(directory)
        self.max_checkpoints = max(3, min(6, int(max_checkpoints)))

    def save(self, task_id: str, description: str, files: Iterable[dict[str, str]]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        existing = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
        while len(existing) >= self.max_checkpoints:
            existing.pop(0).unlink(missing_ok=True)
        path = self.directory / f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{task_id}.json"
        _write_json(path, {"task_id": task_id, "description": description, "created_at": datetime.now(timezone.utc).isoformat(), "files": list(files)})
        return path
