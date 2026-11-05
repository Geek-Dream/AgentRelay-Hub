"""Fusion assist policy, delegation evidence and bounded failure notices."""
from __future__ import annotations

import argparse
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
    previous_notices = int(state.get("failure_notices") or 0)
    failure = status != "success" and str(result.get("error") or "") not in SKIP_FAILURES
    notices = min(MAX_FAILURE_NOTICES, previous_notices + 1) if failure else previous_notices
    state.update(status=status, result=dict(result), failure_notices=notices,
                 updated_at=time.time(), reminded=True)
    if failure:
        should_notify = previous_notices < MAX_FAILURE_NOTICES
        state["notice"] = ("Fusion 调用失败，请检查 Provider；本次任务由主 Agent 继续完成。"
                           if should_notify else "")
        state["should_notify"] = should_notify
    elif status != "success":
        state["notice"] = "Fusion 当前不可用，已由主 Agent 继续处理；无需配置时不提醒。"
        state["should_notify"] = False
    else:
        state["notice"] = "Fusion 已完成辅助调用；主 Agent 仍需审查结果并独立验收。"
        state["should_notify"] = True
    write_state(home, session_id, state)
    return state


def delegate(home: Path, session_id: str, prompt: str, *, workspace: str,
             context: str = "", environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Delegate exactly once through the existing CLI, then persist honest evidence."""
    fusion_script = home / "skills" / "agent-fusion" / "scripts" / "agent_fusion.py"
    if not fusion_script.is_file():
        result = {"status": "failed", "error": "FUSION_SCRIPT_MISSING",
                  "message": "Fusion 脚本不存在，主 Agent 继续本地处理"}
        record_result(home, session_id, result)
        return result
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
        result = json.loads(line.removeprefix("AGENT_FUSION_RESULT=")) if line else {
            "status": "failed", "error": "FUSION_RESULT_MISSING",
            "message": completed.stderr.strip()[:500] or "Fusion 未返回结果",
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {"status": "failed", "error": "FUSION_PROVIDER_CALL_FAILED", "message": str(exc)[:500]}
    record_result(home, session_id, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("remind", "delegate", "status"):
        item = sub.add_parser(name)
        item.add_argument("--session", required=True)
        item.add_argument("--home", type=Path, required=True)
        item.add_argument("--prompt", default="")
        item.add_argument("--workspace", default=".")
        item.add_argument("--context", default="")
    args = parser.parse_args()
    if args.command == "remind":
        print(reminder(args.home, args.session, args.prompt, stage="prompt"))
    elif args.command == "delegate":
        print(json.dumps(delegate(args.home, args.session, args.prompt,
                                  workspace=args.workspace, context=args.context), ensure_ascii=False))
    else:
        print(json.dumps(read_state(args.home, args.session), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
