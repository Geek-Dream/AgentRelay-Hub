#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AgentRelay 自动触发状态追踪器

职责：
1. 接收 Codex Hook 事件
2. 记录当前问题状态
3. 统计有效问题处理时间
4. 统计工具调用失败次数
5. 计算问题难度 weight
6. 判断是否满足 AgentRelay 触发条件
7. 提供 CLI 命令供 Skill 主动更新语义状态

注意：
- 本文件只负责记录和计算
- 不直接启动 AgentRelay
- 不修改 AgentRelay Playwright 程序
- failed_tool_call_count != retry_count
"""

import json
import os
import re
import sys
import time
import traceback
import hashlib
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


# ============================================================
# 路径
# ============================================================

HOME = Path.home()
CODEX_DIR = Path(
    os.environ.get(
        "CODEX_HOME",
        str(HOME / ".codex"),
    )
).expanduser()

TRACKER_DIR = CODEX_DIR / "agent_relay_tracker"
HISTORY_DIR = TRACKER_DIR / "history"
SESSIONS_DIR = TRACKER_DIR / "sessions"
LOGS_DIR = TRACKER_DIR / "logs"

CURRENT_FILE = TRACKER_DIR / "current.json"
EVENTS_FILE = TRACKER_DIR / "events.jsonl"
LOCK_FILE = TRACKER_DIR / ".lock"


def normalize_agent_id(agent_id):
    """
    标准化 Agent ID。

    Agent ID 当前不由 Codex Hook Payload 提供，
    因此由环境变量 AGENT_RELAY_AGENT_ID 提供。
    """
    if agent_id is None:
        return None

    agent_id = str(agent_id).strip()

    if not agent_id:
        return None

    allowed = set(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789-_"
    )

    agent_id = "".join(
        char for char in agent_id
        if char in allowed
    )

    return agent_id or None


def get_agent_id():
    """
    获取当前 Tracker 使用的 Agent ID。

    当前来源：
        AGENT_RELAY_AGENT_ID

    如果没有配置，则使用 default。

    Codex 原生 Hook Payload 没有 agent_id，
    所以这里不从 session_id 推导。
    """
    agent_id = os.environ.get(
        "AGENT_RELAY_AGENT_ID"
    )

    if not agent_id:
        agent_id = "default"

    return normalize_agent_id(
        agent_id
    )


def normalize_session_id(session_id):
    """
    标准化 Session ID。

    Session ID 用于生成文件名，因此只允许：

        字母
        数字
        -
        _

    非法或空 Session ID 返回 None。
    """

    if session_id is None:
        return None

    session_id = str(session_id).strip()

    if not session_id:
        return None

    safe_chars = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "-_"
    )

    if any(
        char not in safe_chars
        for char in session_id
    ):
        return None

    return session_id


def get_session_file(session_id):
    """
    获取指定 Session 的状态文件。

    文件结构：

        ~/.codex/agent_relay_tracker/sessions/
            <session_id>.json

    如果 Session ID 无效，
    返回 None。
    """

    session_id = normalize_session_id(
        session_id
    )

    if not session_id:
        return None

    return (
        SESSIONS_DIR
        / f"{session_id}.json"
    )


# ============================================================
# 常量
# ============================================================

MAX_WEIGHT = 3
TIME_PER_WEIGHT = 5 * 60

EXPLICIT_RELAY_PATTERNS = [
    r"调用\s*agent[-_\s]*relay",
    r"使用\s*agent[-_\s]*relay",
    r"让\s*agent[-_\s]*relay",
    r"请\s*agent[-_\s]*relay",
    r"调用\s*deepseek",
    r"使用\s*deepseek",
    r"让\s*deepseek",
    r"问\s*deepseek",
    r"咨询\s*deepseek",
    r"交给\s*deepseek",
    r"请\s*deepseek",
    r"让\s*deepseek\s*分析",
    r"让\s*deepseek\s*解决",
    r"调用\s*深度求索",
    r"使用\s*深度求索",
    r"问\s*深度求索",
]

EXPLICIT_RELAY_NEGATION_PATTERNS = [
    r"(?:请\s*)?(?:不要|不|勿|禁止|无需|不必|不用|请勿)\s*(?:调用|使用|触发|启动|让|请|问|咨询|交给)\s*(?:deepseek|深度求索|agent[-_\s]*relay)",
    r"(?:不要再|不再)\s*(?:调用|使用)\s*(?:deepseek|深度求索|agent[-_\s]*relay)",
    r"(?:do\s+not|don't|dont|no\s+need\s+to|without)\s+(?:call|use)\s+(?:deepseek|agent[-_\s]*relay)",
]


# ============================================================
# 默认状态
# ============================================================

def default_state(session_id=None, agent_id=None):
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "problem_active": False,
        "problem_id": None,

        # 编排任务标识。旧状态没有该字段时由迁移逻辑回填 problem_id。
        "task_id": None,
        "relay_round": 0,

        "created_at": None,
        "updated_at": None,

        "last_prompt": None,

        "turn_count": 0,

        # 语义重试次数。
        #
        # 只有 Skill 判断：
        # “用户正在重新尝试同一个解决方向”
        # 才会增加。
        #
        # 注意：
        # failed_tool_call_count != retry_count
        "retry_count": 0,

        # 工具调用失败次数。
        #
        # 这个值只用于观察问题情况，
        # 不能直接当成 retry_count。
        "failed_tool_call_count": 0,

        # 有效问题处理时间。
        #
        # 下载、安装、网络等待等操作不计入。
        "effective_time_seconds": 0.0,

        # 当前专家轮次的有效时间与任务全生命周期累计时间。
        # effective_time_seconds 保留为兼容字段，等同于本轮时间。
        "round_effective_time_seconds": 0.0,
        "cumulative_effective_time_seconds": 0.0,

        # 难度权重：
        #
        # < 5 分钟   -> 1
        # >= 5 分钟  -> 2
        # >= 10 分钟 -> 3
        #
        # 5 分钟为 weight 1，10 分钟为 weight 2，15 分钟为 weight 3。
        "weight": 1,

        # 当前问题是否已经触发 AgentRelay。
        "relay_triggered": False,

        # 当前问题是否已经发出过自动触发通知。
        # 这不代表 AgentRelay 已经调用成功。
        "relay_signal_emitted": False,

        # 触发通知只属于这个问题，避免旧状态污染新问题。
        "relay_signal_problem_id": None,

        # AgentRelay 触发原因。
        "relay_trigger_reason": None,

        # 用户是否明确要求 AgentRelay。
        "explicit_relay_request": False,

        # 当前正在执行的工具。
        #
        # 格式：
        # {
        #     "tool_use_id": {
        #         "tool_name": "...",
        #         "started_at": 1234567890.0,
        #         "excluded": False
        #     }
        # }
        "pending_tools": {},

        # 最近一次失败方案的不可逆标识。
        #
        # 仅用于识别“同一个精确方案再次失败”。
        # 不保存原始工具输入，避免把命令内容写入状态文件。
        "last_failed_solution_key": None,
        "last_failed_solution_tool_name": None,

        # 当前问题是否已经解决。
        "resolved": False,
    }


# ============================================================
# 文件操作
# ============================================================

def ensure_dirs():
    """
    初始化 Tracker 所需目录和文件。

    注意：
    这里绝对不能调用 write_state()。

    否则会出现：

        ensure_dirs()
            -> write_state()
                -> ensure_dirs()
                    -> write_state()
                        -> ...

    最终无限递归。
    """

    TRACKER_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    HISTORY_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    SESSIONS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOGS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 初始化 current.json
    if not CURRENT_FILE.exists():
        with open(
            CURRENT_FILE,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                default_state(),
                f,
                ensure_ascii=False,
                indent=2,
            )

    # 初始化 events.jsonl
    if not EVENTS_FILE.exists():
        EVENTS_FILE.touch()

    # 初始化锁文件
    if not LOCK_FILE.exists():
        LOCK_FILE.touch()


def acquire_lock():
    """
    获取文件锁。

    macOS / Linux 使用 fcntl。
    如果当前环境没有 fcntl，则退化为空操作。
    """

    ensure_dirs()

    fp = open(
        LOCK_FILE,
        "a+",
    )

    if fcntl is not None:
        fcntl.flock(
            fp.fileno(),
            fcntl.LOCK_EX,
        )

    return fp


def release_lock(fp):
    """
    释放文件锁。
    """

    if fp is None:
        return

    try:
        if fcntl is not None:
            fcntl.flock(
                fp.fileno(),
                fcntl.LOCK_UN,
            )
    finally:
        fp.close()


def read_state(session_id=None, agent_id=None):
    """
    读取 Tracker 状态。

    当提供有效 session_id 时：

        ~/.codex/agent_relay_tracker/sessions/
            <session_id>.json

    当没有 session_id 时：

        ~/.codex/agent_relay_tracker/current.json

    如果状态文件不存在或损坏，
    自动返回默认状态，
    避免 Tracker 阻塞 Codex。
    """

    ensure_dirs()

    session_id = normalize_session_id(
        session_id
    )

    if agent_id is None:
        agent_id = get_agent_id()

    agent_id = normalize_agent_id(
        agent_id
    )

    state_file = (
        get_session_file(session_id)
        if session_id
        else CURRENT_FILE
    )

    try:
        if (
            state_file is None
            or not state_file.exists()
        ):
            return default_state(
                session_id=session_id,
                agent_id=agent_id
            )

        with open(
            state_file,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        base = default_state(
            session_id=session_id,
            agent_id=agent_id
        )

        base.update(data)

        # 如果 Session 状态文件缺少 session_id，
        # 使用当前调用传入的 session_id 修复。
        if session_id:
            base["session_id"] = session_id

        # 兼容第一代状态文件，并补齐编排任务/轮次字段。
        if not base.get("task_id"):
            base["task_id"] = base.get("problem_id")
        if "relay_round" not in base:
            base["relay_round"] = 0
        legacy_time = float(base.get("effective_time_seconds", 0) or 0)
        if "round_effective_time_seconds" not in base:
            base["round_effective_time_seconds"] = legacy_time
        if "cumulative_effective_time_seconds" not in base:
            base["cumulative_effective_time_seconds"] = legacy_time

        return base

    except Exception:
        return default_state(
            session_id=session_id,
            agent_id=agent_id
        )

def write_state(state, session_id=None, agent_id=None):
    """
    原子写入 Tracker 状态。

    当提供有效 session_id 时：

        sessions/<session_id>.json

    当没有 session_id 时：

        current.json

    使用临时文件 + os.replace()，
    避免并发读取时出现半截 JSON。
    """

    ensure_dirs()

    # 优先使用显式传入的 session_id。
    #
    # 如果没有传入，则尝试从 state 中获取。
    if session_id is None:
        session_id = state.get(
            "session_id"
        )

    # 优先使用显式传入的 agent_id。
    #
    # 如果没有传入，则尝试从 state 中获取。
    if agent_id is None:
        agent_id = state.get(
            "agent_id"
        )

    if agent_id is None:
        agent_id = get_agent_id()

    session_id = normalize_session_id(
        session_id
    )

    agent_id = normalize_agent_id(
        agent_id
    )

    state_file = (
        get_session_file(session_id)
        if session_id
        else CURRENT_FILE
    )

    # 保证写入 Session 文件时，
    # 状态内部也保存对应 session_id。
    if session_id:
        state["session_id"] = session_id

    # 状态内部同时保存当前 Agent ID。
    if agent_id:
        state["agent_id"] = agent_id

    temp_file = state_file.with_suffix(
        ".tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp_file,
        state_file,
    )

def update_state(mutator):
    """
    通用的加锁状态更新函数。

    当前部分功能暂时不强制使用，
    保留给后续扩展。
    """

    lock = acquire_lock()

    try:
        state = read_state()

        result = mutator(state)

        if result is None:
            result = state

        result["updated_at"] = iso_now()

        write_state(result)

        return result

    finally:
        release_lock(lock)


def log_event(
    event_type,
    data=None,
):
    """
    向 events.jsonl 追加一条事件。
    """

    ensure_dirs()

    event = {
        "timestamp": iso_now(),
        "timestamp_unix": now_ts(),
        "event": event_type,
        "data": data or {},
    }

    with open(
        EVENTS_FILE,
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            json.dumps(
                event,
                ensure_ascii=False,
            )
            + "\n"
        )


def write_tracker_log(message, level="WARNING"):
    """将 Hook/Tracker 诊断写入独立日志，不污染 Hook stdout。"""

    ensure_dirs()

    try:
        log_file = LOGS_DIR / "error.log"

        with open(
            log_file,
            "a",
            encoding="utf-8",
        ) as f:
            f.write(
                f"[{iso_now()}] {level}: {message}\n"
            )

    except Exception:
        # 诊断日志失败不能阻塞 Codex。
        pass


# ============================================================
# 基础工具函数
# ============================================================

def now_ts():
    return time.time()


def iso_now():
    return (
        datetime.now()
        .astimezone()
        .isoformat()
    )


def generate_problem_id():
    """
    生成当前问题唯一 ID。

    使用：
        时间戳 + UUID

    避免不同 Session 在同一进程、
    同一秒内创建问题时发生 ID 冲突。
    """

    return (
        datetime.now().strftime(
            "%Y%m%d_%H%M%S_%f"
        )
        + "_"
        + __import__("uuid").uuid4().hex[:8]
    )


def normalize_text(value):
    """
    将各种输入统一转换为干净字符串。
    """

    if value is None:
        return ""

    if not isinstance(value, str):
        try:
            value = json.dumps(
                value,
                ensure_ascii=False,
            )
        except Exception:
            value = str(value)

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def build_solution_key(tool_name, tool_input):
    """
    为一次工具方案生成保守的稳定标识。

    只把工具名和完整工具输入完全相同的调用视为同一方案。
    不尝试推断“语义相似”的命令，避免把不同解决方向误计为 retry。
    """

    tool_name = normalize_text(tool_name)

    if not tool_name:
        return None

    try:
        if isinstance(tool_input, (dict, list)):
            normalized_input = json.dumps(
                tool_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            normalized_input = str(tool_input or "").strip()
    except Exception:
        normalized_input = normalize_text(tool_input)

    fingerprint = (
        tool_name
        + "\x00"
        + normalized_input
    )

    return hashlib.sha256(
        fingerprint.encode("utf-8")
    ).hexdigest()


def record_failed_solution(state, solution_key, tool_name):
    """
    推进失败方案状态机。

    第一次失败只建立失败记录；同一精确方案再次失败才增加 retry。
    与最近失败方案不同的精确方案视为新 branch，并将 retry 重置为 0。
    """

    if not solution_key:
        return None

    previous_key = state.get(
        "last_failed_solution_key"
    )

    if previous_key == solution_key:
        state["retry_count"] = (
            int(state.get("retry_count", 0) or 0)
            + 1
        )
        result = "retry"

    elif previous_key:
        state["retry_count"] = 0
        result = "new_branch"

    else:
        result = "first_failure"

    state["last_failed_solution_key"] = solution_key
    state["last_failed_solution_tool_name"] = normalize_text(
        tool_name
    )

    return result


def contains_explicit_relay_request(text):
    """
    判断用户是否明确要求调用 AgentRelay。
    """

    text = normalize_text(text)

    if not text:
        return False

    # 否定请求不能触发 AgentRelay。
    for pattern in EXPLICIT_RELAY_NEGATION_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return False

    for pattern in EXPLICIT_RELAY_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return True

    return False


def is_error_like_output(value):
    """
    判断工具输出是否表现出明显错误。

    这里只统计：
        failed_tool_call_count

    绝对不会自动增加：
        retry_count
    """

    # Codex PostToolUse normally supplies a structured response such as:
    # {"exit_code": 37, "output": ""}.  Treat a present numeric exit
    # code as authoritative before falling back to the legacy text checks.
    if isinstance(value, dict):
        structured_exit_code = value.get("exit_code")

        if structured_exit_code is None:
            structured_exit_code = value.get("exitCode")

        if structured_exit_code is not None:
            try:
                return int(structured_exit_code) != 0
            except (TypeError, ValueError):
                pass

    text = normalize_text(value)

    if not text:
        return False

    error_patterns = [
        r"\berror\b",
        r"\bexception\b",
        r"\btraceback\b",
        r"\bfailed\b",
        r"\bfailure\b",
        r"command failed",
        r"process exited with code [1-9]",
        r"exit code [1-9]",
        r"syntaxerror",
        r"modulenotfounderror",
        r"filenotfounderror",
        r"permissionerror",
        r"timeout",
        r"timed out",
        r"失败",
        r"报错",
        r"异常",
    ]

    for pattern in error_patterns:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return True

    return False

# ============================================================
# 有效时间判断
# ============================================================

EXCLUDED_COMMAND_PATTERNS = [
    r"\bcurl\b",
    r"\bwget\b",
    r"\bdownload\b",
    r"\bpip\s+install\b",
    r"\bnpm\s+install\b",
    r"\byarn\s+add\b",
    r"\bpnpm\s+add\b",
    r"\bbrew\s+install\b",
    r"\bgit\s+clone\b",
    r"\bgit\s+pull\b",
    r"\buv\s+pip\s+install\b",
    r"\bconda\s+install\b",
    r"\bpython.*-m\s+pip\s+install\b",
]


def is_excluded_tool_call(
    tool_name,
    tool_input,
):
    """
    判断工具调用是否属于：

    - 下载
    - 安装
    - 拉取代码
    - 网络等待

    这些时间不计入有效问题处理时间。
    """

    text = normalize_text(
        str(tool_name or "")
        + " "
        + str(tool_input or "")
    )

    for pattern in EXCLUDED_COMMAND_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return True

    return False


def calculate_weight(
    effective_seconds,
):
    """
    根据有效处理时间计算问题难度。

    < 5 分钟：
        weight = 1

    >= 5 分钟：
        weight = 2

    >= 10 分钟：
        weight = 3

    注意：

    weight == 3
    并不会直接触发 AgentRelay。

    真正的时间触发条件是：

        effective_time >= 15 分钟
    """

    effective_seconds = float(
        effective_seconds or 0
    )

    if effective_seconds >= 10 * 60:
        return 3

    if effective_seconds >= 5 * 60:
        return 2

    return 1


# ============================================================
# AgentRelay 触发条件
# ============================================================

def get_trigger_conditions(state):
    """
    统一计算当前问题是否满足 AgentRelay 条件。

    三个真正的触发条件：

    1. retry_count >= 3
    2. effective_time >= 15 分钟
    3. 用户明确要求 AgentRelay

    weight 只是难度指标，
    不是独立触发条件。
    """

    retry_count = int(
        state.get(
            "retry_count",
            0,
        )
        or 0
    )

    effective_seconds = float(state.get("round_effective_time_seconds", state.get("effective_time_seconds", 0)) or 0)

    explicit_request = bool(
        state.get(
            "explicit_relay_request",
            False,
        )
    )

    already_triggered = bool(
        state.get(
            "relay_triggered",
            False,
        )
    )

    problem_id = state.get("problem_id")
    signal_emitted = bool(
        problem_id
        and state.get(
            "relay_signal_emitted",
            False,
        )
        and state.get(
            "relay_signal_problem_id"
        ) == problem_id
    )

    conditions = {
        "retry_count": retry_count,

        "retry_trigger": (
            retry_count >= 3
        ),

        "effective_time_seconds": (
            effective_seconds
        ),
        "round_effective_time_seconds": effective_seconds,
        "cumulative_effective_time_seconds": float(state.get("cumulative_effective_time_seconds", effective_seconds) or 0),

        "time_trigger": (
            effective_seconds >= 15 * 60
        ),

        "explicit_request": (
            explicit_request
        ),

        "explicit_trigger": (
            explicit_request
        ),

        "weight": int(
            state.get(
                "weight",
                1,
            )
            or 1
        ),

        "already_triggered": (
            already_triggered
        ),

        "signal_emitted": signal_emitted,
    }

    automatic_trigger = (
        conditions["retry_trigger"]
        or conditions["time_trigger"]
    )

    # 自动触发受防重复状态限制；用户显式请求是一次性放行事件，
    # 即使当前问题已经自动触发过 AgentRelay 也必须允许。
    conditions["should_trigger"] = (
        conditions["explicit_trigger"]
        or (
            automatic_trigger
            and not already_triggered
            and not signal_emitted
        )
    )

    if (
        conditions["explicit_trigger"]
    ):
        conditions["reason"] = (
            "explicit_request"
        )

    elif (
        conditions["retry_trigger"]
    ):
        conditions["reason"] = (
            "retry_count"
        )

    elif (
        conditions["time_trigger"]
    ):
        conditions["reason"] = (
            "effective_time"
        )

    else:
        conditions["reason"] = None

    return conditions




# ============================================================
# AgentRelay 触发信号输出
# ============================================================

def emit_relay_trigger(state):
    """
    当 PostToolUse 后当前问题满足 AgentRelay 自动触发条件时，
    向 stdout 输出 Codex Hook 可消费的 JSON additionalContext。

    注意：
    - 这里只负责通知 Agent
    - 不在 Tracker 内直接调用 agent_relay.py
    - 不修改 state["relay_triggered"]
    """

    conditions = get_trigger_conditions(state)

    if not conditions.get("should_trigger"):
        return False

    problem_context = normalize_text(
        state.get("last_prompt")
    )

    if len(problem_context) > 500:
        problem_context = (
            problem_context[:497]
            + "..."
        )

    additional_context = (
        "AgentRelay 自动触发条件已经满足。"
        f"触发原因: {conditions.get('reason')}。"
        f"retry_count: {conditions.get('retry_count')}。"
        "effective_time_seconds: "
        f"{int(conditions.get('effective_time_seconds', 0))}。"
        f"当前问题: {problem_context or '未记录用户问题摘要'}。"
        "请根据 AgentRelay Skill 判断并调用 AgentRelay；"
        "Hook 不会自行调用 agent_relay.py。"
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        },
    }, ensure_ascii=False), flush=True)

    state["relay_signal_emitted"] = True
    state["relay_signal_problem_id"] = state.get(
        "problem_id"
    )

    # 显式请求只消费一次，避免后续每个 PostToolUse 重复发信号。
    state["explicit_relay_request"] = False

    write_state(state)

    return True

# ============================================================
# 问题生命周期
# ============================================================

def archive_current_problem(
    state,
):
    """
    将当前问题归档到：

        ~/.codex/agent_relay_tracker/history/

    每个问题一个 JSON 文件。
    """

    problem_id = state.get(
        "problem_id"
    )

    if not problem_id:
        return

    try:
        archive_file = (
            HISTORY_DIR
            / f"{problem_id}.json"
        )

        with open(
            archive_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception:
        # 归档失败不能影响 Codex。
        pass


def create_new_problem(
    prompt=None,
    session_id=None,
    agent_id=None,
):
    """
    创建一个全新的问题。

    新问题状态：

        retry_count = 0
        effective_time = 0
        weight = 1
        relay_triggered = false
    """

    problem_id = (
        generate_problem_id()
    )

    state = default_state(
        session_id=session_id,
        agent_id=agent_id,
    )

    state["problem_active"] = True

    state["problem_id"] = (
        problem_id
    )
    state["task_id"] = problem_id

    state["created_at"] = iso_now()

    state["updated_at"] = iso_now()

    state["last_prompt"] = (
        normalize_text(prompt)
    )

    state["turn_count"] = 1
    state["relay_round"] = 0

    state[
        "explicit_relay_request"
    ] = (
        contains_explicit_relay_request(
            prompt
        )
    )

    log_event(
        "problem_new",
        {
            "problem_id": problem_id,
            "prompt": normalize_text(
                prompt
            ),
        },
    )

    return state


def start_new_problem(prompt=None, session_id=None):
    """
    无条件结束当前问题，
    然后创建新问题。
    """

    lock = acquire_lock()

    try:
        old_state = read_state(session_id)

        if old_state.get(
            "problem_active"
        ):
            archive_current_problem(
                old_state
            )

        state = create_new_problem(
            prompt,
            session_id=session_id,
            agent_id=old_state.get("agent_id"),
        )

        write_state(state, session_id)

        return state

    finally:
        release_lock(lock)


def resolve_problem(session_id=None):
    """
    将当前问题标记为已经解决。

    注意：

    这里不会立即删除 current.json。

    而是：

        problem_active = false
        resolved = true

    下一次用户产生新问题时，
    再创建新的问题状态。
    """

    lock = acquire_lock()

    try:
        state = read_state(session_id)

        if state.get(
            "problem_active"
        ):
            state["resolved"] = True

            state[
                "problem_active"
            ] = False

            log_event(
                "problem_resolved",
                {
                    "problem_id": (
                        state.get(
                            "problem_id"
                        )
                    ),

                    "retry_count": (
                        state.get(
                            "retry_count",
                            0,
                        )
                    ),

                    "effective_time_seconds": (
                        state.get(
                            "effective_time_seconds",
                            0,
                        )
                    ),
                },
            )

            archive_current_problem(
                state
            )

            write_state(state, session_id)

        return state

    finally:
        release_lock(lock)


# ============================================================
# 语义重试
# ============================================================

def increment_retry(session_id=None):
    """
    增加一次语义重试。

    只有 Skill 明确判断：

        “用户正在重新尝试同一个解决方向”

    才调用这个命令。

    例如：

        第一次：
        修改代码 A

        第二次：
        继续修改代码 A

        第三次：
        再次按照 A 的方向修改

    才会累计 retry_count。

    注意：

        工具报错
        !=
        retry
    """

    lock = acquire_lock()

    try:
        state = read_state(session_id)

        if not state.get(
            "problem_active"
        ):
            state = create_new_problem(
                session_id=session_id,
                agent_id=state.get("agent_id"),
            )

        state["retry_count"] = (
            int(
                state.get(
                    "retry_count",
                    0,
                )
                or 0
            )
            + 1
        )

        log_event(
            "retry",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),

                "retry_count": (
                    state[
                        "retry_count"
                    ]
                ),
            },
        )

        write_state(state, session_id)

        return state

    finally:
        release_lock(lock)


def mark_new_branch(session_id=None):
    """
    标记进入新的解决方案分支。

    新分支的第一次尝试：

        不算 retry。

    例如：

        Maven 依赖 A
            ↓
        Maven 依赖 B

    这是新的解决方向。

    所以：

        retry_count 不增加。

    这里只记录事件。
    """

    lock = acquire_lock()

    try:
        state = read_state(session_id)

        if not state.get(
            "problem_active"
        ):
            state = create_new_problem(
                session_id=session_id,
                agent_id=state.get("agent_id"),
            )

        # 新解决方案分支从第一次尝试重新计算 retry。
        #
        # 例如：
        #
        #   方案 A -> retry=2
        #       ↓
        #   切换方案 B
        #       ↓
        #   retry=0
        #
        # 方案 B 的第一次失败才会变成 retry=1。
        state["retry_count"] = 0
        state["last_failed_solution_key"] = None
        state["last_failed_solution_tool_name"] = None

        log_event(
            "new_solution_branch",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),

                "retry_count": (
                    state.get(
                        "retry_count",
                        0,
                    )
                ),
            },
        )

        write_state(state, session_id)

        return state

    finally:
        release_lock(lock)


# ============================================================
# AgentRelay 状态
# ============================================================

def mark_relay(reason=None, session_id=None):
    """
    标记当前问题已经触发 AgentRelay。

    注意：

    这里不负责启动 AgentRelay。

    这里只记录：

        AgentRelay 已经被触发
        触发原因是什么

    真正调用 AgentRelay Playwright
    由 Skill / 外部流程负责。
    """

    lock = acquire_lock()

    try:
        state = read_state(session_id)

        state[
            "relay_triggered"
        ] = True

        state[
            "relay_trigger_reason"
        ] = (
            normalize_text(reason)
            or "unknown"
        )

        log_event(
            "relay_triggered",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),

                "reason": (
                    state.get(
                        "relay_trigger_reason"
                    )
                ),

                "retry_count": (
                    state.get(
                        "retry_count",
                        0,
                    )
                ),

                "effective_time_seconds": (
                    state.get(
                        "effective_time_seconds",
                        0,
                    )
                ),
            },
        )

        # 专家调用完成后开启下一轮：保留累计时间，但清零本轮计时和重试。
        state["relay_round"] = int(state.get("relay_round", 0) or 0) + 1
        state["round_effective_time_seconds"] = 0.0
        state["effective_time_seconds"] = 0.0
        state["retry_count"] = 0
        state["weight"] = 1
        state["relay_triggered"] = False
        state["relay_signal_emitted"] = False
        state["relay_signal_problem_id"] = None

        write_state(state, session_id)

        return state

    finally:
        release_lock(lock)


def confirm_relay_success(reason=None, session_id=None):
    """Confirm a successful Expert call and start the next task round."""
    return mark_relay(reason=reason or "expert_success", session_id=session_id)
# ============================================================
# UserPromptSubmit
# ============================================================

def handle_user_prompt(
    prompt,
    session_id=None,
):
    """
    用户提交新消息。

    处理：

    1. 没有当前问题 -> 创建新问题
    2. 当前问题已经解决 -> 创建新问题
    3. 当前问题继续对话 -> 更新 turn_count
    4. 检测用户是否明确要求 AgentRelay
    """

    prompt = normalize_text(prompt)

    lock = acquire_lock()

    try:
        state = read_state(
            session_id=session_id
        )

        # 没有当前问题
        if not state.get(
            "problem_active"
        ):
            state = create_new_problem(
                prompt,
                session_id=session_id,
                agent_id=state.get("agent_id"),
            )

            write_state(
                state,
                session_id=session_id,
            )

            return state

        # 当前问题已经解决
        if state.get("resolved"):
            archive_current_problem(
                state
            )

            state = create_new_problem(
                prompt,
                session_id=session_id,
                agent_id=state.get("agent_id"),
            )

            write_state(
                state,
                session_id=session_id,
            )

            return state

        # 当前问题继续
        state["turn_count"] = (
            int(
                state.get(
                    "turn_count",
                    0,
                )
                or 0
            )
            + 1
        )

        state["last_prompt"] = prompt

        # 检测用户明确要求 AgentRelay
        if contains_explicit_relay_request(
            prompt
        ):
            state[
                "explicit_relay_request"
            ] = True

            log_event(
                "explicit_relay_request",
                {
                    "problem_id": (
                        state.get(
                            "problem_id"
                        )
                    ),
                    "prompt": prompt,
                },
            )

        log_event(
            "user_prompt",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),
                "turn_count": (
                    state.get(
                        "turn_count"
                    )
                ),
            },
        )

        write_state(
            state,
            session_id=session_id,
        )

        return state

    finally:
        release_lock(lock)


# ============================================================
# PreToolUse
# ============================================================

def handle_pre_tool_use(
    tool_name=None,
    tool_input=None,
    tool_use_id=None,
    session_id=None,
):
    """
    工具开始执行。

    保存：

        tool_name
        started_at
        excluded

    后续 PostToolUse 会根据 started_at
    计算工具实际执行时间。
    """

    tool_name = normalize_text(
        tool_name
    )

    excluded = is_excluded_tool_call(
        tool_name,
        tool_input,
    )

    tool_id = (
        normalize_text(
            tool_use_id
        )
        or f"anonymous_{time.time_ns()}"
    )

    started_at = now_ts()

    lock = acquire_lock()

    try:
        state = read_state(
            session_id=session_id
        )

        if not state.get(
            "problem_active"
        ):
            state = create_new_problem(
                session_id=session_id,
                agent_id=state.get("agent_id"),
            )

        pending_tools = state.get(
            "pending_tools",
            {},
        )

        pending_tools[tool_id] = {
            "tool_name": tool_name,
            "started_at": started_at,
            "excluded": excluded,
            "solution_key": build_solution_key(
                tool_name,
                tool_input,
            ),
        }

        state[
            "pending_tools"
        ] = pending_tools

        log_event(
            "tool_start",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),
                "tool_use_id": tool_id,
                "tool_name": tool_name,
                "excluded": excluded,
                "solution_key": pending_tools[tool_id].get(
                    "solution_key"
                ),
            },
        )

        write_state(
            state,
            session_id=session_id,
        )

        return state
    finally:
        release_lock(lock)


# ============================================================
# PostToolUse
# ============================================================

def handle_post_tool_use(
    tool_name=None,
    tool_input=None,
    tool_output=None,
    tool_use_id=None,
    session_id=None,
):
    """
    工具执行结束。

    处理：

    1. 计算工具实际执行时间
    2. 排除下载 / 安装等时间
    3. 累加有效处理时间
    4. 重新计算 weight
    5. 统计工具失败次数

    注意：

        failed_tool_call_count

    永远不会自动转换成：

        retry_count
    """

    tool_name = normalize_text(
        tool_name
    )

    tool_id = normalize_text(
        tool_use_id
    )

    lock = acquire_lock()

    try:
        state = read_state(
            session_id=session_id
        )

        pending_tools = state.get(
            "pending_tools",
            {},
        )

        pending = pending_tools.pop(
            tool_id,
            None,
        )

        duration = 0.0
        excluded = False
        solution_key = None
        solution_result = None

        # 找到了对应的 PreToolUse
        if pending:
            started_at = float(
                pending.get(
                    "started_at",
                    now_ts(),
                )
            )

            duration = max(
                0.0,
                now_ts() - started_at,
            )

            excluded = bool(
                pending.get(
                    "excluded",
                    False,
                )
            )

            solution_key = pending.get(
                "solution_key"
            )

        # PostToolUse may arrive without a matching PreToolUse entry.
        # Rebuild the same key from the payload only when pending data
        # did not provide a valid key.
        if not solution_key:
            solution_key = build_solution_key(
                tool_name,
                tool_input,
            )

        state[
            "pending_tools"
        ] = pending_tools

        # ====================================================
        # 有效处理时间
        # ====================================================

        if (
            pending
            and not excluded
            and duration > 0
        ):
            current_round_time = float(state.get("round_effective_time_seconds", state.get("effective_time_seconds", 0)) or 0)
            current_total_time = float(state.get("cumulative_effective_time_seconds", state.get("effective_time_seconds", 0)) or 0)
            state["round_effective_time_seconds"] = current_round_time + duration
            state["cumulative_effective_time_seconds"] = current_total_time + duration
            # Legacy consumers read this field; it now means current-round time.
            state["effective_time_seconds"] = state["round_effective_time_seconds"]

        # ====================================================
        # Weight
        # ====================================================

        state["weight"] = calculate_weight(
            float(
                state.get("round_effective_time_seconds", state.get("effective_time_seconds", 0))
                or 0
            )
        )

        # ====================================================
        # 工具失败统计
        # ====================================================

        tool_failed = is_error_like_output(
            tool_output
        )

        if tool_failed:
            state[
                "failed_tool_call_count"
            ] = (
                int(
                    state.get(
                        "failed_tool_call_count",
                        0,
                    )
                    or 0
                )
                + 1
            )

            solution_result = record_failed_solution(
                state,
                solution_key,
                tool_name,
            )

        elif (
            solution_key
            and solution_key == state.get(
                "last_failed_solution_key"
            )
        ):
            # 同一方案已经成功，之后再失败应视为新的失败序列。
            state["last_failed_solution_key"] = None
            state["last_failed_solution_tool_name"] = None

        log_event(
            "tool_finish",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),

                "tool_use_id": tool_id,

                "tool_name": tool_name,

                "duration_seconds": round(
                    duration,
                    3,
                ),

                "excluded": excluded,

                "effective_time_seconds": round(
                    float(
                        state.get(
                            "effective_time_seconds",
                            0,
                        )
                        or 0
                    ),
                    3,
                ),

                "weight": state.get(
                    "weight",
                    1,
                ),

                "failed_tool_call_count": (
                    state.get(
                        "failed_tool_call_count",
                        0,
                    )
                ),

                "solution_result": solution_result,

                "retry_count": state.get(
                    "retry_count",
                    0,
                ),
            },
        )

        write_state(
            state,
            session_id=session_id,
        )

        emit_relay_trigger(state)

        return state

    finally:
        release_lock(lock)


# ============================================================
# Stop
# ============================================================

def handle_stop(session_id=None):
    """
    Stop Hook。

    当前只记录状态。

    不在这里直接启动 AgentRelay。

    AgentRelay 的实际调用由 Skill
    和外部流程负责。
    """

    lock = acquire_lock()

    try:
        state = read_state(
            session_id=session_id
        )

        log_event(
            "stop",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),

                "retry_count": (
                    state.get(
                        "retry_count",
                        0,
                    )
                ),

                "effective_time_seconds": (
                    state.get(
                        "effective_time_seconds",
                        0,
                    )
                ),

                "weight": (
                    state.get(
                        "weight",
                        1,
                    )
                ),

                "failed_tool_call_count": (
                    state.get(
                        "failed_tool_call_count",
                        0,
                    )
                ),
            },
        )

        write_state(
            state,
            session_id=session_id,
        )

        return state

    finally:
        release_lock(lock)


# ============================================================
# SessionStart
# ============================================================

def handle_session_start(session_id=None):
    """
    Codex SessionStart。

    不重置当前问题。

    因为：

        Codex 重启

    不代表：

        当前问题已经解决。
    """

    lock = acquire_lock()

    try:
        state = read_state(
            session_id=session_id
        )

        if session_id:
            state["session_id"] = session_id

        log_event(
            "session_start",
            {
                "agent_id": (
                    state.get(
                        "agent_id"
                    )
                ),

                "session_id": (
                    state.get(
                        "session_id"
                    )
                ),

                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),

                "problem_active": (
                    state.get(
                        "problem_active",
                        False,
                    )
                ),
            },
        )

        write_state(
            state,
            session_id=session_id,
        )


        return state

    finally:
        release_lock(lock)


# ============================================================
# 状态输出
# ============================================================

def print_status(
    state=None,
):
    """
    输出当前 Tracker 状态。

    输出 JSON，方便 Skill 或人直接读取。
    """

    if state is None:
        state = read_state()

    effective_seconds = float(
        state.get(
            "effective_time_seconds",
            0,
        )
        or 0
    )

    conditions = get_trigger_conditions(
        state
    )

    output = {
        "problem_active": (
            state.get(
                "problem_active",
                False,
            )
        ),

        "problem_id": (
            state.get(
                "problem_id"
            )
        ),

        "task_id": state.get("task_id") or state.get("problem_id"),
        "relay_round": int(state.get("relay_round", 0) or 0),

        "resolved": (
            state.get(
                "resolved",
                False,
            )
        ),

        "turn_count": (
            state.get(
                "turn_count",
                0,
            )
        ),

        "retry_count": (
            state.get(
                "retry_count",
                0,
            )
        ),

        "failed_tool_call_count": (
            state.get(
                "failed_tool_call_count",
                0,
            )
        ),

        "effective_time_seconds": round(
            effective_seconds,
            3,
        ),

        "round_effective_time_seconds": round(float(state.get("round_effective_time_seconds", effective_seconds) or 0), 3),
        "cumulative_effective_time_seconds": round(float(state.get("cumulative_effective_time_seconds", effective_seconds) or 0), 3),

        "effective_time_minutes": round(
            effective_seconds / 60,
            2,
        ),

        "weight": (
            state.get(
                "weight",
                1,
            )
        ),

        "explicit_relay_request": (
            state.get(
                "explicit_relay_request",
                False,
            )
        ),

        "relay_triggered": (
            state.get(
                "relay_triggered",
                False,
            )
        ),

        "relay_trigger_reason": (
            state.get(
                "relay_trigger_reason"
            )
        ),

        "trigger_conditions": conditions,
    }

    print(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        )
    )


# ============================================================
# CLI
# ============================================================

def cli():
    """
    CLI：

        status [--session SESSION_ID]
        new [--session SESSION_ID] [PROMPT]
        retry --session SESSION_ID
        branch --session SESSION_ID
        resolve --session SESSION_ID
        mark-relay --session SESSION_ID REASON
        confirm-relay --session SESSION_ID REASON
        reset --session SESSION_ID
    """

    ensure_dirs()

    if len(sys.argv) < 2:
        print_status()
        return 0

    command = sys.argv[1]

    try:
        # ----------------------------------------------------
        # 解析 --session
        # ----------------------------------------------------

        session_id = None
        args = sys.argv[2:]

        if "--session" in args:
            index = args.index("--session")

            if index + 1 >= len(args):
                print(
                    "Error: --session requires SESSION_ID",
                    file=sys.stderr,
                )
                return 1

            session_id = normalize_session_id(
                args[index + 1]
            )

            if not session_id:
                print(
                    "Error: invalid SESSION_ID",
                    file=sys.stderr,
                )
                return 1

            args = (
                args[:index]
                + args[index + 2:]
            )

        # ----------------------------------------------------
        # status
        # ----------------------------------------------------

        if command == "status":
            state = read_state(
                session_id=session_id
            )

            print_status(state)

            return 0

        # ----------------------------------------------------
        # new
        # ----------------------------------------------------

        if command == "new":
            prompt = " ".join(args)

            state = start_new_problem(
                prompt,
                session_id=session_id,
            )

            print_status(state)

            return 0

        # ----------------------------------------------------
        # retry
        # ----------------------------------------------------

        if command == "retry":
            state = increment_retry(
                session_id=session_id
            )

            print_status(state)

            return 0

        # ----------------------------------------------------
        # branch
        # ----------------------------------------------------

        if command == "branch":
            state = mark_new_branch(
                session_id=session_id
            )

            print_status(state)

            return 0

        # ----------------------------------------------------
        # resolve
        # ----------------------------------------------------

        if command == "resolve":
            state = resolve_problem(
                session_id=session_id
            )

            print_status(state)

            return 0

        # ----------------------------------------------------
        # mark-relay
        # ----------------------------------------------------

        if command == "mark-relay":
            reason = " ".join(args)

            state = mark_relay(
                reason,
                session_id=session_id,
            )

            print_status(state)

            return 0

        if command == "confirm-relay":
            reason = " ".join(args)
            state = confirm_relay_success(reason, session_id=session_id)
            print_status(state)
            return 0

        # ----------------------------------------------------
        # reset
        # ----------------------------------------------------

        if command == "reset":
            state = default_state(
                session_id=session_id
            )

            write_state(
                state,
                session_id=session_id,
            )

            log_event(
                "reset",
                {
                    "session_id": session_id,
                },
            )

            print_status(state)

            return 0

        print(
            "Unknown command:",
            command,
            file=sys.stderr,
        )

        return 1

    except Exception:
        traceback.print_exc(
            file=sys.stderr
        )

        return 1


# ============================================================
# Hook 输入解析
# ============================================================

def read_hook_input():
    """
    Codex Hook 通常通过 stdin
    提供 JSON。

    这里尽量兼容不同字段格式。
    """

    try:
        raw = sys.stdin.read()

        if not raw.strip():
            return {}

        data = json.loads(raw)

        if isinstance(data, dict):
            return data

        return {
            "value": data,
        }

    except Exception:
        # Hook 解析失败不能阻塞 Codex。
        return {}


def first_value(
    data,
    *keys,
):
    """
    从多个可能的字段名称中
    找到第一个存在的值。
    """

    for key in keys:
        value = data.get(key)

        if value is not None:
            return value

    return None


def extract_tool_name(
    data,
):
    return first_value(
        data,
        "tool_name",
        "toolName",
        "name",
    )


def extract_tool_input(
    data,
):
    return first_value(
        data,
        "tool_input",
        "toolInput",
        "input",
        "arguments",
    )


def extract_tool_output(
    data,
):
    return first_value(
        data,
        "tool_response",
        "toolResponse",
        "tool_output",
        "toolOutput",
        "output",
        "result",
    )


def extract_session_id(
    data,
):
    """
    从 Codex Hook Payload 提取 session_id。

    当前已确认 Payload 使用：

        session_id

    同时兼容：

        sessionId
    """

    return normalize_session_id(
        first_value(
            data,
            "session_id",
            "sessionId",
        )
    )


def extract_tool_use_id(
    data,
):
    return first_value(
        data,
        "tool_use_id",
        "toolUseId",
        "call_id",
        "callId",
        "id",
    )


def extract_prompt(
    data,
):
    prompt = first_value(
        data,
        "prompt",
        "user_prompt",
        "userPrompt",
        "message",
        "text",
    )

    return normalize_text(
        prompt
    )


# ============================================================
# Hook 事件入口
# ============================================================

def handle_hook_event(
    event_name,
    data,
):
    """
    根据 Hook 事件名称，
    分发到对应处理函数。

    所有与问题状态相关的 Hook
    都使用同一个 Codex session_id。

    这样：

        Session A
            ↓
        sessions/A.json

        Session B
            ↓
        sessions/B.json

    不会互相污染。
    """

    event_name = normalize_text(
        event_name
    )

    session_id = extract_session_id(
        data
    )

    # Hook 状态必须绑定真实 Codex Session。
    # 缺失 session_id 时禁止回退到共享 current.json。
    if not session_id:
        message = (
            f"missing session_id for hook event {event_name}"
        )

        log_event(
            "missing_session_id",
            {
                "event": event_name,
                "keys": list(data.keys()),
            },
        )
        write_tracker_log(message)
        return None

    if event_name == "SessionStart":
        return handle_session_start(
            session_id=session_id
        )

    if event_name == "UserPromptSubmit":
        return handle_user_prompt(
            extract_prompt(data),
            session_id=session_id,
        )

    if event_name == "PreToolUse":
        return handle_pre_tool_use(
            tool_name=extract_tool_name(
                data
            ),
            tool_input=extract_tool_input(
                data
            ),
            tool_use_id=extract_tool_use_id(
                data
            ),
            session_id=session_id,
        )

    if event_name == "PostToolUse":
        return handle_post_tool_use(
            tool_name=extract_tool_name(
                data
            ),
            tool_input=extract_tool_input(
                data
            ),
            tool_output=extract_tool_output(
                data
            ),
            tool_use_id=extract_tool_use_id(
                data
            ),
            session_id=session_id,
        )

    if event_name == "Stop":
        return handle_stop(
            session_id=session_id
        )

    return read_state(
        session_id=session_id
    )


# ============================================================
# Main
# ============================================================

def main():
    """
    支持两种运行方式。

    CLI：

        python3 agent_relay_tracker.py status
        python3 agent_relay_tracker.py retry
        python3 agent_relay_tracker.py branch
        python3 agent_relay_tracker.py resolve
        python3 agent_relay_tracker.py mark-relay xxx

    Hook：

        python3 agent_relay_tracker.py

    Hook 模式从 stdin 读取 JSON。
    """

    ensure_dirs()

    # --------------------------------------------------------
    # CLI 模式
    # --------------------------------------------------------

    if len(sys.argv) > 1:
        return cli()

    # --------------------------------------------------------
    # Hook 模式
    # --------------------------------------------------------

    data = read_hook_input()

    event_name = first_value(
        data,
        "event",
        "event_name",
        "eventName",
        "hook_event_name",
        "hookEventName",
    )

    # 没有事件名称。
    #
    # 不报错，不阻塞 Codex。
    if not event_name:
        log_event(
            "unknown_hook_event",
            {
                "keys": list(
                    data.keys()
                ),
            },
        )

        return 0

    try:
        handle_hook_event(
            event_name,
            data,
        )

    except Exception:
        # Tracker 出错绝对不能阻塞 Codex。
        log_event(
            "tracker_error",
            {
                "event": event_name,
                "error": traceback.format_exc(),
            },
        )
        write_tracker_log(
            traceback.format_exc(),
            level="ERROR",
        )

    return 0


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    sys.exit(main())
