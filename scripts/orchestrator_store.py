#!/usr/bin/env python3
"""Small, dependency-free stores used by the Agent Orchestrator skill."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import re
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
    verified: bool = False
    used_count: int = 0
    last_used: str | None = None
    record_id: str | None = None
    project: str = "default"
    session_id: str | None = None
    verification_count: int = 0


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

    def mark_verified(self, record_id: str, confidence: float = 0.9) -> bool:
        records = self.records()
        for record in records:
            if record.record_id == record_id:
                record.verified = True
                record.confidence = max(record.confidence, confidence)
                record.used_count += 1
                record.verification_count += 1
                record.last_used = datetime.now(timezone.utc).isoformat()
                _write_json(self.path, [asdict(item) for item in records])
                return True
        return False

    def search(self, query: str, limit: int = 5, *, project: str | None = None) -> list[KnowledgeRecord]:
        terms = self._terms(query)
        ranked = []
        for record in self.records():
            if project and record.project != project:
                continue
            haystack = " ".join([record.problem, *record.symptoms, *record.solution]).lower()
            score = sum(term in haystack for term in terms)
            if record.verified:
                score += 0.25
            if score:
                ranked.append((score, record))
        return [record for _, record in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]

    @staticmethod
    def _terms(value: str) -> set[str]:
        words = re.findall(r"[a-zA-Z0-9_+#.-]+|[\u4e00-\u9fff]{2,}", value.lower())
        terms = set(words)
        for word in words:
            if len(word) > 3 and all("\u4e00" <= char <= "\u9fff" for char in word):
                terms.update(word[index:index + 2] for index in range(len(word) - 1))
        return {term for term in terms if len(term) > 1}

    def record_outcome(self, problem: str, solution: list[str], *, success: bool,
                       project: str = "default", session_id: str | None = None,
                       changed_files: list[str] | None = None) -> KnowledgeRecord:
        record = KnowledgeRecord(problem, solution, source="codex-self", verified=success,
                                 confidence=0.9 if success else 0.2, project=project,
                                 session_id=session_id, symptoms=changed_files or [])
        return self.add(record)


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
class DispatchError:
    code: str
    message: str
    retryable: bool = False


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
    error: DispatchError | None = None


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

    def list_checkpoints(self, task_id: str | None = None) -> list[Path]:
        """Return retained versions in chronological order."""
        paths = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if task_id is None:
            return paths
        return [path for path in paths if _read_json(path, {}).get("task_id") == task_id]

    def read(self, checkpoint_path: str | Path) -> dict[str, Any]:
        payload = _read_json(Path(checkpoint_path), {})
        if not isinstance(payload, dict) or not payload.get("files"):
            raise ValueError("无效或空的 checkpoint")
        return payload

    @staticmethod
    def _fingerprint(path: Path) -> tuple[str, str | None, int | None]:
        if not path.exists():
            return "missing", None, None
        try:
            data = path.read_bytes()
            return "present", hashlib.sha256(data).hexdigest(), len(data)
        except OSError:
            return "unreadable", None, None

    def create_from_workspace(self, task_id: str, workspace: str | Path,
                              allowed_files: Iterable[str],
                              before_files: dict[str, str] | None = None,
                              description: str = "") -> Path:
        """Capture a task-scoped snapshot from the real workspace.

        ``before_files`` may supply content captured immediately before an edit;
        otherwise the current workspace is treated as the before state.
        """
        root = Path(workspace).resolve()
        entries = []
        for relative in tuple(allowed_files):
            target = (root / relative).resolve()
            if target != root and root not in target.parents:
                raise PermissionError(f"文件超出 workspace 范围: {relative}")
            state, current_fp, size = self._fingerprint(target)
            before = (before_files or {}).get(relative)
            before_exists = before is not None
            if before is None and state == "present":
                try:
                    before = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    before = None
            before_fp = hashlib.sha256(before.encode("utf-8")).hexdigest() if before is not None else (current_fp if state == "present" else None)
            entries.append({"path": relative, "status": "present" if state == "present" else state,
                            "existed_before": before_exists or (state == "present" and before_files is None),
                            "before_content": before, "before_fingerprint": before_fp,
                            "after_fingerprint": current_fp, "after_size": size})
        return self.save(task_id, description, entries)

    def finalize_from_workspace(self, checkpoint_path: str | Path, workspace: str | Path) -> dict[str, Any]:
        """Record post-edit state for the same task-scoped files, never scanning others."""
        path = Path(checkpoint_path)
        payload = _read_json(path, {})
        root = Path(workspace).resolve()
        changed = []
        for entry in payload.get("files", []):
            relative = entry.get("path", "")
            target = (root / relative).resolve()
            if target != root and root not in target.parents:
                entry["error"] = "WORKSPACE_ESCAPE"
                continue
            state, fingerprint, size = self._fingerprint(target)
            entry["after_status"] = state
            entry["after_fingerprint"] = fingerprint
            entry["after_size"] = size
            if state == "present":
                try:
                    entry["after_content"] = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    entry["after_content"] = None
            else:
                entry["after_content"] = None
            before_fp = entry.get("before_fingerprint")
            if before_fp != fingerprint or entry.get("existed_before") != (state == "present"):
                changed.append(relative)
        payload["changed_files"] = changed
        payload["finalized_at"] = datetime.now(timezone.utc).isoformat()
        payload["diff_summary"] = "修改文件: " + (", ".join(changed) if changed else "无")
        _write_json(path, payload)
        return payload

    def restore(self, checkpoint_path: str | Path, workspace: str | Path, *, state: str = "before",
                expected_current: dict[str, str | None] | None = None) -> dict[str, Any]:
        """Restore one retained version with per-file conflict protection.

        ``expected_current`` is required for restoring an arbitrary historical
        ``after`` version.  Omitting it refuses every file instead of risking
        an overwrite of a user's later edit.  Rollback to the operation's
        ``before`` version can use the persisted post-edit fingerprints.
        """
        if state not in {"before", "after"}:
            raise ValueError("state 必须是 before 或 after")
        payload = self.read(checkpoint_path)
        root = Path(workspace).resolve()
        restored, conflicts, errors = [], [], []
        for entry in payload["files"]:
            relative = str(entry.get("path", ""))
            target = (root / relative).resolve()
            if target != root and root not in target.parents:
                errors.append(relative + ": WORKSPACE_ESCAPE")
                continue
            expected = (entry.get("after_fingerprint") if state == "before" else
                        (expected_current or {}).get(relative))
            current = self._fingerprint(target)[1]
            if expected is None or current != expected:
                conflicts.append(relative)
                continue
            content = entry.get("before_content") if state == "before" else entry.get("after_content")
            try:
                if content is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                restored.append(relative)
            except OSError as exc:
                errors.append(f"{relative}: {exc}")
        status = "rollback_conflict" if conflicts else ("rolled_back" if restored and not errors else "rollback_failed")
        return {"task_id": payload.get("task_id"), "checkpoint": str(checkpoint_path),
                "state": state, "restored_files": restored, "conflict_files": conflicts,
                "errors": errors, "status": status}
