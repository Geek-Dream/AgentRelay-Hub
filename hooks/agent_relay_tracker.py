#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AgentRelay 候选求援状态追踪器

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

import difflib
import hashlib
import json
import os
import re
import sys
import time
import traceback
import unicodedata
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
PROBLEMS_DIR = TRACKER_DIR / "problems"
LOGS_DIR = TRACKER_DIR / "logs"

# 权重分级动作：1=继续处理，2=先查本地归档，3=才进入调用支援模型的判断。
ARCHIVE_REMINDER_WEIGHT = 2
ARCHIVE_SCRIPT = CODEX_DIR / "skills" / "agent-relay" / "scripts" / "agent_relay_archive.py"
# 只做保守的本地模糊匹配。匹配后最高只把起始权重抬到 2，绝不直接用 3
# 触发支援模型；模型仍需先查归档并自行核对。
ARCHIVE_MATCH_THRESHOLD = 0.68
ARCHIVE_RECORDS_FILENAME = "已完成任务.jsonl"

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


def get_problem_file(session_id, problem_id):
    """返回会话内某个问题的独立快照路径。"""

    session_id = normalize_session_id(session_id)
    problem_id = normalize_session_id(problem_id)

    if not session_id or not problem_id:
        return None

    return PROBLEMS_DIR / session_id / f"{problem_id}.json"


# ============================================================
# 常量
# ============================================================

MAX_WEIGHT = 3
WEIGHT_TWO_SECONDS = 5 * 60
# The historical warning ladder is 5m -> weight 2, 15m -> weight 3;
# 15m is the independent escalation threshold.
WEIGHT_THREE_SECONDS = 15 * 60

PAUSE_PROBLEM_PATTERNS = [
    r"先(?:不|别|不要)处理",
    r"暂时(?:不|别|不要)处理",
    r"先放一放",
    r"暂停(?:这个|当前)?问题",
    r"先别管",
    r"跳过(?:这个|当前)?问题",
    r"(?:pause|skip)\s+(?:this\s+)?(?:issue|problem|task)",
]

NEW_PROBLEM_PATTERNS = [
    r"换(?:个|一个)(?:问题|话题)",
    r"另(?:外|一个)(?:有|的)?(?:问题|事情)",
    r"再问(?:个|一个)",
    r"新(?:的)?问题",
    r"转而处理",
    r"(?:new|another|different)\s+(?:issue|problem|task|topic)",
]

CONTINUATION_PATTERNS = [
    r"继续",
    r"还是",
    r"仍然",
    r"依然",
    r"又(?:出现|报错|失败|不行)",
    r"刚才|上面|前面|之前",
    r"这个问题|当前问题|同一个问题",
    r"没(?:有)?解决|没好|不行|失败了|又报错",
    r"修改后|测试后|运行后|安装后",
    r"你说的|你刚刚|我没明白|什么意思",
    r"为什么|为啥|怎么做|如何做|然后呢|还有呢|可以吗|确定吗|好了吗",
    r"^(?:不对|不是|对|好的|明白了|知道了)[。！!？?]?$",
    r"(?:continue|still|again|same\s+(?:issue|problem)|previous|didn.?t\s+work|not\s+fixed)",
]

RESOLUTION_PATTERNS = [
    r"已(?:经)?完成",
    r"完成了",
    r"已(?:经)?修复",
    r"问题已(?:经)?解决",
    r"已经解决",
    r"验证通过",
    r"测试(?:已)?通过",
    r"构建(?:已)?通过",
    r"编译(?:已)?通过",
    r"运行正常",
    r"全部通过",
    r"可以封板",
    r"\b(?:done|fixed|resolved)\b",
    r"\btests? pass(?:ed)?\b",
]

UNRESOLVED_PATTERNS = [
    r"未完成|尚未完成|还没完成",
    r"未解决|尚未解决|还没解决|没有解决",
    r"测试失败|验证失败|构建失败|编译失败",
    r"仍然(?:失败|报错|不行)",
    r"无法(?:完成|解决|验证)",
    r"需要你(?:提供|确认|选择)",
    r"等待你(?:提供|确认|选择)",
    r"如果.*(?:完成|修复|解决|通过)",
    r"需要(?:再|你)?(?:测试|验证|确认)",
    r"\b(?:not|isn.?t)\s+(?:done|fixed|resolved|complete)\b",
    r"\b(?:failed|blocked|cannot|can.?t)\b",
]

# Hook 只做保守的“行动句式”预筛选。它不能证明用户真的要求调用，
# 只能让 Agent 再做一次完整语义审核。
EXPLICIT_RELAY_CANDIDATE_PATTERNS = [
    r"^\s*\$agent-relay(?:\s|$)",
    r"^\s*agent[-_\s]*relay(?:\s|$)",
    r"^\s*(?:请|麻烦|劳驾)?\s*(?:你\s*)?(?:现在|马上|立即)?\s*(?:帮我\s*)?(?:去\s*)?(?:调用|触发|启动|使用)(?:一下)?\s*(?:deepseek|深度求索|agent[-_\s]*relay|在线模型|外援模型)",
    r"^\s*(?:请|麻烦|劳驾)?\s*(?:你\s*)?(?:现在|马上|立即)?\s*(?:帮我\s*)?(?:去\s*)?(?:问|咨询|找)(?:一下)?\s*(?:deepseek|深度求索|agent[-_\s]*relay|在线模型|外援模型)",
    r"^\s*(?:请|麻烦|劳驾)?\s*(?:你\s*)?(?:现在|马上|立即)?\s*(?:帮我\s*)?(?:让|交给)\s*(?:deepseek|深度求索|agent[-_\s]*relay|在线模型|外援模型)",
    r"^\s*(?:call|use|ask)\s+(?:deepseek|agent[-_\s]*relay|the\s+online\s+model)",
]

EXPLICIT_RELAY_CANDIDATE_NEGATIONS = [
    r"(?:不要|不必|不用|无需|禁止|别|勿|请勿).*(?:deepseek|深度求索|agent[-_\s]*relay|在线模型|外援模型)",
    r"(?:do\s+not|don't|dont|no\s+need\s+to|without).*(?:deepseek|agent[-_\s]*relay|online\s+model)",
]

EXPLICIT_RELAY_META_PREFIXES = [
    r"^\s*(?:如果|假如|假设|比如|例如|譬如|当我说|用户说|文档说)",
    r"^\s*(?:我|我们)?\s*(?:只是|正在|想要)?\s*(?:讨论|描述|解释|评审|分析)(?:一下)?",
]

EXPLICIT_RELAY_META_TERMS = [
    r"(?:这句话|这个词|关键词|关键字|字样|触发规则|触发机制|误触发)",
]

# ============================================================
# 默认状态
# ============================================================

def default_state(session_id=None, agent_id=None):
    return {
        "schema_version": 2,
        "agent_id": agent_id,
        "session_id": session_id,
        "problem_active": False,
        "problem_id": None,
        "problem_status": "idle",

        # Orchestrator/Commander identity.  A child receives an explicit
        # task_id from its parent; it must never infer one from a session.
        "task_id": None,
        "relay_round": 0,

        "created_at": None,
        "updated_at": None,

        "initial_prompt": None,
        "last_prompt": None,
        "problem_prompts": [],
        "last_assistant_message": None,

        "turn_count": 0,
        "turn_closed": False,

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

        # Commander consumes the current relay round while the problem
        # history keeps the lifetime total for local inspection.
        "round_effective_time_seconds": 0.0,
        "cumulative_effective_time_seconds": 0.0,

        # 当前 Agent 主动处理区间的起点。UserPromptSubmit 开始计时，
        # PreToolUse / Stop 结算；下载、安装和网络等待期间暂停。
        "active_started_at": None,

        # 难度权重：
        #
        # < 5 分钟    -> 1
        # >= 5 分钟   -> 2
        # >= 15 分钟  -> 3
        #
        # 注意：
        # weight == 3 不直接触发 AgentRelay。
        "weight": 0,

        # 当前问题命中本地归档时记录的匹配信息。该信息只用于解释
        # 为什么从 weight=2 起算，不代表归档结论已经适用于当前环境。
        "archive_match": None,

        # 复发问题的权重下限。普通问题为 1；归档命中后为 2。
        # 时间增长到 15 分钟后仍会正常升到 weight=3。
        "weight_floor": 1,

        # 当前问题是否已经进入 AgentRelay 外援协作。
        # 该值只抑制 Hook 重复发送“首次求援”候选提醒，
        # 不禁止 Agent 在同一问题内携带新结果继续追问。
        "relay_triggered": False,

        # 当前问题的外援协作是否仍处于活动状态。
        "relay_collaboration_active": False,

        # 当前问题成功调用 AgentRelay 的次数（首次 + 后续追问）。
        "relay_call_count": 0,

        # 当前问题是否已经发出过首次求援候选提醒。
        # 这不代表 AgentRelay 已经调用成功。
        "relay_signal_emitted": False,

        # 触发通知只属于这个问题，避免旧状态污染新问题。
        "relay_signal_problem_id": None,

        # weight 达到 2 时是否已经提醒过 Agent 先查本地归档。
        # 只提醒一次，避免每个工具调用都重复刷屏。
        "archive_signal_emitted": False,
        "archive_signal_problem_id": None,

        # AgentRelay 首次进入外援协作的原因。
        "relay_trigger_reason": None,

        # 最近一次 AgentRelay 调用原因，后续追问时更新。
        "relay_last_reason": None,

        # Hook 从当前用户消息中发现了疑似直接调用句式。
        # 这只是交给 Agent 审核的候选，不是已经确认的显式请求。
        "explicit_relay_candidate": False,

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

        # 当前问题结束原因和时间，供会话内问题历史查看。
        "closed_reason": None,
        "closed_at": None,
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

    PROBLEMS_DIR.mkdir(
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

        # Migrate states written before Commander task envelopes existed.
        # Keep the problem id as a stable fallback while allowing an explicit
        # parent-assigned task id to remain isolated from sibling sessions.
        if not base.get("task_id"):
            base["task_id"] = base.get("problem_id")
        if "round_effective_time_seconds" not in data:
            base["round_effective_time_seconds"] = float(
                base.get("effective_time_seconds", 0) or 0
            )
        if "cumulative_effective_time_seconds" not in data:
            base["cumulative_effective_time_seconds"] = float(
                base.get("effective_time_seconds", 0) or 0
            )

        # 旧版曾通过关键词正则把用户文本直接判成显式调用请求。
        # 显式请求现在只由 Agent 做语义判断，Tracker 不再保留该状态。
        base.pop("explicit_relay_request", None)

        # v1 只有“每会话一份累计状态”。迁移后先视为上一轮已经结束，
        # 下一条消息再按问题相关性决定继续还是切分，避免旧计时无条件污染新问题。
        if int(data.get("schema_version", 1) or 1) < 2:
            base["schema_version"] = 2
            base["problem_status"] = (
                "active" if base.get("problem_active") else "idle"
            )
            base["initial_prompt"] = base.get("last_prompt")
            base["problem_prompts"] = (
                [base.get("last_prompt")] if base.get("last_prompt") else []
            )
            base["turn_closed"] = True
            base["active_started_at"] = None
            # Preserve the legacy v1 ladder for archived sessions during
            # migration (v1 raised weight 3 only at the 15-minute mark).
            legacy_seconds = float(base.get("effective_time_seconds", 0) or 0)
            if base.get("problem_active"):
                base["weight"] = 3 if legacy_seconds >= 15 * 60 else (2 if legacy_seconds >= 5 * 60 else 1)
            else:
                base["weight"] = 0

        # 如果 Session 状态文件缺少 session_id，
        # 使用当前调用传入的 session_id 修复。
        if session_id:
            base["session_id"] = session_id

        return base

    except Exception:
        return default_state(
            session_id=session_id,
            agent_id=agent_id
        )

def atomic_write_json(path, value):
    """原子写入 JSON，避免并发读取半截文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(path.suffix + ".tmp")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)

    os.replace(temp_file, path)


def write_problem_snapshot(state):
    """把当前问题写入其所属会话的问题目录。"""

    problem_file = get_problem_file(
        state.get("session_id"),
        state.get("problem_id"),
    )

    if problem_file is None:
        return

    atomic_write_json(problem_file, state)


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

    if not state.get("task_id") and state.get("problem_id"):
        state["task_id"] = state["problem_id"]
    effective = float(state.get("effective_time_seconds", 0) or 0)
    state["round_effective_time_seconds"] = float(
        state.get("round_effective_time_seconds", effective) or 0
    )
    state["cumulative_effective_time_seconds"] = max(
        float(state.get("cumulative_effective_time_seconds", effective) or 0),
        effective,
    )

    state["updated_at"] = iso_now()

    atomic_write_json(state_file, state)
    write_problem_snapshot(state)

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


def matches_any_pattern(text, patterns):
    text = normalize_text(text)
    return any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in patterns
    )


def contains_explicit_relay_candidate(text):
    """
    保守识别“现在调用外援”的行动句式。

    这里只为 Hook 生成候选提醒。引用、代码、假设、规则讨论和否定句
    不应产生候选；最终是否调用始终由 Agent 理解完整语境后决定。
    """

    text = normalize_text(text)
    if not text:
        return False

    # 先移除常见引用和代码区域，避免把示例命令当成当前行动指令。
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"[“‘][^”’\n]*[”’]", " ", text)
    text = re.sub(r'"[^"\n]*"', " ", text)

    # 按自然语句和逗号分段，既支持“先说明上下文，再下达调用指令”，
    # 也能排除“如果我说你调用 DeepSeek”这种假设片段。
    clauses = re.split(r"[。！？!?；;，,\n]+", text)

    for clause in clauses:
        clause = normalize_text(clause)
        if not clause:
            continue

        if matches_any_pattern(clause, EXPLICIT_RELAY_CANDIDATE_NEGATIONS):
            continue
        if matches_any_pattern(clause, EXPLICIT_RELAY_META_PREFIXES):
            continue
        if matches_any_pattern(clause, EXPLICIT_RELAY_META_TERMS):
            continue
        if matches_any_pattern(clause, EXPLICIT_RELAY_CANDIDATE_PATTERNS):
            return True

    return False


def assistant_indicates_resolution(text):
    """仅在助手明确声称完成且没有反向表述时判定解决。"""

    text = normalize_text(text)

    if not text or matches_any_pattern(text, UNRESOLVED_PATTERNS):
        return False

    return matches_any_pattern(text, RESOLUTION_PATTERNS)


def significant_terms(text):
    """提取保守的中英文关键词，用于区分同会话内的问题。"""

    text = normalize_text(text).lower()
    terms = set(
        re.findall(r"[a-z0-9_./:-]{3,}", text)
    )
    ignored = {
        "这个", "问题", "一下", "可以", "现在", "然后", "就是",
        "需要", "已经", "一个", "什么", "怎么", "处理", "修改",
        "检查", "告诉", "进行", "里面", "目前", "时候", "发现",
    }

    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for width in (2, 3):
            for index in range(max(0, len(chunk) - width + 1)):
                term = chunk[index:index + width]
                if term not in ignored:
                    terms.add(term)

    return terms


def prompt_belongs_to_current_problem(state, prompt):
    """保守判断新消息是否仍属于当前问题链。"""

    prompt = normalize_text(prompt)

    if matches_any_pattern(prompt, PAUSE_PROBLEM_PATTERNS):
        return False

    if matches_any_pattern(prompt, NEW_PROBLEM_PATTERNS):
        return False

    if matches_any_pattern(prompt, CONTINUATION_PATTERNS):
        return True

    # Agent 尚未结束当前轮时收到的 steer，默认属于同一问题。
    if not state.get("turn_closed", False):
        return True

    prompts = state.get("problem_prompts") or []
    corpus = " ".join(
        [state.get("initial_prompt") or ""]
        + [normalize_text(value) for value in prompts[-8:]]
    )
    current_terms = significant_terms(corpus)
    new_terms = significant_terms(prompt)
    shared = current_terms & new_terms

    if len(shared) >= 2:
        return True

    # 路径、错误码、命令或英文技术标识的精确复现足以认为是延续。
    if any(re.search(r"[a-z0-9_./:-]", term) for term in shared):
        return True

    return False


def pause_prompt_starts_new_problem(prompt):
    """判断暂停旧问题的同一条消息是否明确指定了新任务。"""

    prompt = normalize_text(prompt)
    return bool(re.search(
        r"(?:改为|转而|然后|接着|现在|先去|先来|[,，;；。]\s*先)"
        r"(?:再)?(?:处理|解决|检查|修改|实现|做)",
        prompt,
        re.IGNORECASE,
    ))


def accrue_active_time(state, ended_at=None):
    """结算 Agent 主动分析/思考时间，并刷新当前问题权重。"""

    started_at = state.get("active_started_at")
    state["active_started_at"] = None

    if started_at is None or not state.get("problem_active"):
        return 0.0

    ended_at = float(ended_at if ended_at is not None else now_ts())
    duration = max(0.0, ended_at - float(started_at))
    state["effective_time_seconds"] = (
        float(state.get("effective_time_seconds", 0) or 0)
        + duration
    )
    update_weight_from_time(
        state,
        state["effective_time_seconds"],
    )
    return duration


def start_active_time(state, started_at=None):
    """开始记录当前问题的主动处理时间。"""

    if state.get("problem_active"):
        state["active_started_at"] = float(
            started_at if started_at is not None else now_ts()
        )


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
    r"\bdocker\s+pull\b",
    r"\bdocker\s+build\b",
    r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b",
    r"\b(?:mvn|mvnw)\b.*\b(?:compile|package|install|test)\b",
    r"\b(?:gradle|gradlew)\b.*\b(?:build|test|assemble)\b",
    r"\bcargo\s+(?:build|test)\b",
    r"\bgo\s+(?:build|test)\b",
    r"\bxcodebuild\b",
    r"\bagent_relay\.py\b",
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

    >= 15 分钟：
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

    if effective_seconds >= WEIGHT_THREE_SECONDS:
        return 3

    if effective_seconds >= WEIGHT_TWO_SECONDS:
        return 2

    return 1


def update_weight_from_time(state, effective_seconds=None):
    """按有效时间刷新权重，同时保留复发问题的权重下限。"""

    if effective_seconds is None:
        effective_seconds = state.get(
            "round_effective_time_seconds",
            state.get("effective_time_seconds", 0),
        )

    floor = int(
        state.get("weight_floor", 1)
        or 1
    )
    floor = max(1, min(MAX_WEIGHT, floor))
    state["weight"] = max(
        calculate_weight(effective_seconds),
        floor,
    )
    return state["weight"]


def _sanitize_archive_project(project):
    """与本地归档脚本保持相同的项目名安全规则。"""

    cleaned = re.sub(
        r"[^0-9A-Za-z\u4e00-\u9fff._-]+",
        "-",
        str(project or "").strip(),
    )
    cleaned = cleaned.strip(".-")
    return cleaned or "default"


def infer_archive_project(workspace=None):
    """推断当前项目名，优先使用显式环境变量。"""

    explicit = os.environ.get("AGENTRELAY_PROJECT")
    if explicit:
        return _sanitize_archive_project(explicit)

    candidate = Path(workspace or os.getcwd()).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate

    marker = resolved
    while marker != marker.parent and not (marker / ".git").exists():
        marker = marker.parent

    if (marker / ".git").exists():
        return _sanitize_archive_project(marker.name)
    return _sanitize_archive_project(resolved.name)


def archive_root_path():
    """返回安装后 Skill 目录内的本地归档根目录。"""

    return (
        Path(ARCHIVE_SCRIPT)
        .expanduser()
        .resolve()
        .parent.parent
        / "归档"
    )


def load_archive_project_records(project):
    """读取当前项目的归档 JSONL；任何读取错误都按无归档处理。"""

    path = (
        archive_root_path()
        / _sanitize_archive_project(project)
        / ARCHIVE_RECORDS_FILENAME
    )

    if not path.is_file():
        return []

    records = []
    try:
        lines = path.read_text(
            encoding="utf-8",
        ).splitlines()
    except OSError:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("title"):
            records.append(value)

    return records


def _compact_archive_text(value):
    text = unicodedata.normalize(
        "NFKC",
        normalize_text(value),
    ).casefold()
    return re.sub(
        r"[\W_]+",
        "",
        text,
        flags=re.UNICODE,
    )


def _archive_tokens(value):
    return set(
        re.findall(
            r"[a-z0-9_+#.-]{2,}|[\u4e00-\u9fff]{2,}",
            unicodedata.normalize(
                "NFKC",
                normalize_text(value),
            ).casefold(),
        )
    )


def _dice_similarity(left, right):
    if not left or not right:
        return 0.0
    if len(left) < 2 or len(right) < 2:
        return 1.0 if left == right else 0.0

    left_grams = {
        left[index:index + 2]
        for index in range(len(left) - 1)
    }
    right_grams = {
        right[index:index + 2]
        for index in range(len(right) - 1)
    }
    if not left_grams or not right_grams:
        return 0.0
    return (
        2.0
        * len(left_grams & right_grams)
        / (len(left_grams) + len(right_grams))
    )


def _containment_similarity(left, right):
    """字符串包含时给一个稳定的高分，但短字段包含要降权。"""

    if not left or not right:
        return 0.0
    if left not in right and right not in left:
        return 0.0

    shorter = min(len(left), len(right))
    longer = max(len(left), len(right))
    if shorter < 4 or longer == 0:
        return 0.0

    coverage = shorter / longer
    if coverage < 0.35:
        return 0.0
    return min(1.0, 0.72 + 0.28 * coverage)


def archive_record_match_score(query, record):
    """计算新问题与原归档记录的保守相似度。"""

    query_compact = _compact_archive_text(query)
    if len(query_compact) < 4:
        return 0.0

    query_tokens = _archive_tokens(query)
    fields = (
        (record.get("title"), 1.0),
        (record.get("request"), 0.96),
        (record.get("summary"), 0.90),
        (" ".join(record.get("tags") or []), 0.84),
        (" ".join(record.get("changed_files") or []), 0.78),
    )

    best_score = 0.0

    for value, multiplier in fields:
        candidate_compact = _compact_archive_text(value)
        if not candidate_compact:
            continue

        ratio = difflib.SequenceMatcher(
            None,
            query_compact,
            candidate_compact,
            autojunk=False,
        ).ratio()
        ratio = max(
            ratio,
            _containment_similarity(
                query_compact,
                candidate_compact,
            ),
            _dice_similarity(
                query_compact,
                candidate_compact,
            ),
        )

        candidate_tokens = _archive_tokens(value)
        shared_tokens = query_tokens & candidate_tokens
        if shared_tokens and (
            len(shared_tokens) >= 2
            or any(len(token) >= 4 for token in shared_tokens)
        ):
            token_coverage = (
                len(shared_tokens)
                / min(len(query_tokens), len(candidate_tokens))
            )
            ratio = max(
                ratio,
                token_coverage,
            )

        best_score = max(
            best_score,
            min(1.0, ratio * multiplier),
        )

    return best_score


def find_archive_recurrence(prompt, workspace=None):
    """在当前项目归档里寻找保守的复发匹配。

    命中只设置 weight=2，作用是把流程推进到“先查归档”，不会设置
    weight=3，也不会直接触发在线支援模型。
    """

    query = normalize_text(prompt)
    if len(_compact_archive_text(query)) < 4:
        return None

    project = infer_archive_project(workspace)
    best_record = None
    best_score = 0.0

    for record in load_archive_project_records(project):
        score = archive_record_match_score(
            query,
            record,
        )
        if score > best_score:
            best_record = record
            best_score = score

    if best_record is None or best_score < ARCHIVE_MATCH_THRESHOLD:
        return None

    return {
        "record_id": str(best_record.get("record_id") or ""),
        "project": project,
        "title": str(best_record.get("title") or ""),
        "status": str(best_record.get("status") or ""),
        "summary": str(best_record.get("summary") or "")[:300],
        "score": round(best_score, 3),
        "matched_at": iso_now(),
    }


# ============================================================
# AgentRelay 触发条件
# ============================================================

def get_trigger_conditions(state):
    """
    统一计算是否应向 Agent 发出“考虑首次求援”的候选提醒。

    Tracker 判断两个可机械验证的计数条件，并对用户消息做保守的
    直接行动句式预筛选：

    1. retry_count >= 3
    2. effective_time >= 15 分钟

    3. 当前消息疑似直接要求调用 AgentRelay

    第 3 项只是候选，不能由 DeepSeek / AgentRelay 关键词本身成立。
    用户是否真的要求当前调用必须由 Agent 结合语境判断。

    即使计数条件满足，Hook 也只发候选提醒；是否值得调用以及是否
    与当前未解决问题匹配，最终由 Agent 判断。
    """

    retry_count = int(
        state.get(
            "retry_count",
            0,
        )
        or 0
    )

    round_value = state.get("round_effective_time_seconds")
    legacy_value = state.get("effective_time_seconds", 0)
    # Older callers only populated effective_time_seconds.  Treat a missing
    # or zero round value as legacy data unless the legacy value is also zero.
    effective_seconds = float(
        (legacy_value if (round_value is None or
                          (float(round_value or 0) == 0 and float(legacy_value or 0) > 0))
         else round_value) or 0
    )

    explicit_candidate = bool(
        state.get("explicit_relay_candidate", False)
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
        "cumulative_effective_time_seconds": float(state.get(
            "cumulative_effective_time_seconds", effective_seconds
        ) or 0),

        "time_trigger": (
            effective_seconds >= 15 * 60
        ),

        "explicit_candidate": explicit_candidate,

        "weight": int(
            state.get(
                "weight",
                0,
            )
            or 0
        ),

        "already_triggered": (
            already_triggered
        ),

        "signal_emitted": signal_emitted,

        "requires_agent_validation": True,
    }

    automatic_trigger = (
        conditions["retry_trigger"]
        or conditions["time_trigger"]
    )

    # 直接行动候选不受计数门槛限制；即使权重和重试为零，也要交给
    # Agent 审核。计数提醒则只为尚未进入外援协作的问题发送一次。
    conditions["should_trigger"] = (
        explicit_candidate
        or (
            automatic_trigger
            and not already_triggered
            and not signal_emitted
        )
    )

    if (
        conditions["explicit_candidate"]
    ):
        conditions["reason"] = "user_direct_request_candidate"

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
# AgentRelay 候选提醒输出
# ============================================================

def wants_archive_lookup(state, conditions):
    """
    权重到 2 时提醒 Agent 先查本地归档。

    权重分级动作：

        weight 1 -> 正常处理
        weight 2 -> 先查本地归档
        weight 3 -> 才进入“是否调用支援模型”的判断

    每个问题只提醒一次，避免每次工具调用都刷屏。
    """

    weight = int(
        conditions.get(
            "weight",
            0,
        )
        or 0
    )

    if weight < ARCHIVE_REMINDER_WEIGHT:
        return False

    if conditions.get("already_triggered"):
        return False

    problem_id = state.get("problem_id")

    if not problem_id:
        return False

    return not (
        state.get("archive_signal_emitted")
        and state.get("archive_signal_problem_id") == problem_id
    )


def archive_reminder_text(state):
    """权重到 2 时提示 Agent 查询本机归档。"""

    problem_context = normalize_text(
        state.get("last_prompt")
    )

    if len(problem_context) > 300:
        problem_context = (
            problem_context[:297]
            + "..."
        )

    match = (
        state.get("archive_match")
        if isinstance(state.get("archive_match"), dict)
        else {}
    )
    if match:
        match_text = (
            "归档复发预匹配: "
            f"{match.get('status') or '已归档'} · "
            f"{match.get('title') or '未命名记录'}"
            f"（项目: {match.get('project') or 'unknown'}，"
            f"相似度: {match.get('score', 0)}）。"
        )
        reason_text = (
            "Tracker 认为这可能是同一问题的复发，因此起始权重已是 2。"
            "请先运行 search 核对该记录是否真的适用于当前环境；"
            "匹配结果不是结论，不能直接照抄。"
        )
    else:
        match_text = ""
        reason_text = (
            "权重已经到 2，说明这个问题已经处理了一段时间："
            "先查一次本机归档，看以前是否解决过同类问题。"
        )

    return (
        "本地归档提醒（不是命令）。"
        f"当前权重: {state.get('weight')}。"
        f"{match_text}"
        f"当前问题: {problem_context or '未记录用户问题摘要'}。"
        f"{reason_text}"
        f'查询命令: python3 "{ARCHIVE_SCRIPT}" search "关键词"；'
        f'不确定项目名时先运行 python3 "{ARCHIVE_SCRIPT}" projects。'
        "命中时优先复用归档里已验证的结论，并在本次环境重新验证；"
        "没有命中再继续自行排查。归档只保存结论，不替 Agent 做决定，"
        "也不改变需求卡、确认卡和验证规则；weight=2 不会触发在线支援模型。"
    )


def emit_relay_trigger(state, hook_event_name="PostToolUse"):
    """
    向 Agent 输出候选提醒。

    两类提醒：

    1. 权重到 2：先查本地归档
    2. 达到求援门槛或出现直接行动句式候选：考虑调用支援模型

    注意：
    - 这里只负责提醒 Agent 做语义审核
    - 提醒本身不是调用命令，也不是已经确认的触发结论
    - 不在 Tracker 内直接调用 agent_relay.py
    - 不修改 state["relay_triggered"]
    """

    conditions = get_trigger_conditions(state)

    relay_due = bool(
        conditions.get("should_trigger")
    )

    archive_due = wants_archive_lookup(
        state,
        conditions,
    )

    if not relay_due and not archive_due:
        return False

    problem_context = normalize_text(
        state.get("last_prompt")
    )

    if len(problem_context) > 500:
        problem_context = (
            problem_context[:497]
            + "..."
        )

    notices = []

    if archive_due:
        notices.append(
            archive_reminder_text(state)
        )

    if relay_due:
        notices.append(
            "AgentRelay 候选求援提醒（不是调用命令）。"
            f"候选原因: {conditions.get('reason')}。"
            f"retry_count: {conditions.get('retry_count')}。"
            "effective_time_seconds: "
            f"{int(conditions.get('effective_time_seconds', 0))}。"
            f"当前问题: {problem_context or '未记录用户问题摘要'}。"
            "模型判断优先级最高：必须先核对问题仍未解决、提醒与当前问题匹配、"
            "当前不是单纯等待，并判断外援是否确实值得调用。只有模型审核通过后"
            "才可根据 AgentRelay Skill 调用；审核不通过就忽略本提醒并继续正常处理。"
            "Hook 不会调用 agent_relay.py；用户消息候选只来自保守的直接行动句式预筛选，"
            "单独出现 DeepSeek 或 AgentRelay 字样不能触发。"
        )

    additional_context = "\n".join(notices)

    if hook_event_name == "Stop":
        output = {
            "decision": "block",
            "reason": additional_context,
        }
    else:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": additional_context,
            },
        }

    print(json.dumps(output, ensure_ascii=False), flush=True)

    # 显式行动候选是一次性消息，不消耗当前问题未来的计数门槛提醒。
    if relay_due and not conditions.get("explicit_candidate"):
        state["relay_signal_emitted"] = True
        state["relay_signal_problem_id"] = state.get(
            "problem_id"
        )

    if archive_due:
        state["archive_signal_emitted"] = True
        state["archive_signal_problem_id"] = state.get(
            "problem_id"
        )

    state["explicit_relay_candidate"] = False

    write_state(state, session_id=state.get("session_id"))

    return True

# ============================================================
# 问题生命周期
# ============================================================

def archive_current_problem(
    state,
):
    """
    将当前问题归档到所属会话的问题目录：

        ~/.codex/agent_relay_tracker/problems/<session_id>/

    每个问题一个 JSON 文件。
    """

    problem_id = state.get(
        "problem_id"
    )

    if not problem_id:
        return

    try:
        write_problem_snapshot(state)
    except Exception:
        # 归档失败不能影响 Codex。
        pass


def close_problem_state(state, status, reason=None):
    """停止当前问题计时并保存独立快照。"""

    accrue_active_time(state)
    state["problem_active"] = False
    state["problem_status"] = status
    state["resolved"] = status == "resolved"
    state["closed_reason"] = normalize_text(reason) or status
    state["closed_at"] = iso_now()
    state["turn_closed"] = True
    state["pending_tools"] = {}
    state["relay_collaboration_active"] = False
    archive_current_problem(state)
    return state


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

    如果当前项目归档里保守命中同一问题，则只把起始权重抬到 2，
    让 Hook 先提醒 Agent 查询归档；不会直接抬到 3 或调用支援模型。
    """

    problem_id = (
        generate_problem_id()
    )

    state = default_state(
        session_id=session_id,
        agent_id=agent_id,
    )

    state["problem_active"] = True
    state["problem_status"] = "active"

    state["problem_id"] = (
        problem_id
    )
    state["task_id"] = problem_id

    state["created_at"] = iso_now()

    state["updated_at"] = iso_now()

    normalized_prompt = normalize_text(prompt)
    archive_match = find_archive_recurrence(
        normalized_prompt,
    )
    if archive_match:
        state["archive_match"] = archive_match
        state["weight_floor"] = ARCHIVE_REMINDER_WEIGHT

    update_weight_from_time(state, 0)

    state["initial_prompt"] = normalized_prompt
    state["last_prompt"] = normalized_prompt
    state["problem_prompts"] = (
        [normalized_prompt] if normalized_prompt else []
    )

    state["turn_count"] = 1
    state["turn_closed"] = False
    start_active_time(state)

    state["explicit_relay_candidate"] = (
        contains_explicit_relay_candidate(prompt)
    )

    log_event(
        "problem_new",
        {
            "problem_id": problem_id,
            "prompt": normalized_prompt,
            "weight": state.get("weight", 1),
            "archive_match": archive_match,
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
            close_problem_state(
                old_state,
                "superseded",
                "manual_new_problem",
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

    已解决问题保留在会话问题历史；当前计时器立即回到空闲零值。
    """

    lock = acquire_lock()

    try:
        state = read_state(session_id)

        if state.get(
            "problem_active"
        ):
            agent_id = state.get("agent_id")
            close_problem_state(
                state,
                "resolved",
                "verified_complete",
            )

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

            state = default_state(
                session_id=session_id,
                agent_id=agent_id,
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
    标记当前问题已经进入 AgentRelay 外援协作。

    注意：

    这里不负责启动 AgentRelay。

    这里只记录当前问题已经进入外援协作，以及成功调用次数。
    第一次调用和携带新结果的后续追问都使用同一个当前问题状态。

    真正调用 AgentRelay Playwright
    由 Skill / 外部流程负责。
    """

    lock = acquire_lock()

    try:
        state = read_state(session_id)

        first_call = not bool(state.get("relay_triggered", False))
        normalized_reason = normalize_text(reason) or "unknown"

        state["relay_triggered"] = True
        state["relay_collaboration_active"] = True
        state["relay_call_count"] = (
            int(state.get("relay_call_count", 0) or 0) + 1
        )
        state["relay_last_reason"] = normalized_reason

        if first_call or not state.get("relay_trigger_reason"):
            state["relay_trigger_reason"] = normalized_reason

        log_event(
            "relay_triggered" if first_call else "relay_followup",
            {
                "problem_id": (
                    state.get(
                        "problem_id"
                    )
                ),

                "reason": (
                    normalized_reason
                ),

                "relay_call_count": state.get("relay_call_count", 1),

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

        write_state(state, session_id)

        return state

    finally:
        release_lock(lock)


def confirm_relay_success(reason=None, session_id=None, task_id=None):
    """Record a successful expert call and begin a fresh relay round.

    The problem remains active and its cumulative time is retained, while
    retry/time counters are reset for the next round.  A mismatched task id
    is rejected so a sibling Commander child cannot mutate this session.
    """
    state = read_state(session_id)
    current_task = state.get("task_id") or state.get("problem_id")
    if task_id and current_task != task_id:
        return None
    state = mark_relay(reason or "expert_success", session_id=session_id)
    lock = acquire_lock()
    try:
        state = read_state(session_id)
        state["relay_round"] = int(state.get("relay_round", 0) or 0) + 1
        state["round_effective_time_seconds"] = 0.0
        state["effective_time_seconds"] = 0.0
        state["retry_count"] = 0
        # 专家调用完成即开启新轮次；归档提醒已经完成，不再保留复发
        # 权重下限。归档匹配元数据仍保留，便于审计。
        state["weight_floor"] = 1
        update_weight_from_time(state, 0)
        state["relay_signal_emitted"] = False
        state["relay_signal_problem_id"] = None
        state["relay_collaboration_active"] = True
        write_state(state, session_id)
        return state
    finally:
        release_lock(lock)


def task_context_snapshot(state):
    """Return the minimal task event envelope consumed by RuntimeManager."""
    round_value = state.get("round_effective_time_seconds")
    legacy_value = state.get("effective_time_seconds", 0)
    round_time = float((legacy_value if (round_value is None or
                                         (float(round_value or 0) == 0 and float(legacy_value or 0) > 0))
                        else round_value) or 0)
    conditions = get_trigger_conditions({**state, "effective_time_seconds": round_time})
    return {
        "task_id": state.get("task_id") or state.get("problem_id"),
        "retry_count": int(state.get("retry_count", 0) or 0),
        "weight": int(state.get("weight", 0) or 0),
        "need_escalation": bool(conditions.get("retry_trigger") or conditions.get("time_trigger")),
        "effective_time_seconds": round_time,
        "problem_active": bool(state.get("problem_active", False)),
    }
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
    4. 显式 AgentRelay 请求留给 Agent 做语义判断，Hook 不扫描关键词
    """

    prompt = normalize_text(prompt)

    lock = acquire_lock()

    try:
        state = read_state(
            session_id=session_id
        )

        pause_requested = matches_any_pattern(
            prompt,
            PAUSE_PROBLEM_PATTERNS,
        )

        # 没有当前问题
        if not state.get(
            "problem_active"
        ):
            if pause_requested and not pause_prompt_starts_new_problem(prompt):
                state = default_state(
                    session_id=session_id,
                    agent_id=state.get("agent_id"),
                )
                write_state(state, session_id=session_id)
                return state

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

        # 暂停旧问题时，它的时间、权重和重试立即停止参与当前判断。
        if pause_requested:
            old_problem_id = state.get("problem_id")
            agent_id = state.get("agent_id")
            close_problem_state(
                state,
                "paused",
                "user_paused",
            )

            if pause_prompt_starts_new_problem(prompt):
                state = create_new_problem(
                    prompt,
                    session_id=session_id,
                    agent_id=agent_id,
                )
            else:
                state = default_state(
                    session_id=session_id,
                    agent_id=agent_id,
                )

            log_event(
                "problem_paused",
                {
                    "session_id": session_id,
                    "problem_id": old_problem_id,
                },
            )
            write_state(state, session_id=session_id)
            return state

        # 上一轮已经结束且新消息与旧问题无关，自动切成独立问题。
        if not prompt_belongs_to_current_problem(state, prompt):
            old_problem_id = state.get("problem_id")
            agent_id = state.get("agent_id")
            close_problem_state(
                state,
                "superseded",
                "new_user_problem",
            )
            state = create_new_problem(
                prompt,
                session_id=session_id,
                agent_id=agent_id,
            )
            log_event(
                "problem_switched",
                {
                    "session_id": session_id,
                    "previous_problem_id": old_problem_id,
                    "problem_id": state.get("problem_id"),
                },
            )
            write_state(state, session_id=session_id)
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
        prompts = list(state.get("problem_prompts") or [])
        if prompt:
            prompts.append(prompt)
        state["problem_prompts"] = prompts[-12:]
        state["turn_closed"] = False
        if state.get("active_started_at") is None:
            start_active_time(state)

        # Hook 只标记疑似直接行动句式，最终是否调用由 Agent 审核。
        state["explicit_relay_candidate"] = (
            contains_explicit_relay_candidate(prompt)
        )

        if state["explicit_relay_candidate"]:
            log_event(
                "explicit_relay_candidate",
                {
                    "problem_id": state.get("problem_id"),
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

        # UserPromptSubmit 到工具启动之间是 Agent 的主动分析/思考时间。
        accrue_active_time(state, ended_at=started_at)

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
            current_round_time = float(state.get(
                "round_effective_time_seconds",
                state.get("effective_time_seconds", 0),
            ) or 0)
            current_total_time = float(state.get(
                "cumulative_effective_time_seconds",
                state.get("effective_time_seconds", 0),
            ) or 0)
            state["round_effective_time_seconds"] = current_round_time + duration
            state["cumulative_effective_time_seconds"] = current_total_time + duration
            # Keep the legacy field as the current round for trigger consumers.
            state["effective_time_seconds"] = state["round_effective_time_seconds"]

        # 工具结束后 Agent 继续分析，重新开始主动处理计时。
        start_active_time(state)

        # ====================================================
        # Weight
        # ====================================================

        update_weight_from_time(
            state,
            float(
                state.get(
                    "round_effective_time_seconds",
                    state.get("effective_time_seconds", 0),
                )
                or 0
            ),
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

def handle_stop(session_id=None, last_assistant_message=None):
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

        accrue_active_time(state)
        assistant_message = normalize_text(last_assistant_message)
        state["last_assistant_message"] = assistant_message
        stop_event_state = state

        # 如果门槛是在最后一段思考期间达到，Stop Hook 阻止本轮直接结束，
        # 把候选提醒送回 Agent 做最终语义审核。
        if state.get("problem_active") and emit_relay_trigger(
            state,
            hook_event_name="Stop",
        ):
            state["turn_closed"] = False
            start_active_time(state)
            write_state(state, session_id=session_id)
            return state

        if (
            state.get("problem_active")
            and assistant_indicates_resolution(assistant_message)
        ):
            completed_state = state
            agent_id = state.get("agent_id")
            close_problem_state(
                completed_state,
                "resolved",
                "assistant_verified_complete",
            )
            state = default_state(
                session_id=session_id,
                agent_id=agent_id,
            )
        elif state.get("problem_active"):
            state["turn_closed"] = True

        log_event(
            "stop",
            {
                "problem_id": (
                    stop_event_state.get(
                        "problem_id"
                    )
                ),

                "retry_count": (
                    stop_event_state.get(
                        "retry_count",
                        0,
                    )
                ),

                "effective_time_seconds": (
                    stop_event_state.get(
                        "effective_time_seconds",
                        0,
                    )
                ),

                "weight": (
                    stop_event_state.get(
                        "weight",
                        1,
                    )
                ),

                "failed_tool_call_count": (
                    stop_event_state.get(
                        "failed_tool_call_count",
                        0,
                    )
                ),

                "problem_status": stop_event_state.get("problem_status"),
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

def latest_session_id():
    """返回最近更新的会话，供无需手查 ID 的状态命令使用。"""

    ensure_dirs()
    candidates = list(SESSIONS_DIR.glob("*.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime).stem


def problem_summaries(session_id):
    """列出指定会话下相互独立的问题快照。"""

    session_id = normalize_session_id(session_id)
    if not session_id:
        return []

    directory = PROBLEMS_DIR / session_id
    values = []
    for path in directory.glob("*.json") if directory.is_dir() else []:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        values.append({
            "problem_id": state.get("problem_id"),
            "status": state.get("problem_status", "active"),
            "initial_prompt": state.get("initial_prompt") or state.get("last_prompt"),
            "last_prompt": state.get("last_prompt"),
            "effective_time_seconds": round(float(
                state.get("effective_time_seconds", 0) or 0
            ), 3),
            "weight": state.get("weight", 0),
            "retry_count": state.get("retry_count", 0),
            "relay_triggered": state.get("relay_triggered", False),
            "relay_collaboration_active": state.get(
                "relay_collaboration_active",
                False,
            ),
            "relay_call_count": int(
                state.get("relay_call_count", 0) or 0
            ),
            "created_at": state.get("created_at"),
            "closed_at": state.get("closed_at"),
        })

    values.sort(key=lambda value: value.get("created_at") or "")
    return values

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
        "session_id": state.get("session_id"),

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

        "problem_status": state.get("problem_status", "idle"),

        "initial_prompt": state.get("initial_prompt"),

        "last_prompt": state.get("last_prompt"),

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

        "round_effective_time_seconds": round(float(state.get(
            "round_effective_time_seconds", effective_seconds
        ) or 0), 3),
        "cumulative_effective_time_seconds": round(float(state.get(
            "cumulative_effective_time_seconds", effective_seconds
        ) or 0), 3),

        "effective_time_minutes": round(
            effective_seconds / 60,
            2,
        ),

        "weight": (
            state.get(
                "weight",
                0,
            )
        ),

        "relay_triggered": (
            state.get(
                "relay_triggered",
                False,
            )
        ),

        "relay_collaboration_active": (
            state.get(
                "relay_collaboration_active",
                False,
            )
        ),

        "relay_call_count": int(
            state.get("relay_call_count", 0) or 0
        ),

        "relay_trigger_reason": (
            state.get(
                "relay_trigger_reason"
            )
        ),

        "relay_last_reason": state.get("relay_last_reason"),

        "explicit_relay_candidate": bool(
            state.get("explicit_relay_candidate", False)
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
        sessions
        problems [--session SESSION_ID]
        new [--session SESSION_ID] [PROMPT]
        retry --session SESSION_ID
        branch --session SESSION_ID
        resolve --session SESSION_ID
        mark-relay --session SESSION_ID REASON
        reset --session SESSION_ID
    """

    ensure_dirs()

    if len(sys.argv) < 2:
        session_id = normalize_session_id(
            os.environ.get("CODEX_SESSION_ID")
        ) or latest_session_id()
        print_status(read_state(session_id=session_id))
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
            session_id = (
                session_id
                or normalize_session_id(os.environ.get("CODEX_SESSION_ID"))
                or latest_session_id()
            )
            state = read_state(
                session_id=session_id
            )

            print_status(state)

            return 0

        if command == "snapshot":
            state = read_state(session_id=session_id)
            snapshot = task_context_snapshot(state)
            snapshot.update({
                "request": state.get("last_prompt") or "",
                "session_id": state.get("session_id") or session_id,
                "event_type": "TRACKER_SNAPSHOT",
            })
            print(json.dumps(snapshot, ensure_ascii=False))
            return 0

        # ----------------------------------------------------
        # sessions / problems
        # ----------------------------------------------------

        if command == "sessions":
            values = []
            for path in sorted(
                SESSIONS_DIR.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ):
                state = read_state(session_id=path.stem)
                values.append({
                    "session_id": path.stem,
                    "problem_id": state.get("problem_id"),
                    "problem_status": state.get("problem_status", "idle"),
                    "last_prompt": state.get("last_prompt"),
                    "effective_time_seconds": round(float(
                        state.get("effective_time_seconds", 0) or 0
                    ), 3),
                    "weight": state.get("weight", 0),
                    "retry_count": state.get("retry_count", 0),
                    "relay_triggered": state.get("relay_triggered", False),
                    "relay_collaboration_active": state.get(
                        "relay_collaboration_active",
                        False,
                    ),
                    "relay_call_count": int(
                        state.get("relay_call_count", 0) or 0
                    ),
                })
            print(json.dumps(values, ensure_ascii=False, indent=2))
            return 0

        if command == "problems":
            session_id = (
                session_id
                or normalize_session_id(os.environ.get("CODEX_SESSION_ID"))
                or latest_session_id()
            )
            print(json.dumps({
                "session_id": session_id,
                "problems": problem_summaries(session_id),
            }, ensure_ascii=False, indent=2))
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
            task_id = None
            if "--task" in args:
                index = args.index("--task")
                if index + 1 >= len(args):
                    print("Error: --task requires TASK_ID", file=sys.stderr)
                    return 1
                task_id = args[index + 1]
                args = args[:index] + args[index + 2:]
            state = confirm_relay_success(
                " ".join(args), session_id=session_id, task_id=task_id
            )
            if state is None:
                print("Error: task_id does not match current session", file=sys.stderr)
                return 1
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


def extract_last_assistant_message(data):
    return normalize_text(first_value(
        data,
        "last_assistant_message",
        "lastAssistantMessage",
        "assistant_message",
        "assistantMessage",
    ))


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
            session_id=session_id,
            last_assistant_message=extract_last_assistant_message(data),
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
