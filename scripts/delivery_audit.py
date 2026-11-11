"""Evidence-bound delivery audit inspired by gstack's review/QA workflow.

The hook tracks writes and freshness. The main agent must inspect the actual
requirements/diff and run behavior checks; this module never infers a semantic
pass from a green test suite or a self-declared completion message.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


CHECKS = {"requirements", "diff", "behavior", "visual", "self_review", "independent_review"}
BASE = {"requirements", "diff", "behavior", "self_review"}
COMPLETE = BASE | {"independent_review"}
SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".css", ".html", ".htm",
    ".json", ".toml", ".yaml", ".yml", ".go", ".rs", ".java", ".kt", ".cpp", ".c", ".h",
    ".sql", ".sh", ".md", ".tsx", ".prisma", ".graphql",
}
EXCLUDED_PARTS = {".git", "node_modules", "__pycache__", "dist", "build", ".agentrelay", "runtime"}
COMPLETION_CLAIM_PATTERNS = (
    r"已(?:经)?(?:完成|修复|解决|实现|处理|更新|修改)(?:了)?",
    r"(?:修复|实现|开发|处理)(?:已经|已)?完成",
    r"(?:验证|测试|检查|构建|编译|回归)[^\n。！？；;]{0,20}(?:通过|成功)",
    r"(?:通过|完成)[^\n。！？；;]{0,12}(?:验证|测试|检查|构建|编译|回归)",
    r"问题(?:已经|已)?解决",
    r"可以(?:交付|发布|封板)",
    r"\b(?:done|fixed|resolved|completed)\b",
    r"\btests?\s+(?:all\s+)?pass(?:ed)?\b",
)
NEGATED_COMPLETION_PATTERNS = (
    r"尚未|还没|没有(?:完成|通过|修复|解决)|未(?:完成|通过|修复|解决)",
    r"不会|不要|无需|并不|不是|不能|无法|不代表|不能算|不算|并未|失败|阻塞|仍需|还需|尚需|待完成",
    r"\b(?:not|isn.?t|wasn.?t|hasn.?t|failed|blocked|cannot|can.?t)\b",
)
UNRESOLVED_DELIVERY_PATTERNS = (
    r"(?:仍|尚|还|目前)(?:未|没有)(?:完成|通过|验证|检查|验收)",
    r"(?:视觉|页面|浏览器|交付|审计|验收)[^\n。！？；;]{0,30}"
    r"(?:未完成|尚未完成|仍未完成|未通过|尚未通过|未验证|尚未验证|未记录|未检查|仍需|还需)",
    r"(?:不能|无法|不应|不得)(?:声称|标记|视为|算作)"
    r"[^\n。！？；;]{0,30}(?:完成|通过|交付)",
    r"(?:不再声明|已停止声明)[^\n。！？；;]{0,30}(?:完成|通过)",
    r"(?:仍需|还需|等待|待)[^\n。！？；;]{0,20}(?:用户)?(?:复查|确认|验证|处理)",
)
META_COMPLETION_PATTERNS = (
    r"是否|为什么|为何|什么|怎么|如何|能否|可否|哪(?:个|些)?|请问",
    r"用于|用来|作用是|含义|机制|规则|判断|识别|检测|解释|说明|表示|示例|例如",
    r"(?:检查|审计|验收|交付)(?:清单|标准|流程)",
    r"如果|假如|假设|计划|准备|后续|下一步|完成后(?:的|再|才|会)|才能|不可以|不得",
    r"肯定(?:完成)?声明|否定(?:完成)?声明|表述|措辞|短语|关键词|门禁",
    r"\b(?:whether|explain|example|rule|check(?:s|ing)?|if|plan|would|should)\b",
    r"(?:吗|呢|么)[。.!！?？\s]*$",
)
QUOTED_SPAN = re.compile(r"“[^”\n]{0,300}”|‘[^’\n]{0,300}’|\"[^\"\n]{0,300}\"|'[^'\n]{0,300}'")
CLAUSE_SPLIT = re.compile(r"[。！？!?；;\n]+")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def state_path(home: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    if not safe:
        raise ValueError("delivery audit requires a session id")
    return home.expanduser().resolve() / "agent_relay_tracker" / "delivery-audit" / f"{safe}.json"


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def session_lock(home: Path, session_id: str):
    path = state_path(home, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".json.lock")
    with _thread_lock(lock_path):
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows fallback
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows fallback
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def read_state_unlocked(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"交付审计状态文件损坏，无法证明审计有效性：{path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"交付审计状态文件格式无效：{path}")
    return data


def write_state_unlocked(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_state(home: Path, session_id: str) -> dict[str, Any]:
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        return read_state_unlocked(path)


def write_state(home: Path, session_id: str, state: dict[str, Any]) -> None:
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        write_state_unlocked(path, state)


def _file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"sha256": "", "missing": True}
    try:
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "missing": False}
    except OSError:
        return {"sha256": "", "missing": False, "unreadable": True}


def workspace_fingerprint(workspace: Path, files: Iterable[str]) -> str:
    """Bind audit to the real worktree, not only the agent's selected files."""
    try:
        result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                cwd=workspace, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        digest = hashlib.sha256(result.stdout)
        try:
            tree = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            digest.update(tree.stdout.encode("utf-8"))
        except (OSError, subprocess.SubprocessError):
            pass
        for entry in result.stdout.decode("utf-8", "surrogateescape").split("\0"):
            if len(entry) < 4:
                continue
            relative = entry[3:]
            if " -> " in relative:
                relative = relative.split(" -> ", 1)[1]
            path = workspace / relative
            try:
                is_file = path.is_file()
            except OSError:
                is_file = False
            if is_file:
                digest.update(relative.encode("utf-8", "surrogateescape"))
                digest.update(_file_fingerprint(path)["sha256"].encode("ascii"))
        return digest.hexdigest()
    digest = hashlib.sha256()
    for root, directories, filenames in os.walk(workspace, topdown=True, onerror=lambda _error: None):
        directories[:] = sorted(name for name in directories if name not in EXCLUDED_PARTS)
        for filename in sorted(filenames):
            path = Path(root) / filename
            relative = path.relative_to(workspace)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(json.dumps(_file_fingerprint(path), sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _inside_workspace(workspace: Path, value: str) -> str | None:
    path = Path(value).expanduser()
    absolute = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    try:
        relative = absolute.relative_to(workspace)
    except ValueError:
        return None
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return None
    return str(absolute)


def _canonical_requirements(requirements: Iterable[str | dict[str, Any]],
                            request: str) -> list[dict[str, str]]:
    """Normalize the original requirement list that completion must cover."""
    values = list(requirements or [])
    if not values:
        values = [request.strip() or "当前任务的原始需求"]
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(values, 1):
        if isinstance(value, dict):
            requirement_id = str(value.get("id") or f"R{index}").strip()
            text = str(value.get("text") or "").strip()
        else:
            requirement_id = f"R{index}"
            text = str(value or "").strip()
        if not requirement_id:
            raise ValueError("canonical 需求必须有 id")
        if not text:
            raise ValueError(f"canonical 需求 {requirement_id} 不能为空")
        if requirement_id in seen:
            raise ValueError(f"canonical 需求 id 重复：{requirement_id}")
        seen.add(requirement_id)
        normalized.append({"id": requirement_id, "text": text})
    return normalized


def _requirements_fingerprint(requirements: list[dict[str, str]]) -> str:
    payload = json.dumps(requirements, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def begin(home: Path, session_id: str, workspace: str | Path, request: str, *,
          mode: str = "standard", files: Iterable[str] = (),
          task_id: str = "", style_change: bool = False,
          activate: bool | None = None, work_profile: str | None = None,
          logic_change: bool | None = None,
          canonical_requirements: Iterable[str | dict[str, Any]] = ()) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    tracked = [value for value in (_inside_workspace(root, str(item)) for item in files) if value]
    tracked = sorted(set(tracked))
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        existing = read_state_unlocked(path)
        previous_task_id = str(existing.get("task_id") or "").strip()
        current_task_id = str(task_id or previous_task_id).strip()
        same_task = existing.get("workspace") == str(root)
        if same_task and task_id:
            # Evidence from before task_id tracking cannot prove it belongs to
            # the current task, so never carry it into an identified task.
            same_task = bool(previous_task_id) and previous_task_id == str(task_id)
        previous_files = existing.get("files") or []
        tracked = sorted(set(tracked) | set(previous_files)) if same_task else tracked
        fingerprint = workspace_fingerprint(root, tracked)
        checks = (existing.get("checks") or {}) if same_task else {}
        candidate_changed = same_task and existing.get("fingerprint") != fingerprint
        if candidate_changed:
            checks = {}
        baseline_fingerprint = (
            str(existing.get("baseline_fingerprint") or existing.get("fingerprint") or fingerprint)
            if same_task else fingerprint
        )
        red_behavior = existing.get("red_behavior") if same_task else None
        if isinstance(red_behavior, dict) and red_behavior.get("fingerprint") != baseline_fingerprint:
            red_behavior = None
        review_fingerprint = existing.get("review_fingerprint") if same_task else None
        if candidate_changed or review_fingerprint != fingerprint:
            review_fingerprint = None
        request_text = str(request).strip() or str(existing.get("request") or "")
        if request_text.lstrip().startswith("<task-notification>"):
            request_text = str(existing.get("request") or "")
        requirements_source = list(canonical_requirements or [])
        existing_canonical = (
            existing.get("canonical_requirements") if same_task else None
        )
        if same_task and existing_canonical and candidate_changed:
            # Scope cannot be shrunk after work has started. A genuinely
            # expanded task must receive a new task_id and a new confirmation.
            canonical = _canonical_requirements(existing_canonical, request_text)
        elif requirements_source:
            canonical = _canonical_requirements(requirements_source, request_text)
        elif existing_canonical:
            canonical = _canonical_requirements(
                existing_canonical or [], request_text,
            )
        else:
            canonical = _canonical_requirements([], request_text)
        inferred_logic = logic_change
        if inferred_logic is None:
            inferred_logic = bool(
                same_task
                and existing.get("logic_change")
                and not style_change
            )
        state = {
            "version": 2, "status": "pending", "mode": mode,
            "workspace": str(root),
            "request": request_text,
            "canonical_requirements": canonical,
            "canonical_requirements_sha256": _requirements_fingerprint(canonical),
            "task_id": current_task_id,
            "style_change": bool(style_change),
            "work_profile": work_profile or existing.get("work_profile"),
            "logic_change": bool(inferred_logic),
            "active_edit": (
                bool(activate)
                if activate is not None
                else bool(not same_task or existing.get("active_edit"))
            ),
            "files": tracked, "started_at": time.time(),
            "fingerprint": fingerprint,
            "baseline_fingerprint": baseline_fingerprint,
            "candidate_fingerprint": fingerprint,
            "review_fingerprint": review_fingerprint,
            "red_behavior": red_behavior,
            "checks": checks, "attempts": int(existing.get("attempts") or 0) + 1,
        }
        write_state_unlocked(path, state)
        return _status_from_state(state)


def set_requirements(home: Path, session_id: str,
                     requirements: Iterable[str | dict[str, Any]]) -> dict[str, Any]:
    """Freeze the canonical requirement list before the first real write."""
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        state = read_state_unlocked(path)
        if not state:
            raise ValueError("没有待审计任务")
        current = workspace_fingerprint(
            Path(state["workspace"]), state.get("files") or [],
        )
        baseline = str(state.get("baseline_fingerprint") or state.get("fingerprint") or "")
        if current != baseline or current != str(state.get("fingerprint") or ""):
            raise ValueError("首次写入后不能重定义 canonical 需求；请按原始任务记录缺口")
        canonical = _canonical_requirements(requirements, str(state.get("request") or ""))
        state["version"] = 2
        state["canonical_requirements"] = canonical
        state["canonical_requirements_sha256"] = _requirements_fingerprint(canonical)
        if isinstance(state.get("checks"), dict):
            state["checks"].pop("requirements", None)
        write_state_unlocked(path, state)
        return _status_from_state(state)


def close(home: Path, session_id: str) -> None:
    """Stop legacy reminders when a problem ends; retain evidence for history."""
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        state = read_state_unlocked(path)
        if not state:
            return
        state["status"] = "closed"
        state["active_edit"] = False
        state["closed_at"] = time.time()
        write_state_unlocked(path, state)


def _complex(state: dict[str, Any]) -> bool:
    request = str(state.get("request") or "")
    if len(state.get("files") or []) >= 2:
        return True
    if re.search(r"公共组件|共享组件|全局|整个项目|全项目|跨模块|前后端|大工程|大型任务|完整实现|重构|迁移|部署|数据库|认证|权限|支付|密钥", request, re.I):
        return True
    if state.get("style_change"):
        return False
    return any(Path(value).name == "config_web.py" for value in state.get("files") or [])


def _required_checks_from_state(state: dict[str, Any]) -> set[str]:
    if not state:
        return set()
    mode = state.get("mode")
    if mode == "relaxed" and not _complex(state):
        return set()
    if state.get("style_change") and len(state.get("files") or []) == 1 and mode != "strict":
        return {"visual"}
    required = set(COMPLETE) if mode == "strict" or _complex(state) else set(BASE)
    if any(Path(value).name == "config_web.py" or Path(value).suffix.lower() in {".css", ".html", ".htm", ".vue", ".svelte", ".tsx", ".jsx", ".js", ".ts"}
           for value in state.get("files") or []):
        required.add("visual")
    return required


def required_checks(home: Path, session_id: str) -> set[str]:
    return _required_checks_from_state(read_state(home, session_id))


def _stale(state: dict[str, Any]) -> bool:
    if not state:
        return False
    current = workspace_fingerprint(Path(state["workspace"]), state.get("files") or [])
    if state.get("fingerprint") != current:
        return True
    review = state.get("review_fingerprint")
    return bool(review and review != current)


def refresh_candidate(home: Path, session_id: str) -> dict[str, Any]:
    """Accept a real post-write candidate while keeping the pre-change baseline."""
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        state = read_state_unlocked(path)
        if not state:
            raise ValueError("没有待审计任务")
        current = workspace_fingerprint(
            Path(state["workspace"]), state.get("files") or [],
        )
        if state.get("fingerprint") != current:
            state["checks"] = {}
            state["review_fingerprint"] = None
            state["status"] = "pending"
        state["version"] = 2
        state["candidate_fingerprint"] = current
        state["fingerprint"] = current
        state.setdefault("baseline_fingerprint", current)
        state.setdefault("red_behavior", None)
        write_state_unlocked(path, state)
        return _status_from_state(state)


def _check_passed(state: dict[str, Any], name: str) -> bool:
    item = (state.get("checks") or {}).get(name)
    return isinstance(item, dict) and item.get("passed") is True


def _shared_fusion_state(home: Path, session_id: str) -> dict[str, Any]:
    try:
        from scripts import fusion_assist
        return fusion_assist.read_state(home, session_id)
    except ImportError:
        import importlib.util
        path = home.expanduser().resolve() / "skills/agent-relay/scripts/fusion_assist.py"
        spec = importlib.util.spec_from_file_location("agentrelay_fusion_assist", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.read_state(home, session_id)


def record_check(home: Path, session_id: str, name: str, passed: bool,
                 evidence: Any, *, notes: str = "", recheck: bool = False) -> dict[str, Any]:
    if name not in CHECKS:
        raise ValueError("未知审计检查项")
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        state = read_state_unlocked(path)
        if _stale(state):
            raise ValueError("工作区已变化，旧审计证据已过期；请先重新读取 diff 并刷新审计")
        previous = (state.get("checks") or {}).get(name)
        if previous and previous.get("passed") is False and passed and name != "independent_review" and not recheck:
            raise ValueError(f"{name} 上次检查未通过，必须修复后用 --recheck 和新的真实证据复检，不能直接改写为通过")
        if name == "requirements":
            if not isinstance(evidence, list) or not evidence:
                raise ValueError("需求审计必须逐项列出需求、状态和证据")
            canonical = _canonical_requirements(
                state.get("canonical_requirements") or [],
                str(state.get("request") or ""),
            )
            canonical_ids = {item["id"] for item in canonical}
            normalized_evidence = []
            evidence_ids: list[str] = []
            for item in evidence:
                status_value = str(item.get("status") or "").strip().upper() if isinstance(item, dict) else ""
                requirement_id = (
                    str(item.get("requirement_id") or "").strip()
                    if isinstance(item, dict) else ""
                )
                if isinstance(item, dict) and status_value in {
                    "DONE", "CHANGED", "PARTIAL", "NOT_DONE", "UNVERIFIABLE",
                }:
                    normalized_evidence.append(dict(
                        item, status=status_value, requirement_id=requirement_id,
                    ))
                    evidence_ids.append(requirement_id)
                if not isinstance(item, dict) or not requirement_id \
                        or not str(item.get("item") or "").strip() \
                        or status_value not in {"DONE", "CHANGED", "PARTIAL", "NOT_DONE", "UNVERIFIABLE"} \
                        or not str(item.get("evidence") or "").strip():
                    raise ValueError("每项需求必须有 requirement_id、状态和具体证据")
                if requirement_id not in canonical_ids:
                    raise ValueError(f"需求证据引用了不存在的 canonical id：{requirement_id}")
                if passed and status_value not in {"DONE", "CHANGED"}:
                    raise ValueError("partial/not_done/unverifiable 需求不能标记通过，必须继续实现或明确未验证")
            if len(evidence_ids) != len(set(evidence_ids)):
                raise ValueError("同一 canonical 需求不能重复登记；请合并实际证据")
            if passed and set(evidence_ids) != canonical_ids:
                missing = sorted(canonical_ids - set(evidence_ids))
                extra = sorted(set(evidence_ids) - canonical_ids)
                detail = []
                if missing:
                    detail.append("缺少 " + "、".join(missing))
                if extra:
                    detail.append("多余 " + "、".join(extra))
                raise ValueError("需求证据必须完整覆盖 canonical 清单：" + "；".join(detail))
            evidence = normalized_evidence
        elif name in {"diff", "self_review"}:
            if not isinstance(evidence, str) or len(evidence.strip()) < 8:
                raise ValueError("diff/主审必须记录实际读取和检查结论，不能只写通过")
        elif name == "behavior":
            if not isinstance(evidence, dict) or not str(evidence.get("command") or "").strip():
                raise ValueError("行为检查必须记录真实命令")
            if "exit_code" not in evidence:
                raise ValueError("行为检查必须记录真实退出码")
        elif name == "visual":
            if not isinstance(evidence, dict) or evidence.get("status") not in {"passed", "user_review_required", "not_applicable"}:
                raise ValueError("视觉检查必须明确 passed/user_review_required/not_applicable")
            if evidence.get("status") == "passed" and not evidence.get("certification") and not evidence.get("observations"):
                raise ValueError("视觉通过必须包含逐项视觉认证或真实观察")
        elif name == "independent_review":
            if not isinstance(evidence, dict):
                raise ValueError("独立审计必须记录真实结果或不可用原因")
            if evidence.get("status") == "success":
                if passed is not True:
                    raise ValueError("真实 Fusion 成功证据不能登记为未通过")
                if evidence.get("provider") != "fusion":
                    raise ValueError("独立审计 success 必须来自真实 Fusion 调用")
                fusion = _shared_fusion_state(home, session_id)
                actual = fusion.get("result") or {}
                if fusion.get("status") != "success" or actual.get("status") != "success":
                    raise ValueError("没有当前会话真实 Fusion 成功证据，不能登记独立审计通过")
                for field in ("session_id", "handoff_path"):
                    if evidence.get(field) and evidence[field] != actual.get(field):
                        raise ValueError("独立审计证据与真实 Fusion 结果不匹配")
            elif evidence.get("status") == "unavailable":
                fallback = evidence.get("fallback")
                if fallback not in {
                    "main_agent_completed_local_adversarial_review",
                    "main_agent_local_adversarial_review_required",
                }:
                    raise ValueError("独立审计不可用时必须如实记录本地对抗性审查状态")
                if fallback == "main_agent_local_adversarial_review_required" and passed:
                    raise ValueError("仅登记本地对抗性审查待办时不能把独立审计标记为通过")
                if fallback == "main_agent_completed_local_adversarial_review" and not passed:
                    raise ValueError("本地对抗性审查已完成时应登记通过，或保留待办状态")
            else:
                raise ValueError("独立审计状态必须是 success 或 unavailable")
        checks = dict(state.get("checks") or {})
        fingerprint = state["fingerprint"]
        if name in {"self_review", "independent_review"}:
            review = str(state.get("review_fingerprint") or "")
            if review and review != fingerprint:
                raise ValueError("评审后候选版本已变化，旧评审证据已过期；必须重新审查")
            state["review_fingerprint"] = fingerprint
        checks[name] = {"passed": bool(passed), "evidence": evidence, "notes": notes.strip(),
                        "checked_at": time.time(), "fingerprint": fingerprint}
        state["checks"] = checks
        state["status"] = "pending"
        write_state_unlocked(path, state)
        return _status_from_state(state)


def _fusion_module(home: Path):
    try:
        from scripts import fusion_assist
        return fusion_assist
    except ImportError:
        import importlib.util
        path = home.expanduser().resolve() / "skills/agent-relay/scripts/fusion_assist.py"
        spec = importlib.util.spec_from_file_location("agentrelay_fusion_assist", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def independent_review(home: Path, session_id: str) -> dict[str, Any]:
    """Actually delegate once when configured; never fake independent coverage."""
    state = read_state(home, session_id)
    if not state:
        raise ValueError("没有待审计任务")
    if _stale(state):
        raise ValueError("工作区已变化，不能对旧版本执行独立审计")
    fusion = _fusion_module(home)
    prompt = ("请对当前交付做只读独立审计：逐项对照用户需求，读取实际 diff，检查逻辑错误、"
              "异常路径、未声明副作用、目标外影响和测试覆盖。只报告具体证据；没有发现问题也明确说。"
              f"\n用户需求：{state.get('request')}\n涉及文件：{state.get('files')}")
    context = "仅只读审查，不修改文件。给出需求遗漏、风险、证据和未验证项，不要代主 Agent 宣布通过。"
    try:
        result = fusion.delegate(home, session_id, prompt, workspace=state["workspace"], context=context)
    except TimeoutError as exc:
        result = {
            "status": "failed",
            "error": "FUSION_PROVIDER_TIMEOUT",
            "message": f"Fusion Provider 调用超时（timeout）: {exc}",
        }
    except (OSError, ValueError) as exc:
        result = {
            "status": "failed",
            "error": "FUSION_PROVIDER_CALL_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(result, dict):
        result = {"status": "failed", "error": "FUSION_RESULT_INVALID"}
    result = dict(result)
    result.setdefault("status", "failed" if result.get("error") else "success")
    if result.get("status") == "failed" and not result.get("error"):
        result["error"] = "FUSION_PROVIDER_CALL_FAILED"
    return record_fusion_result(home, session_id, result)


def record_fusion_result(home: Path, session_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Bridge actual Fusion evidence; unavailable means local review is still required."""
    state = read_state(home, session_id)
    if not state:
        raise ValueError("没有待审计任务")
    if result.get("status") == "success":
        fusion = _shared_fusion_state(home, session_id)
        if fusion.get("status") != "success" or (fusion.get("result") or {}).get("status") != "success":
            result = dict(result, status="failed", error="FUSION_RESULT_NOT_PERSISTED")
            return record_check(home, session_id, "independent_review", False, {
                "status": "unavailable", "reason": "FUSION_RESULT_NOT_PERSISTED",
                "fallback": "main_agent_local_adversarial_review_required",
            })
        evidence = dict(result)
        evidence["provider"] = "fusion"
        return record_check(home, session_id, "independent_review", True, evidence)
    evidence = {
        "status": "unavailable",
        "reason": str(result.get("message") or result.get("error") or "Fusion unavailable"),
        "fallback": "main_agent_local_adversarial_review_required",
    }
    return record_check(home, session_id, "independent_review", False, evidence)


def _status_from_state(state: dict[str, Any]) -> dict[str, Any]:
    if not state:
        return {"version": 1, "status": "not_started", "pending_checks": []}
    required = _required_checks_from_state(state)
    pending = sorted(name for name in required if not _check_passed(state, name))
    failed = sorted(name for name, item in (state.get("checks") or {}).items() if item.get("passed") is False)
    requirement_evidence = (
        ((state.get("checks") or {}).get("requirements") or {}).get("evidence") or []
    )
    requirement_coverage = [
        {
            "id": item.get("requirement_id"),
            "status": item.get("status"),
            "item": item.get("item"),
        }
        for item in requirement_evidence if isinstance(item, dict)
    ] if isinstance(requirement_evidence, list) else []
    result = dict(state)
    stale = _stale(state)
    result.update(status="stale" if stale else ("passed" if not pending and not failed else "pending"),
                  pending_checks=pending, failed_checks=failed, required_checks=sorted(required))
    result["requirement_coverage"] = requirement_coverage
    return result


def status(home: Path, session_id: str) -> dict[str, Any]:
    return _status_from_state(read_state(home, session_id))


def _behavior_problem(evidence: dict[str, Any]) -> str | None:
    if evidence.get("executed") is not True:
        return "行为检查没有实际命令执行证据"
    if evidence.get("exit_code") != 0:
        return "行为检查命令退出码非 0"
    if evidence.get("kind", "test") == "test" and evidence.get("tests_ran") == 0:
        return "测试命令没有实际执行测试，不能把 exit 0 当通过"
    return None


def _command_allowed(command: str) -> bool:
    """Reuse the host's dangerous-command policy for audit probes."""
    try:
        from hooks import agent_relay_tracker as tracker
        return tracker.dangerous_command_reason("Bash", {"command": command}) is None
    except ImportError:
        import importlib.util
        path = Path(__file__).resolve().parents[2] / "hooks/agent_relay_tracker.py"
        if not path.is_file():
            return not re.search(r"\brm\s+-[a-z]*r|git\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\s+--force)|\b(?:drop|truncate)\s+table|deploy|production", command, re.I)
        spec = importlib.util.spec_from_file_location("agentrelay_audit_command_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.dangerous_command_reason("Bash", {"command": command}) is None


def _execute_behavior(command: str, workspace: str, *, kind: str, timeout: int) -> dict[str, Any]:
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout[-4000:]
        stderr = completed.stderr[-1000:]
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = str(exc.stdout or "")[-4000:]
        stderr = "command timed out"
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
    tests_ran = None
    if kind == "test":
        patterns = [r"Ran\s+(\d+)\s+tests?", r"(\d+)\s+passed", r"(\d+)\s+tests?\s+passed",
                    r"(\d+)\s+tests?\s+completed", r"(\d+)\s+examples?"]
        counts = [
            int(match.group(1))
            for pattern in patterns
            if (match := re.search(pattern, stdout + "\n" + stderr, re.I))
        ]
        tests_ran = max(counts, default=0)
    return {
        "command": command, "kind": kind, "executed": True, "exit_code": exit_code,
        "tests_ran": tests_ran, "duration_seconds": round(time.time() - started, 3),
        "stdout_tail": stdout, "stderr_tail": stderr,
    }


def record_red_behavior(home: Path, session_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Store a real failing probe against the immutable pre-change baseline."""
    path = state_path(home, session_id)
    with session_lock(home, session_id):
        state = read_state_unlocked(path)
        if not state:
            raise ValueError("没有待审计任务")
        baseline = str(state.get("baseline_fingerprint") or state.get("fingerprint") or "")
        current = workspace_fingerprint(
            Path(state["workspace"]), state.get("files") or [],
        )
        if not baseline or current != baseline:
            raise ValueError("RED 必须记录在修改前基线上；当前候选已变化")
        if evidence.get("fingerprint") != baseline:
            raise ValueError("RED 证据指纹与修改前基线不一致")
        state["version"] = 2
        state["red_behavior"] = dict(
            evidence,
            recorded_at=time.time(),
            passed=bool(evidence.get("exit_code") != 0 and (
                evidence.get("kind") != "test" or int(evidence.get("tests_ran") or 0) > 0
            )),
        )
        write_state_unlocked(path, state)
        return _status_from_state(state)


def run_behavior(home: Path, session_id: str, command: str, *,
                 kind: str = "test", timeout: int = 120,
                 phase: str = "green") -> dict[str, Any]:
    """Run a real project check and bind it to RED baseline or GREEN candidate."""
    if not command.strip():
        raise ValueError("行为检查命令不能为空")
    if not _command_allowed(command):
        raise ValueError("审计行为检查不允许危险、部署或破坏性命令")
    if phase not in {"red", "green"}:
        raise ValueError("行为检查阶段必须是 red 或 green")
    state = read_state(home, session_id)
    if not state:
        raise ValueError("没有待审计任务")
    if phase == "red":
        baseline = str(state.get("baseline_fingerprint") or state.get("fingerprint") or "")
        current = workspace_fingerprint(Path(state["workspace"]), state.get("files") or [])
        if current != baseline:
            raise ValueError("RED 必须在修改前基线上执行；当前候选已经变化")
        evidence = _execute_behavior(
            command, state["workspace"], kind=kind, timeout=timeout,
        )
        after = workspace_fingerprint(Path(state["workspace"]), state.get("files") or [])
        if after != baseline:
            raise ValueError("RED 命令执行后工作区发生变化，不能作为稳定基线证据")
        evidence["fingerprint"] = baseline
        evidence["phase"] = "red"
        return record_red_behavior(home, session_id, evidence)

    refresh_candidate(home, session_id)
    state = read_state(home, session_id)
    candidate = str(state.get("candidate_fingerprint") or state.get("fingerprint") or "")
    if not candidate:
        raise ValueError("缺少候选指纹，不能登记 GREEN")
    evidence = _execute_behavior(
        command, state["workspace"], kind=kind, timeout=timeout,
    )
    after = workspace_fingerprint(Path(state["workspace"]), state.get("files") or [])
    if after != candidate:
        raise ValueError("GREEN 命令改变了工作区，当前候选不再稳定")
    evidence["fingerprint"] = candidate
    evidence["phase"] = "green"
    passed = evidence["exit_code"] == 0 and (
        kind != "test" or int(evidence.get("tests_ran") or 0) > 0
    )
    return record_check(
        home, session_id, "behavior", passed, evidence, recheck=True,
    )


def completion_claim(message: str) -> bool:
    """Return True only for a positive delivery claim, not a discussion of one."""
    text = QUOTED_SPAN.sub(" ", str(message or ""))
    if any(re.search(pattern, text, re.I) for pattern in UNRESOLVED_DELIVERY_PATTERNS):
        return False
    for raw_clause in CLAUSE_SPLIT.split(text):
        clause = raw_clause.strip()
        if not clause:
            continue
        if any(re.search(pattern, clause, re.I) for pattern in NEGATED_COMPLETION_PATTERNS):
            continue
        if any(re.search(pattern, clause, re.I) for pattern in META_COMPLETION_PATTERNS):
            continue
        if any(re.search(pattern, clause, re.I) for pattern in COMPLETION_CLAIM_PATTERNS):
            return True
    return False


def _red_green_problem(state: dict[str, Any]) -> str | None:
    profile = str(state.get("work_profile") or "")
    if profile not in {"standard", "strict", "user_required"} or not state.get("logic_change"):
        return None
    baseline = str(state.get("baseline_fingerprint") or "")
    red = state.get("red_behavior")
    if not isinstance(red, dict):
        return "[AgentRelay 交付审计] 逻辑修改缺少 RED：必须先在修改前基线执行真实失败检查。"
    if red.get("fingerprint") != baseline or red.get("passed") is not True:
        return "[AgentRelay 交付审计] RED 证据无效：必须绑定修改前基线并真实非零退出。"
    behavior = (state.get("checks") or {}).get("behavior")
    if not isinstance(behavior, dict) or behavior.get("passed") is not True:
        return "[AgentRelay 交付审计] 逻辑修改缺少 GREEN：必须在当前候选执行真实通过检查。"
    evidence = behavior.get("evidence") or {}
    candidate = str(state.get("candidate_fingerprint") or state.get("fingerprint") or "")
    if behavior.get("fingerprint") != candidate:
        return "[AgentRelay 交付审计] GREEN 证据没有绑定当前候选指纹。"
    problem = _behavior_problem(evidence)
    if problem:
        return "[AgentRelay 交付审计] GREEN " + problem + "，不能汇报完成。"
    return None


def _requirements_problem(state: dict[str, Any]) -> str | None:
    behavior = (state.get("checks") or {}).get("requirements")
    if not isinstance(behavior, dict) or behavior.get("passed") is not True:
        return None
    canonical = _canonical_requirements(
        state.get("canonical_requirements") or [],
        str(state.get("request") or ""),
    )
    canonical_ids = {item["id"] for item in canonical}
    evidence = behavior.get("evidence")
    if not isinstance(evidence, list):
        return "[AgentRelay 交付审计] 需求证据格式无效，缺少 canonical 覆盖清单。"
    covered: dict[str, str] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        requirement_id = str(item.get("requirement_id") or "").strip()
        status_value = str(item.get("status") or "").strip().upper()
        if requirement_id:
            covered[requirement_id] = status_value
    missing = sorted(canonical_ids - set(covered))
    unresolved = sorted(
        requirement_id for requirement_id, status_value in covered.items()
        if requirement_id in canonical_ids and status_value not in {"DONE", "CHANGED"}
    )
    unknown = sorted(set(covered) - canonical_ids)
    if missing or unresolved or unknown:
        details = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if unresolved:
            details.append("未完成 " + "、".join(unresolved))
        if unknown:
            details.append("范围外 " + "、".join(unknown))
        return "[AgentRelay 交付审计] canonical 需求覆盖不完整：" + "；".join(details)
    return None


def completion_block(
    home: Path,
    session_id: str,
    mode: str,
    message: str = "",
    *,
    mutate: bool = True,
) -> str | None:
    """Return a completion blocker; mutate=False keeps final preflight read-only."""
    state = read_state(home, session_id)
    if not state or state.get("status") == "closed":
        return None
    red_green_problem = _red_green_problem(state)
    if red_green_problem:
        return red_green_problem
    required = required_checks(home, session_id)
    checks = state.get("checks") or {}
    if not required:
        failed = [name for name, item in checks.items() if not item.get("passed")]
        if failed:
            return "[AgentRelay 交付审计] 以下已登记检查未通过或仍需处理：" + "、".join(sorted(failed))
        behavior = checks.get("behavior", {}).get("evidence") or {}
        problem = _behavior_problem(behavior) if behavior else None
        if problem:
            return "[AgentRelay 交付审计] " + problem + "，不能汇报完成。"
        if mutate:
            state["active_edit"] = False
            write_state(home, session_id, state)
        return None
    if _stale(state):
        return "[AgentRelay 交付审计] 审计证据已过期：文件在审计后又有修改，必须重新检查并运行 /agent-audit。"
    if not state.get("active_edit"):
        return None
    missing = sorted(required - set(checks))
    if missing:
        return ("[AgentRelay 交付审计] 尚未完成需求/diff/真实行为/审计闭环："
                + "、".join(missing) + "。请运行 /agent-audit，完成真实检查后逐项登记。")
    failed = [name for name, item in checks.items() if not item.get("passed")]
    if failed:
        return "[AgentRelay 交付审计] 以下检查未通过或仍需处理：" + "、".join(sorted(failed))
    missing_pass = sorted(name for name in required if not _check_passed(state, name))
    if missing_pass:
        return "[AgentRelay 交付审计] 以下必要检查尚未通过：" + "、".join(missing_pass)
    requirements_problem = _requirements_problem(state)
    if requirements_problem:
        return requirements_problem
    behavior = checks.get("behavior", {}).get("evidence") or {}
    problem = _behavior_problem(behavior)
    if problem:
        return "[AgentRelay 交付审计] " + problem + "，不能汇报完成。"
    visual = (checks.get("visual") or {}).get("evidence") or {}
    if visual.get("status") == "user_review_required":
        return "[AgentRelay 交付审计] 无视觉或视觉 Provider 不可用；非视觉检查已完成，仍需用户复查，不能声称视觉通过。"
    if mutate:
        state["active_edit"] = False
        write_state(home, session_id, state)
    return None


def render_status(home: Path, session_id: str) -> str:
    value = status(home, session_id)
    lines = ["「AgentRelay · 交付审计」", f"状态: {value.get('status')}",
             f"需求: {value.get('request') or '未记录'}", f"文件: {len(value.get('files') or [])}"]
    if value.get("pending_checks"):
        lines.append("待完成: " + "、".join(value["pending_checks"]))
    if value.get("failed_checks"):
        lines.append("未通过: " + "、".join(value["failed_checks"]))
    if value.get("status") == "passed":
        lines.append("证据已覆盖当前工作区；可以继续既有页面/契约验收。")
    return "\n".join(lines)


def _parse_evidence(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("begin", "set-requirements", "status", "record", "run-behavior",
                 "independent-review"):
        item = sub.add_parser(name)
        item.add_argument("--home", type=Path, required=True)
        item.add_argument("--session", required=True)
        item.add_argument("--workspace", default=".")
        item.add_argument("--request", default="")
        item.add_argument("--mode", choices=["strict", "standard", "relaxed"], default="standard")
        item.add_argument("--file", action="append", default=[])
        item.add_argument("--check", choices=sorted(CHECKS))
        item.add_argument("--evidence", default="")
        item.add_argument("--passed", action="store_true")
        item.add_argument("--recheck", action="store_true", help="上次失败后，以新证据显式复检")
        item.add_argument("--cmd", default="")
        item.add_argument("--kind", choices=["test", "probe"], default="test")
        item.add_argument("--timeout", type=int, default=120)
        item.add_argument("--phase", choices=["red", "green"], default="green")
        item.add_argument("--requirement", action="append", default=[])
        item.add_argument("--work-profile",
                          choices=["light", "standard", "strict", "user_required"])
        item.add_argument("--logic-change", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "begin":
        result = begin(args.home, args.session, args.workspace, args.request,
                       mode=args.mode, files=args.file,
                       work_profile=args.work_profile,
                       logic_change=args.logic_change or None,
                       canonical_requirements=args.requirement)
    elif args.command == "set-requirements":
        if not args.requirement:
            parser.error("set-requirements 需要至少一个 --requirement")
        result = set_requirements(args.home, args.session, args.requirement)
    elif args.command == "record":
        if not args.check:
            parser.error("record 需要 --check")
        result = record_check(args.home, args.session, args.check, args.passed,
                              _parse_evidence(args.evidence), recheck=args.recheck)
    elif args.command == "run-behavior":
        if not args.cmd.strip():
            parser.error("run-behavior 需要 --cmd")
        result = run_behavior(
            args.home, args.session, args.cmd, kind=args.kind,
            timeout=args.timeout, phase=args.phase,
        )
    elif args.command == "independent-review":
        result = independent_review(args.home, args.session)
    else:
        result = status(args.home, args.session)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"passed", "pending", "not_started"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
