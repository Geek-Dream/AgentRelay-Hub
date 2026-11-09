"""Fusion assist policy, delegation evidence and bounded failure notices."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

MAX_FAILURE_NOTICES = 3
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
                 "COMMANDER_CHILD", "FUSION_CHILD", "FUSION_PROVIDER_NOT_FOUND"}


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
        state["notice"] = (
            f"Fusion 暂不可用（{error or '调用失败'}）。主 Agent 已继续本地处理；"
            "该任务可重试，同一错误不会重复提醒。"
        )
        state["should_notify"] = should_notify
    elif status != "success":
        state["notice"] = "Fusion 当前未配置或不可用，已由主 Agent 继续处理；无需额外提醒。"
        state["should_notify"] = False
    else:
        state["notice"] = "Fusion 辅助结果已保存，等待主 Agent 审查与独立验收。"
        state["should_notify"] = True
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
            context: str = "", environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    fusion_script = home / "skills" / "agent-fusion" / "scripts" / "agent_fusion.py"
    if not fusion_script.is_file():
        return {"status": "failed", "error": "FUSION_SCRIPT_MISSING",
                "message": "Fusion 脚本不存在，主 Agent 继续本地处理"}
    python = Path(os.environ.get("AGENTRELAY_PYTHON", ""))
    if not python.is_file():
        python = home / "agentrelay-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python), str(fusion_script), "delegate", prompt,
               "--workspace", workspace, "--session-id", session_id]
    if context:
        command.extend(["--context", context])
    try:
        import subprocess
        completed = subprocess.run(command, capture_output=True, text=True,
                                   env=dict(environ or os.environ), timeout=120, check=False)
        line = next((item for item in completed.stdout.splitlines()
                     if item.startswith("AGENT_FUSION_RESULT=")), "")
        return json.loads(line.removeprefix("AGENT_FUSION_RESULT=")) if line else {
            "status": "failed", "error": "FUSION_RESULT_MISSING",
            "message": completed.stderr.strip()[:500] or "Fusion 未返回结果",
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "error": "FUSION_PROVIDER_CALL_FAILED", "message": str(exc)[:500]}


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
    if previous.get("request_id") == request_id and previous.get("delivery_state") in {"dispatched", "interrupted"} and not retry:
        result = {"status": "failed", "error": "FUSION_RETRY_REQUIRED",
                  "message": "上次委派在投递后未完成，需明确重试；未自动再次调用 Provider。",
                  "request_id": request_id}
        record_result(home, session_id, result)
        return result
    state = begin_request(home, session_id, prompt, workspace=workspace, context=context)
    result = dict(_invoke(home, session_id, prompt, workspace=workspace,
                          context=context, environ=environ), request_id=request_id)
    record_result(home, session_id, result)
    return result


def main() -> int:
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
