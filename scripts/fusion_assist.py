"""Fusion assist policy, delegation evidence and explicit recovery controls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

try:
    from . import fusion_trace
except ImportError:
    import fusion_trace  # type: ignore


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


MAX_FAILURE_NOTICES = 1
SUBPROCESS_TIMEOUT_SECONDS = 120
PYTHON_UTF8_ARGS = ("-X", "utf8")
CIRCUIT_OPEN_ERRORS = {
    "FUSION_PROVIDER_BUSY",
    "FUSION_PROVIDER_CALL_FAILED",
    "FUSION_PROVIDER_TIMEOUT",
    "FUSION_RESULT_INVALID",
    "FUSION_RESULT_MISSING",
    "FUSION_SCRIPT_MISSING",
}
SUITABLE = re.compile(r"审查|复核|调查|批量|搜索|扫描|测试套件|全量测试|回归|复现|收集证据|跨.*文件|代码审查|review|audit|investigate|regression|test suite", re.I)
SKIP = re.compile(r"^(?:为什么|什么是|解释|翻译|你好|谢谢|好的|确认|批准)|单行|拼写|一句话|危险命令", re.I)


def suitable(prompt: str, environ: Mapping[str, str] | None = None) -> bool:
    env = environ or os.environ
    if env.get("AGENTRELAY_COMMANDER_CHILD") == "1" or env.get("AGENTRELAY_FUSION_CHILD") == "1":
        return False
    text = str(prompt or "").strip()
    if not text or env.get("AGENTRELAY_FUSION_ASSIST") == "off" or SKIP.search(text):
        return False
    return bool(SUITABLE.search(text))


def session_path(home: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    if not safe:
        raise ValueError("Fusion assist 需要有效 session_id")
    return home / "agent_relay_tracker" / "fusion-assist" / f"{safe}.json"


def read_state(home: Path, session_id: str) -> dict[str, Any]:
    try:
        value = json.loads(session_path(home, session_id).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(home: Path, session_id: str, state: dict[str, Any]) -> None:
    path = session_path(home, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def reminder(home: Path, session_id: str, prompt: str, *, stage: str,
             environ: Mapping[str, str] | None = None) -> str:
    state = read_state(home, session_id)
    if not suitable(prompt, environ):
        return ""
    if state.get("provider_circuit_open"):
        return ""
    if state.get("status") in {"success", "failed", "rejected"}:
        return ""
    if state.get("reminded"):
        return ""
    state.update(status="reminded", reminded=True, stage=stage, updated_at=time.time())
    write_state(home, session_id, state)
    return ("[AgentRelay Fusion 辅助]\n当前有适合独立调查/审查/回归的子任务。"
            "请先运行 agent_fusion.py status --json，delegable=true 时实际 delegate 一次；"
            "调用失败则继续本地完成，不要阻塞主任务。未实际调用不得声称 Fusion 已复核。")


SKIP_FAILURES = {"FUSION_PROVIDER_NOT_CONFIGURED", "FUSION_DISABLED", "COMMANDER_ACTIVE",
                 "COMMANDER_CHILD", "FUSION_CHILD", "FUSION_PROVIDER_NOT_FOUND",
                 "FUSION_RECOVERY_REQUIRED"}


def _clear_provider_circuit(state: dict[str, Any]) -> None:
    mutable = dict(state)
    for key in (
        "provider_circuit_open",
        "provider_circuit_reason",
        "provider_circuit_opened_at",
    ):
        mutable.pop(key, None)
    state.clear()
    state.update(mutable)


def _failure_notice(error: str, result: Mapping[str, Any]) -> str:
    message = str(result.get("message") or "").strip()
    if error == "FUSION_PROVIDER_TIMEOUT":
        detail = message or "Fusion 调用超时"
    elif error == "FUSION_PROVIDER_BUSY":
        detail = "Fusion Provider 排队超时"
    elif error == "FUSION_PROVIDER_CALL_FAILED":
        detail = "Fusion Provider 调用失败"
    else:
        detail = f"Fusion 调用失败（{error or '未知错误'}）"
    return (
        f"{detail}。主 Agent 已继续本地处理；本会话已暂停自动调用 Fusion，"
        "需要时可重试（显式 retry）恢复。"
    )


def record_result(home: Path, session_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    state = read_state(home, session_id)
    status = str(result.get("status") or "failed")
    error = str(result.get("error") or "")
    failure = status != "success" and error not in SKIP_FAILURES
    same_failure = state.get("last_error") == error and state.get("status") == status
    notices = int(state.get("failure_notices") or 0)
    should_notify = failure and not same_failure and notices < MAX_FAILURE_NOTICES
    if should_notify:
        notices += 1
    display_state = "completed" if status == "success" else "retryable" if failure else "unavailable"
    state.update(status=status, result=dict(result), failure_notices=notices,
                 last_error=error, display_state=display_state,
                 delivery_state="completed" if status == "success" else "interrupted" if failure else "unavailable",
                 updated_at=time.time(), reminded=True)
    if failure:
        state["notice"] = _failure_notice(error, result)
        state["should_notify"] = should_notify
        if error in CIRCUIT_OPEN_ERRORS:
            state["provider_circuit_open"] = True
            state["provider_circuit_reason"] = error
            state["provider_circuit_opened_at"] = time.time()
    elif status != "success":
        state["notice"] = "Fusion 当前未配置或不可用，已由主 Agent 继续处理；无需额外提醒。"
        state["should_notify"] = False
    else:
        state["notice"] = "Fusion 辅助结果已保存，等待主 Agent 审查与独立验收。"
        state["should_notify"] = True
        state.pop("provider_circuit_open", None)
        state.pop("provider_circuit_reason", None)
        state.pop("provider_circuit_opened_at", None)
    write_state(home, session_id, state)
    return state


def request_identity(session_id: str, prompt: str, workspace: str, context: str = "") -> str:
    payload = json.dumps([session_id, str(Path(workspace).expanduser().resolve()), prompt, context], ensure_ascii=False)
    return "fusion-request-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def begin_request(home: Path, session_id: str, prompt: str, *, workspace: str,
                  context: str = "") -> dict[str, Any]:
    """Persist intent before delivery so an interruption never becomes a new task."""
    state = read_state(home, session_id)
    request_id = request_identity(session_id, prompt, workspace, context)
    state.update(
        request_id=request_id, prompt=prompt, workspace=str(Path(workspace).expanduser().resolve()),
        context=context, delivery_state="dispatched", status="dispatched",
        updated_at=time.time(), reminded=True,
    )
    write_state(home, session_id, state)
    return state


def _invoke(home: Path, session_id: str, prompt: str, *, workspace: str,
            context: str = "", environ: Mapping[str, str] | None = None,
            request_id: str = "") -> dict[str, Any]:
    fusion_script = home / "skills" / "agent-fusion" / "scripts" / "agent_fusion.py"
    if not fusion_script.is_file():
        return {"status": "failed", "error": "FUSION_SCRIPT_MISSING",
                "message": "Fusion 脚本不存在，主 Agent 继续本地处理"}
    python = Path(os.environ.get("AGENTRELAY_PYTHON", ""))
    if not python.is_file():
        python = home / "agentrelay-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python), *PYTHON_UTF8_ARGS, str(fusion_script), "delegate", prompt,
               "--workspace", workspace, "--session-id", session_id]
    if context:
        command.extend(["--context", context])
    child_environ = dict(environ or os.environ)
    trace_path = Path(
        child_environ.get(fusion_trace.TRACE_PATH_ENV)
        or fusion_trace.default_trace_path(home)
    ).expanduser()
    child_environ[fusion_trace.TRACE_PATH_ENV] = str(trace_path)
    child_environ[fusion_trace.TRACE_ID_ENV] = request_id
    child_environ[fusion_trace.TRACE_SESSION_ENV] = session_id
    child_environ.setdefault("PYTHONUNBUFFERED", "1")
    started_at = time.monotonic()
    fusion_trace.emit(
        "assist.invoke.start",
        environ=child_environ,
        stage="subprocess_start",
        workspace=str(Path(workspace).expanduser().resolve()),
        timeout_seconds=SUBPROCESS_TIMEOUT_SECONDS,
    )
    try:
        import subprocess
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   env=child_environ,
                                   timeout=SUBPROCESS_TIMEOUT_SECONDS,
                                   check=False)
        line = next((item for item in completed.stdout.splitlines()
                     if item.startswith("AGENT_FUSION_RESULT=")), "")
        result = json.loads(line.removeprefix("AGENT_FUSION_RESULT=")) if line else {
            "status": "failed", "error": "FUSION_RESULT_MISSING",
            "message": completed.stderr.strip()[:500] or "Fusion 未返回结果",
        }
        result.setdefault("trace_path", str(trace_path))
        if str(result.get("status") or "") != "success":
            events = fusion_trace.recent_events(trace_path, request_id, limit=12)
            stage = fusion_trace.diagnostic_stage(
                events,
                exclude_events={
                    "assist.invoke.start",
                    "assist.invoke.end",
                    "assist.invoke.error",
                    "assist.invoke.timeout",
                    "fusion.delegate.error",
                    "runner.error",
                },
            )
            result.setdefault("last_stage", stage)
            result.setdefault("trace_tail", events)
        fusion_trace.emit(
            "assist.invoke.end",
            environ=child_environ,
            stage="subprocess_complete",
            duration_ms=round((time.monotonic() - started_at) * 1000),
            status=str(result.get("status") or ""),
            error=str(result.get("error") or ""),
            returncode=completed.returncode,
        )
        return result
    except subprocess.TimeoutExpired as exc:
        timeout = int(exc.timeout or SUBPROCESS_TIMEOUT_SECONDS)
        events = fusion_trace.recent_events(trace_path, request_id, limit=12)
        stage = (
            fusion_trace.diagnostic_stage(
                events,
                exclude_events={
                    "assist.invoke.start",
                    "assist.invoke.end",
                    "assist.invoke.error",
                    "assist.invoke.timeout",
                },
            )
            or fusion_trace.diagnostic_stage(events)
        )
        detail = f"；最后阶段：{stage}" if stage else ""
        stderr_tail = (
            exc.stderr[-2000:]
            if isinstance(exc.stderr, str)
            else exc.stderr.decode("utf-8", errors="replace")[-2000:]
            if isinstance(exc.stderr, bytes)
            else ""
        )
        result = {
            "status": "failed",
            "error": "FUSION_PROVIDER_TIMEOUT",
            "message": f"Fusion Provider 调用超过 {timeout} 秒未返回{detail}",
            "trace_path": str(trace_path),
            "last_stage": stage,
            "trace_tail": events,
        }
        if stderr_tail.strip():
            result["stderr_tail"] = stderr_tail.strip()
        fusion_trace.emit(
            "assist.invoke.timeout",
            environ=child_environ,
            stage="subprocess_timeout",
            duration_ms=round((time.monotonic() - started_at) * 1000),
            last_stage=stage,
        )
        return {
            **result,
            "trace_tail": fusion_trace.recent_events(trace_path, request_id, limit=12),
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {
            "status": "failed",
            "error": "FUSION_PROVIDER_CALL_FAILED",
            "message": f"{type(exc).__name__}: {exc}"[:500],
            "trace_path": str(trace_path),
        }
        fusion_trace.emit(
            "assist.invoke.error",
            environ=child_environ,
            stage="subprocess_error",
            duration_ms=round((time.monotonic() - started_at) * 1000),
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        return result


def delegate(home: Path, session_id: str, prompt: str, *, workspace: str,
             context: str = "", environ: Mapping[str, str] | None = None,
             retry: bool = False) -> dict[str, Any]:
    """Reuse one durable request across reminders, restarts and explicit retries."""
    request_id = request_identity(session_id, prompt, workspace, context)
    previous = read_state(home, session_id)
    if previous.get("request_id") == request_id and previous.get("result") and not retry:
        result = dict(previous["result"], request_id=request_id, deduplicated=True)
        record_result(home, session_id, result)
        return result
    if previous.get("provider_circuit_open") and not retry:
        return {
            "status": "rejected",
            "error": "FUSION_RECOVERY_REQUIRED",
            "message": "本会话的 Fusion Provider 已因失败暂停自动调用；需要时请显式 retry。",
            "request_id": request_id,
        }
    if previous.get("request_id") == request_id and previous.get("delivery_state") in {"dispatched", "interrupted"} and not retry:
        result = {"status": "failed", "error": "FUSION_RETRY_REQUIRED",
                  "message": "上次委派在投递后未完成，需明确重试；未自动再次调用 Provider。",
                  "request_id": request_id}
        record_result(home, session_id, result)
        return result
    if retry:
        _clear_provider_circuit(previous)
        write_state(home, session_id, previous)
    state = begin_request(home, session_id, prompt, workspace=workspace, context=context)
    result = dict(_invoke(home, session_id, prompt, workspace=workspace,
                          context=context, environ=environ,
                          request_id=request_id), request_id=request_id)
    record_result(home, session_id, result)
    return result


def main() -> int:
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("remind", "delegate", "status", "retry"):
        item = sub.add_parser(name)
        item.add_argument("--session", required=True)
        item.add_argument("--home", type=Path, required=True)
        item.add_argument("--prompt", default="")
        item.add_argument("--workspace", default=".")
        item.add_argument("--context", default="")
    args = parser.parse_args()
    if args.command == "remind":
        print(reminder(args.home, args.session, args.prompt, stage="prompt"))
    elif args.command in {"delegate", "retry"}:
        print(json.dumps(delegate(args.home, args.session, args.prompt,
                                  workspace=args.workspace, context=args.context,
                                  retry=args.command == "retry"), ensure_ascii=False))
    else:
        print(json.dumps(read_state(args.home, args.session), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
