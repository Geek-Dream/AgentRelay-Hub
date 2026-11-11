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
import ntpath
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
from collections import namedtuple
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


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


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _utf8_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _detached_process_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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

# 求援提醒升级计划。发出第 n 次提醒后如果模型仍放弃（未发起外援
# 协作、问题未解决），等待对应秒数进入下一步：
#
#   第 1 次提醒 -> 等 600 秒 -> 第 2 次提醒
#   第 2 次提醒 -> 等 300 秒 -> 第 3 次提醒（最后一次）
#   第 3 次提醒 -> 等 300 秒 -> Hook 直接强制执行外援调用
#
# 强制执行由 Hook 后台启动 agent_relay.py，结果就绪后注入后续上下文。
RELAY_ESCALATION_WAIT_SECONDS = {1: 600, 2: 300, 3: 300}
RELAY_MAX_SIGNALS = 3

HOOK_CARD_DIVIDER = "--------------------------------"
CHANGE_HISTORY_LIMIT = 2
CHANGE_HISTORY_TEXT_WIDTH = 68


def format_command(
    parts,
    *,
    platform_name=None,
):
    """Render a display/CLI command with syntax native to the current host."""

    values = [str(part) for part in parts]
    if (platform_name or os.name) == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


def active_python_command():
    return str(sys.executable or ("py" if os.name == "nt" else "python3"))


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

TASK_APPROVAL_PATTERNS = [
    r"^(?:确认|批准|同意)(?:执行|实施|修改|继续|这个方案|该方案|上述方案|方案|计划|需求|确认卡|并|[,，。.!！]|$)",
    r"^(?:可以|好的|行|继续|开始|执行|动手|做吧)(?:执行|实施|修改|开始|继续|按|做|吧|[,，。.!！]|$)",
    r"^(?:按|按照)(?:这个|上述|该|刚才的|以下)?(?:方案|计划|需求|确认卡|步骤)"
    r"(?:执行|实施|做|修改|继续|吧)",
    r"^(?:无需|不用|不必|跳过)(?:再次|再)?确认(?:[,，。.!！\s]|$)",
    r"^(?:approve|approved|proceed|go\s+ahead|continue|yes|do\s+it)(?:[,，。.!！\s]|$)",
]

TASK_APPROVAL_NEGATIONS = [
    r"(?:先别|暂时别|不要|不能|不允许|不批准|不同意|拒绝|等等|稍等)",
    r"(?:但是|但|不过|然而|如果|假如)",
    r"(?:do\s+not|don't|dont|wait|not\s+yet|reject)",
]

TASK_APPROVAL_QUESTIONS = [
    r"[?？]$",
    r"(?:吗|呢|么|是否|为什么|为何|怎么|如何|什么|风险|影响|区别)[?？。.!！]?$",
]

GATE_RESPONSE_CANDIDATE_PATTERNS = TASK_APPROVAL_PATTERNS + [
    r"^(?:嗯+|没问题|没意见|收到|明白|了解|知道了|可以|你看着办|就这么办)",
    r"^(?:不行|不要|不用|先别|等等|稍等|取消|暂停|拒绝|算了|不批准|不同意)",
    r"^\[?[123]\]?(?:[,，。.!！\s]|$)",
    r"(?:吗|呢|么|是否|为什么|为何|怎么|如何|什么|风险|影响|区别)[?？。.!！]?$",
]

GATE_APPROVAL_BLOCKERS = TASK_APPROVAL_NEGATIONS + TASK_APPROVAL_QUESTIONS + [
    r"(?:先说明|先说清楚|再考虑|还没决定|不确定|如果|假如|假设)",
    r"\b(?:but|however|if|unless|maybe|perhaps)\b",
    r"[?？]",
]

INFORMATIONAL_PROMPT_PATTERNS = [
    r"^\s*(?:是否|为什么|为何|什么是|是什么|怎么|如何)",
    r"(?:是干嘛的|是干什么的|什么意思|是什么机制|有什么作用|有什么影响)",
    r"(?:解释|说明一下|介绍一下|告知我|告诉我|讲讲|说说)",
    r"(?:吗|呢|么|还是|是否|为什么|为何|怎么|如何|什么)[?？。.!！\s]*$",
    r"[?？]\s*$",
]

INFORMATIONAL_DIRECT_ACTION_PATTERNS = [
    r"^\s*(?:请|麻烦|帮我|给我|现在|马上|立即|立刻)"
    r"[^。！？!?\n]{0,24}(?:修复|修改|实现|安装|装上|运行|执行|删除|创建|写入|"
    r"提交|部署|截图|验证|检查|审计|测试|重试|重装|同步|更新|清理|开始)",
    r"^\s*(?:修复|修改|实现|安装|运行|执行|删除|创建|写入|提交|部署|截图|"
    r"验证|检查|审计|测试|重试|重装|同步|更新|清理|开始)",
    r"^\s*(?:能不能|能否|可否|可以|可不可以)"
    r"[^。！？!?\n]{0,16}(?:帮我|给我)?"
    r"[^。！？!?\n]{0,12}(?:修复|修改|实现|安装|装上|运行|执行|删除|截图|验证|检查|审计|测试|清理|更新)",
]

TASK_WRITE_ACTION_PATTERNS = [
    r"(?:修改|新增|增加|添加|删除|重构|迁移|升级|替换|改成|改为|改造|修复|接入|集成|实现|搭建|部署|发布|安装|配置|调整)",
    r"\b(?:implement|add|remove|delete|refactor|migrate|upgrade|replace|change\s+to|set\s+to|fix|integrate|build|deploy|install)\b",
]

TASK_USER_REQUIRED_PATTERNS = [
    r"必须(?:完整|严格|深度|逐项)?(?:审查|检查|验证|测试|核对)",
    r"(?:完整|严格|深度|逐项)(?:审查|检查|验证)",
    r"(?:运行|执行)?(?:全量|全部|完整)(?:测试|回归|验证)",
    r"不能只看\s*diff|不能只跑(?:一个|单个)",
    r"must\s+(?:fully\s+)?(?:review|verify|test)|full\s+(?:review|test)",
]

TASK_LARGE_PATTERNS = [
    r"(?:大型任务|大工程|完整实现|从零实现|整体重构|全面重构|系统性改造)",
    r"(?:整个项目|全项目|跨模块|全链路|端到端|前后端|系统架构|架构重构)",
    r"(?:数据库迁移|迁移数据|数据迁移|消息队列迁移|鉴权体系|权限体系)",
    r"(?:部署上线|上线部署|重启服务|升级依赖|安装多个依赖)",
]

TASK_MEDIUM_PATTERNS = [
    r"(?:重构|迁移|安装依赖|升级依赖|改配置|修改配置|新增服务|重启服务|部署)",
    r"(?:表结构|鉴权流程|权限模型|消息协议|并发模型|数据模型)",
]

TASK_MULTI_REQUEST_PATTERNS = [
    r"(?:并且|同时|另外|然后|接着|还要|还需要|以及|最后)",
    r"(?:^|[;；])\s*(?:再|然后|接着|另外|最后)?(?:修改|新增|删除|实现|补|加|改)",
    r"(?:^|\n)\s*(?:\d+[.)、]|[-*])\s+",
]

TASK_FILE_REFERENCE_PATTERN = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)*[\w.-]+"
    r"\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|json|ya?ml|toml|md|css|scss|sass|"
    r"less|html|rs|go|java|kt|kts|sql|sh|bash|zsh|swift|c|cc|cpp|h|hpp|"
    r"xml|gradle|properties|env)\b",
    re.IGNORECASE,
)

TASK_DOMAIN_PATTERNS = [
    r"(?:前端|frontend|ui|页面)",
    r"(?:后端|backend|服务端|api|接口)",
    r"(?:数据库|database|db|sql|表结构)",
    r"(?:缓存|cache|redis|消息队列|mq|kafka|rabbitmq)",
    r"(?:部署|deploy|docker|kubernetes|k8s|服务器)",
    r"(?:测试|test|pytest|jest|vitest|playwright)",
]

TASK_DANGEROUS_REQUEST_PATTERNS = [
    r"(?:rm\s+-rf|删除全部|清空数据库|drop\s+(?:database|table))",
    r"(?:git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f|强制推送|force\s+push)",
    r"(?:sudo|管理员权限|关闭系统|重启系统|删除分支|删库)",
]

WRITE_TOOL_NAMES = {
    "apply_patch",
    "applypatch",
    "edit",
    "edit_file",
    "write",
    "write_file",
    "multiedit",
    "multi_edit",
    "notebookedit",
    "notebook_edit",
    "create_file",
    "delete_file",
    "str_replace_editor",
    "search_replace",
    "replace_file_content",
}

SHELL_TOOL_NAMES = {
    "bash",
    "shell",
    "exec",
    "exec_command",
    "local_shell",
    "shell_command",
    "run_command",
    "terminal",
    "command",
}

SHELL_INTERPRETERS = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
}

SHELL_WRITE_COMMANDS = {
    "add-content",
    "clear-content",
    "copy",
    "copy-item",
    "cp",
    "del",
    "erase",
    "install",
    "md",
    "mkdir",
    "move",
    "move-item",
    "mv",
    "new-item",
    "out-file",
    "patch",
    "ren",
    "rename",
    "rename-item",
    "remove-item",
    "rd",
    "rm",
    "rmdir",
    "robocopy",
    "set-content",
    "tee",
    "touch",
    "truncate",
    "unlink",
    "xcopy",
}

SHELL_CONTROL_OPERATORS = {
    ";",
    "&&",
    "||",
    "|",
    "|&",
    "&",
}

SHELL_OUTPUT_REDIRECT_OPERATORS = {
    ">",
    ">>",
    ">|",
    "&>",
    "&>>",
    "<>",
    ">&",
}

SHELL_TOKEN_OPERATORS = (
    "<<<",
    "<<-",
    "&>>",
    ";;",
    "&&",
    "||",
    "|&",
    ">>",
    ">|",
    "&>",
    ">&",
    "<>",
    "<<",
    ";",
    "&",
    "|",
    ">",
    "<",
    "(",
    ")",
)

_ShellToken = namedtuple(
    "_ShellToken",
    ("value", "kind", "quoted"),
)

_ShellSegment = namedtuple(
    "_ShellSegment",
    ("tokens", "joiner"),
)

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

        # 一轮真实写任务的简短修改记录。Stop 最终放行时写入独立历史，
        # 纯问答和只读检查保持静默。
        "change_tracking": {
            "turn_id": None,
            "started_at": None,
            "request": None,
            "workspace": None,
            "files": [],
            "file_groups": [],
            "summary": None,
            "tools": [],
            "had_write": False,
            "recorded": False,
            "recorded_at": None,
        },

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

        # 求援提醒升级阶梯：已发出的提醒次数（0-3）、最近一次提醒的
        # 时间戳（unix 秒）以及当时的 retry_count。模型放弃提醒后按
        # RELAY_ESCALATION_WAIT_SECONDS 的间隔升级重发，第 3 次之后
        # 由 Hook 直接强制执行外援调用。
        "relay_signal_count": 0,
        "relay_last_signal_at": None,
        "relay_signal_retry_count": 0,

        # 强制执行进度：None=未启动，"launched"=后台调用进行中，
        # "delivered"=结果已注入上下文。
        "relay_force_state": None,

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

        # UserPromptSubmit 的本地任务预检。它只使用确定性规则，
        # 不调用第二个模型；中大型任务和涉及多个修改点的任务会要求
        # 主 Agent 先展示确认卡，等待用户明确批准。
        "task_gate": {
            "level": "small",
            "requires_confirmation": False,
            "status": "clear",
            "reasons": [],
            "request_sha256": None,
            "notified_sha256": None,
            "requested_at": None,
            "approved_at": None,
        },

        # PreToolUse 的逐工具审批。高风险命令保存的是不可逆指纹，
        # 不保存原始命令；批准只对完全相同的工具输入生效一次。
        "tool_approval": {
            "pending_keys": [],
            "approved_keys": [],
        },

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

    macOS / Linux 使用 fcntl，Windows 使用 msvcrt。
    两个原生锁都不可用时才退化为空操作。
    """

    ensure_dirs()

    fp = open(
        LOCK_FILE,
        "a+b",
    )

    if fcntl is not None:
        fcntl.flock(
            fp.fileno(),
            fcntl.LOCK_EX,
        )
    elif msvcrt is not None:
        fp.seek(0, os.SEEK_END)
        if fp.tell() == 0:
            fp.write(b"\0")
            fp.flush()
        while True:
            try:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.05)

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
        elif msvcrt is not None:
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
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


def strip_prompt_examples(prompt):
    """移除代码块和引用行，避免把说明文字误判成当前任务。"""

    text = str(prompt or "")
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(">")
    )
    return normalize_text(text)


def is_informational_prompt(prompt):
    """Return whether a message asks for an explanation instead of an action."""

    candidate = strip_prompt_examples(prompt)
    if not candidate:
        return False
    if matches_any_pattern(candidate, INFORMATIONAL_DIRECT_ACTION_PATTERNS):
        return False
    return matches_any_pattern(candidate, INFORMATIONAL_PROMPT_PATTERNS)


def is_explicit_task_approval(prompt):
    """Return a legacy keyword hint only; this function never authorizes."""

    candidate = strip_prompt_examples(prompt)
    if not candidate or len(candidate) > 240:
        return False

    if matches_any_pattern(candidate, TASK_APPROVAL_NEGATIONS):
        return False

    if matches_any_pattern(candidate, TASK_APPROVAL_QUESTIONS):
        return False

    # 允许“好的，确认执行”一类自然表达，但保留单独的“好的/可以”。
    without_greeting = re.sub(
        r"^(?:嗯+|好的|好|okay|ok)(?:\s*[,，。.!！]\s*|\s+)",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip()
    if without_greeting:
        candidate = without_greeting

    return matches_any_pattern(candidate, TASK_APPROVAL_PATTERNS)


def prompt_sha256(prompt):
    """Return the exact source hash used by semantic approval resolution."""

    return hashlib.sha256(
        normalize_text(prompt).encode("utf-8")
    ).hexdigest()


def pending_approval_scope(state):
    """Describe only the approvals that are still pending in one session."""

    gate = state.get("task_gate")
    task_pending = (
        isinstance(gate, dict)
        and gate.get("status") == "awaiting_confirmation"
    )
    approvals = state.get("tool_approval")
    pending_keys = (
        approvals.get("pending_keys", [])
        if isinstance(approvals, dict)
        else []
    )
    pending_keys = [
        item for item in pending_keys
        if isinstance(item, str) and item
    ]
    contract = state.get("change_contract")
    contract_pending = (
        isinstance(contract, dict)
        and not contract.get("approved")
        and not contract.get("draft")
    )
    return {
        "task_request_sha256": (
            gate.get("request_sha256") if task_pending else None
        ),
        "tool_keys": pending_keys,
        "contract_sha256": (
            hashlib.sha256(
                json.dumps(
                    contract,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if contract_pending else None
        ),
    }


def has_pending_approval(scope):
    return bool(
        isinstance(scope, dict)
        and (
            scope.get("task_request_sha256")
            or scope.get("tool_keys")
            or scope.get("contract_sha256")
        )
    )


def is_gate_response_candidate(state, prompt):
    """Return whether Agent approval semantics may need to resolve a gate.

    This is deliberately not an authorization check. The main Agent sees the
    full conversation and decides whether the reply approves, rejects, asks a
    question, or changes scope.
    """

    if not has_pending_approval(pending_approval_scope(state)):
        return False
    candidate = strip_prompt_examples(prompt)
    if not candidate or len(candidate) > 800:
        return False
    if candidate.startswith("/"):
        return False
    if matches_any_pattern(candidate, GATE_RESPONSE_CANDIDATE_PATTERNS):
        return True
    # Natural replies such as "那就照你说的办" are still candidates. Safety
    # comes from the later semantic resolution, not from this prefilter.
    return len(candidate) <= 160


def approval_intent_blocker(prompt):
    """Veto approve writes for replies that are clearly not approval."""

    candidate = strip_prompt_examples(prompt)
    if matches_any_pattern(candidate, GATE_APPROVAL_BLOCKERS):
        return "用户消息含问句、否定、犹豫或条件，不能写回批准"
    return None


def classify_task_request(prompt, contract=None):
    """Apply the persisted approval tolerance without weakening hard gates."""
    text = strip_prompt_examples(prompt)
    try:
        module = _shared_script_module("approval_policy")
        policy = module.load_policy(CODEX_DIR)
        file_count = len({value.lower() for value in TASK_FILE_REFERENCE_PATTERN.findall(text)})
        domains = {value.lower() for value in re.findall(
            "|".join(f"(?:{pattern})" for pattern in TASK_DOMAIN_PATTERNS), text, flags=re.IGNORECASE)}
        required, reasons = module.should_confirm(text, policy["mode"], file_count, len(domains), contract)
        work_profile = module.select_work_profile(
            text, policy["mode"], file_count=file_count, domain_count=len(domains),
        )
        if work_profile.get("card_required"):
            required = True
            reasons = list(dict.fromkeys([
                *reasons,
                *(work_profile.get("reasons") or []),
            ]))
        return {"level": "large" if required and matches_any_pattern(text, TASK_LARGE_PATTERNS)
                else "medium" if required else "small",
                "requires_confirmation": required, "reasons": reasons, "mode": policy["mode"],
                "work_profile": work_profile}
    except (ImportError, OSError, ValueError, TypeError):
        pass
    if not text:
        return {
            "level": "small",
            "requires_confirmation": False,
            "reasons": [],
            "work_profile": {
                "required": False, "profile": None, "minimum": None,
                "card_required": False, "reasons": [],
            },
        }

    write_requested = matches_any_pattern(text, TASK_WRITE_ACTION_PATTERNS)
    user_required = matches_any_pattern(text, TASK_USER_REQUIRED_PATTERNS)
    large_scope = matches_any_pattern(text, TASK_LARGE_PATTERNS)
    medium_scope = matches_any_pattern(text, TASK_MEDIUM_PATTERNS)
    multi_request = matches_any_pattern(text, TASK_MULTI_REQUEST_PATTERNS)
    file_references = {
        value.lower()
        for value in TASK_FILE_REFERENCE_PATTERN.findall(text)
    }
    domains = {
        value.lower()
        for value in re.findall(
            "|".join(f"(?:{pattern})" for pattern in TASK_DOMAIN_PATTERNS),
            text,
            flags=re.IGNORECASE,
        )
    }

    reasons = []
    if large_scope and write_requested:
        reasons.append("属于大型、跨模块或全链路任务")
    if write_requested and multi_request:
        reasons.append("包含多个连续修改目标")
    if write_requested and len(file_references) >= 2:
        reasons.append("涉及多个代码文件")
    if write_requested and len(domains) >= 2:
        reasons.append("同时涉及多个技术领域")
    if write_requested and medium_scope:
        reasons.append("包含重构、迁移、配置、依赖或部署操作")

    if user_required and write_requested:
        reasons.append("用户明确要求完整检查或全量验证")
        reasons.append("非轻量任务需要现象、期望、定位、边界、疑点五栏确认卡")

    if reasons:
        level = "large" if large_scope and write_requested else "medium"
        minimum = "user_required" if user_required and write_requested else "strict"
        return {
            "level": level,
            "requires_confirmation": True,
            "reasons": reasons,
            "work_profile": {
                "required": write_requested,
                "profile": minimum if write_requested else None,
                "minimum": minimum if write_requested else None,
                "card_required": write_requested,
                "reasons": reasons,
            },
        }

    return {
        "level": "small",
        "requires_confirmation": False,
        "reasons": [],
        "work_profile": {
            "required": write_requested,
            "profile": "light" if write_requested else None,
            "minimum": "light" if write_requested else None,
            "card_required": False,
            "reasons": [],
        },
    }


def shell_command_text(tool_name, tool_input):
    """从不同 Agent 的 shell 工具输入中提取可检查的命令文本。"""

    if not tool_is_shell(tool_name):
        return ""

    if isinstance(tool_input, str):
        return str(tool_input).replace("\r\n", "\n").strip()

    if isinstance(tool_input, (list, tuple)):
        return format_command(tool_input)

    if not isinstance(tool_input, dict):
        return ""

    for key in (
        "command",
        "cmd",
        "script",
        "shell_command",
        "shellCommand",
    ):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.replace("\r\n", "\n").strip()

    argv = tool_input.get("argv")
    if isinstance(argv, (list, tuple)):
        return format_command(argv)

    return ""


def tool_is_shell(tool_name):
    normalized = re.sub(
        r"[^a-z0-9_.-]+",
        "_",
        normalize_text(tool_name).lower(),
    )
    if normalized in SHELL_TOOL_NAMES:
        return True
    tail = normalized.rsplit(".", 1)[-1]
    return tail in SHELL_TOOL_NAMES


def _shell_tokens(command):
    """保留引号语义，区分真实 shell 操作符和搜索文本。"""

    text = str(command or "").replace("\r\n", "\n").replace("\r", "\n")
    tokens = []
    pending_heredocs = []
    heredoc_operator = None
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if char.isspace():
            if char == "\n":
                if pending_heredocs and heredoc_operator is None:
                    index += 1
                    for delimiter, strip_tabs in pending_heredocs:
                        while index < length:
                            line_end = text.find("\n", index)
                            if line_end < 0:
                                line_end = length
                            line = text[index:line_end]
                            index = min(line_end + 1, length)
                            if strip_tabs:
                                line = line.lstrip("\t")
                            if line == delimiter:
                                break
                    pending_heredocs = []
                tokens.append(_ShellToken(";", "operator", False))
            index += 1
            continue

        if char == "#":
            while index < length and text[index] != "\n":
                index += 1
            continue

        operator = next(
            (
                candidate for candidate in SHELL_TOKEN_OPERATORS
                if text.startswith(candidate, index)
            ),
            None,
        )
        if operator:
            tokens.append(_ShellToken(operator, "operator", False))
            if operator in {"<<", "<<-"}:
                heredoc_operator = operator
            index += len(operator)
            continue

        value = []
        quoted = False
        word_started = False
        while index < length:
            char = text[index]
            if char.isspace() or char in ";&|<>()":
                break

            if char == "\\" and os.name != "nt":
                word_started = True
                quoted = True
                index += 1
                if index < length:
                    if text[index] != "\n":
                        value.append(text[index])
                    index += 1
                continue

            if char in {"'", '"'}:
                quote = char
                word_started = True
                quoted = True
                index += 1
                while index < length and text[index] != quote:
                    if quote == '"' and text[index] == "\\" and os.name != "nt":
                        index += 1
                        if index < length:
                            value.append(text[index])
                            index += 1
                        continue
                    value.append(text[index])
                    index += 1
                if index < length:
                    index += 1
                continue

            word_started = True
            value.append(char)
            index += 1

        if word_started:
            token = _ShellToken("".join(value), "word", quoted)
            tokens.append(token)
            if heredoc_operator is not None:
                pending_heredocs.append((
                    token.value,
                    heredoc_operator == "<<-",
                ))
                heredoc_operator = None
        elif index < length:
            # Unknown punctuation still belongs to a word and must not trap
            # the lexer in an infinite loop.
            tokens.append(_ShellToken(text[index], "word", False))
            index += 1

    return tokens


def _shell_command_segments(command):
    """按未加引号的控制操作符切分命令，并保留每一段的连接方式。"""

    segments = []
    current = []
    current_joiner = None
    pending_joiner = None

    for token in _shell_tokens(command):
        if token.kind == "operator" and token.value in SHELL_CONTROL_OPERATORS:
            if current:
                segments.append(_ShellSegment(
                    tuple(current),
                    current_joiner,
                ))
                current = []
            pending_joiner = token.value
            current_joiner = pending_joiner
            continue
        current.append(token)

    if current:
        segments.append(_ShellSegment(
            tuple(current),
            current_joiner,
        ))
    return segments


def _segment_words(segment):
    return [
        token.value
        for token in segment.tokens
        if token.kind == "word"
    ]


def _command_name(value):
    text = str(value or "")
    return (
        ntpath.basename(text)
        or os.path.basename(text)
        or text
    ).lower()


def _skip_options(words, options_with_values=()):
    index = 0
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        index += 1
        if option in options_with_values and index < len(words):
            index += 1
    return words[index:]


def _unwrap_shell_words(words, strip_sudo=True):
    """去掉环境变量、env/command 等执行包装，返回真正命令。"""

    value = list(words)
    wrappers = {
        "builtin",
        "command",
        "env",
        "exec",
        "nice",
        "nohup",
        "time",
    }
    if strip_sudo:
        wrappers.add("sudo")

    while value:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value[0]):
            value.pop(0)
            continue

        command = _command_name(value[0])
        if command not in wrappers:
            break

        value = value[1:]
        if command == "env":
            while value:
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value[0]):
                    value.pop(0)
                    continue
                if value[0].startswith("-"):
                    option = value.pop(0)
                    if option in {"-u", "--unset", "-C", "--chdir"} and value:
                        value.pop(0)
                    continue
                break
            continue

        if command in {"command", "builtin"} and "-v" in value[:1]:
            return []

        if command == "nice":
            value = _skip_options(value, {"-n"})
        elif command == "sudo":
            value = _skip_options(
                value,
                {
                    "-C",
                    "-D",
                    "-g",
                    "-h",
                    "-p",
                    "-r",
                    "-t",
                    "-u",
                },
            )
        else:
            value = _skip_options(value)

    return value


def _shell_wrapper_script(words):
    """返回 shell -c 后面的脚本；没有 -c 时返回 None。"""

    for index, option in enumerate(words[1:], start=1):
        normalized = option.lower()
        if normalized in {"--command", "-command", "/command"}:
            return words[index + 1] if index + 1 < len(words) else None
        if normalized.startswith(("--command=", "-command=")):
            return option.split("=", 1)[1]
        if normalized in {"/c", "/k"}:
            return words[index + 1] if index + 1 < len(words) else None
        if normalized.startswith("-") and not normalized.startswith("--") and "c" in normalized[1:]:
            return words[index + 1] if index + 1 < len(words) else None
    return None


def _redirects_to_file(segment):
    tokens = segment.tokens
    for index, token in enumerate(tokens):
        if (
            token.kind != "operator"
            or token.value not in SHELL_OUTPUT_REDIRECT_OPERATORS
        ):
            continue

        target = next(
            (
                candidate.value
                for candidate in tokens[index + 1:]
                if candidate.kind == "word"
            ),
            "",
        )
        if token.value == ">&" and (
            target in {"-", "&"}
            or target.startswith("&")
            or target.isdigit()
        ):
            continue
        if target in {"/dev/null", "/dev/fd/2", "/dev/stderr"}:
            continue
        return True
    return False


def _segment_write_command(words):
    words = _unwrap_shell_words(words)
    if not words:
        return False

    command = _command_name(words[0])
    arguments = words[1:]

    if command in SHELL_WRITE_COMMANDS:
        return True
    if command == "sed":
        return any(
            argument == "--in-place"
            or argument.startswith("--in-place=")
            or (
                argument.startswith("-")
                and not argument.startswith("--")
                and "i" in argument[1:]
            )
            for argument in arguments
        )
    if command == "perl":
        return any(
            argument.startswith("-")
            and not argument.startswith("--")
            and "i" in argument[1:]
            for argument in arguments
        )
    if command == "dd":
        return any(argument.startswith("of=") for argument in arguments)
    if command == "git":
        return bool(arguments) and arguments[0] in {"am", "apply", "checkout", "restore"}
    return False


def _shell_text_writes_files(command, depth=0):
    if depth > 6:
        return False

    for segment in _shell_command_segments(command):
        if _redirects_to_file(segment):
            return True

        words = _segment_words(segment)
        if _segment_write_command(words):
            return True

        unwrapped = _unwrap_shell_words(words, strip_sudo=False)
        if not unwrapped:
            continue

        command_name = _command_name(unwrapped[0])
        if command_name in SHELL_INTERPRETERS:
            script = _shell_wrapper_script(unwrapped)
            if script and _shell_text_writes_files(script, depth + 1):
                return True
        elif command_name == "eval":
            script = " ".join(unwrapped[1:])
            if script and _shell_text_writes_files(script, depth + 1):
                return True

    return False


def _git_dangerous(arguments):
    if not arguments:
        return False
    command = arguments[0]
    tail = arguments[1:]
    if command == "reset" and "--hard" in tail:
        return True
    if command == "clean" and any(
        option.startswith("-") and "f" in option
        for option in tail
    ):
        return True
    if command == "checkout" and "--" in tail:
        return True
    if command == "restore" and any(
        option == "--source" or option.startswith("--source=")
        for option in tail
    ):
        return True
    if command == "branch" and "-D" in tail:
        return True
    if command == "stash" and tail and tail[0] in {"drop", "clear"}:
        return True
    if command == "push" and any(
        option in {"--force", "-f"} or option.startswith("--force=")
        for option in tail
    ):
        return True
    return False


SAFE_TEMP_SCREENSHOT_NAME = re.compile(
    r"(?:agent[-_]?relay|screen[-_]?shot|capture|snapshot|review|"
    r"verif(?:y|ication)|evidence|check)",
    re.IGNORECASE,
)
SAFE_TEMP_SCREENSHOT_TARGET = re.compile(
    r"(?:^|[-_])(?:dcm|dc-query)(?:[-_]|$)",
    re.IGNORECASE,
)
SAFE_SCREENSHOT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_screenshot_cleanup(arguments):
    """Allow only explicit, non-recursive cleanup of disposable image evidence."""

    targets = []
    options_done = False
    for argument in arguments:
        if not options_done and argument == "--":
            options_done = True
            continue
        if not options_done and argument.startswith("-"):
            if argument in {"-f", "--force", "-v", "--verbose"}:
                continue
            return False
        targets.append(argument)
    if not targets:
        return False

    temp_roots = []
    for value in (tempfile.gettempdir(), os.environ.get("TMPDIR"), "/tmp", "/private/tmp"):
        if not value:
            continue
        try:
            root = Path(value).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if root not in temp_roots:
            temp_roots.append(root)
    evidence_root = (TRACKER_DIR / "page-verification").resolve()

    for argument in targets:
        if any(char in argument for char in "*?[]{}"):
            return False
        raw = Path(argument).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            return False
        try:
            path = raw.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        if path.suffix.lower() not in SAFE_SCREENSHOT_SUFFIXES:
            return False
        in_evidence = _is_within(path, evidence_root)
        in_temp = any(_is_within(path, root) for root in temp_roots)
        if not in_evidence and not in_temp:
            return False
        if (
            not in_evidence
            and not SAFE_TEMP_SCREENSHOT_NAME.search(path.name)
            and not SAFE_TEMP_SCREENSHOT_TARGET.search(path.name)
            and not SAFE_TEMP_SCREENSHOT_TARGET.search(path.parent.name)
        ):
            return False
        try:
            if path.exists() and not path.is_file():
                return False
        except OSError:
            return False
    return True


def _segment_dangerous_reason(words, depth=0):
    if depth > 6 or not words:
        return None

    command = _command_name(words[0])
    if command == "sudo":
        return "权限或系统级操作"

    words = _unwrap_shell_words(words)
    if not words:
        return None
    command = _command_name(words[0])
    arguments = words[1:]

    if command in SHELL_INTERPRETERS:
        script = _shell_wrapper_script(words)
        if script:
            return _dangerous_shell_text(script, depth + 1)
        return None
    if command == "eval":
        return _dangerous_shell_text(" ".join(arguments), depth + 1)

    if command in {"del", "erase", "rd", "remove-item", "rm", "rmdir", "unlink"}:
        return "删除文件或目录"
    if command == "find" and any(
        argument == "-delete"
        or (
            argument == "-exec"
            and index + 1 < len(arguments)
            and _command_name(arguments[index + 1]) == "rm"
        )
        for index, argument in enumerate(arguments)
    ):
        return "删除文件或目录"

    if command == "git" and _git_dangerous(arguments):
        return "Git 破坏性操作"

    if command in {"chmod", "chown"} and "-R" in arguments:
        return "权限或系统级操作"
    if command in {
        "restart-computer",
        "shutdown",
        "stop-computer",
        "stop-process",
        "stop-service",
        "taskkill",
    }:
        return "权限或系统级操作"
    if command in {"launchctl", "sc", "schtasks", "systemctl"} and arguments:
        if arguments[0].lower() in {
            "/delete",
            "/end",
            "delete",
            "restart",
            "stop",
            "unload",
            "bootout",
        }:
            return "权限或系统级操作"
    if command == "kill" and "-9" in arguments or command == "pkill":
        return "权限或系统级操作"

    if (
        command in {"pip", "pip3"}
        and arguments[:1] == ["install"]
        or command in {"py", "python", "python.exe", "python3", "python3.exe"}
        and arguments[:3] == ["-m", "pip", "install"]
        or command in {"uv"}
        and arguments[:2] == ["pip", "install"]
        or command == "conda" and arguments[:1] == ["install"]
        or command in {"choco", "scoop", "winget"} and arguments[:1] == ["install"]
        or command in {"npm", "pnpm", "yarn"}
        and arguments[:1] in (["install"], ["add"])
        or command in {"brew", "apt", "apt-get", "dnf", "yum"}
        and arguments[:1] == ["install"]
    ):
        return "安装或升级依赖"

    if (
        command in {"drop", "truncate"}
        and arguments[:1] in (["database"], ["table"])
        or command == "delete" and arguments[:1] == ["from"]
        or command == "alembic" and arguments[:1] == ["upgrade"]
        or command == "prisma" and arguments[:1] == ["migrate"]
        or command in {"django-admin", "rails"}
        and arguments[:2] in (["migrate"], ["db:migrate"])
    ):
        return "数据库或迁移操作"

    if (
        command == "docker"
        and arguments
        and arguments[0] in {"rm", "rmi", "prune", "push"}
        or command == "kubectl" and arguments[:1] == ["delete"]
        or command == "terraform"
        and arguments[:1] in (["apply"], ["destroy"])
        or command in {"npm", "cargo", "docker"}
        and arguments[:1] == ["publish"]
    ):
        return "部署、重启或容器破坏性操作"

    return None


def _remote_pipe_reason(segments):
    previous = None
    for segment in segments:
        if segment.joiner in {"|", "|&"} and previous is not None:
            previous_words = _unwrap_shell_words(
                _segment_words(previous),
                strip_sudo=True,
            )
            current_words = _unwrap_shell_words(
                _segment_words(segment),
                strip_sudo=True,
            )
            if previous_words and current_words:
                previous_command = _command_name(previous_words[0])
                current_command = _command_name(current_words[0])
                if (
                    previous_command in {"curl", "wget"}
                    and current_command in SHELL_INTERPRETERS
                ):
                    return "远程脚本直接执行"
        previous = segment
    return None


def _dangerous_shell_text(command, depth=0):
    if depth > 6:
        return None
    segments = _shell_command_segments(command)
    remote_reason = _remote_pipe_reason(segments)
    if remote_reason:
        return remote_reason
    for segment in segments:
        reason = _segment_dangerous_reason(
            _segment_words(segment),
            depth,
        )
        if reason:
            return reason
    return None


def dangerous_command_reason(tool_name, tool_input):
    """返回危险命令类别；只保存调用指纹，不把原命令写入审批状态。"""

    command = shell_command_text(tool_name, tool_input)
    if not command:
        return None
    return _dangerous_shell_text(command)


def tool_writes_files(tool_name, tool_input):
    """判断工具是否会修改工作区文件；只读搜索和测试命令不算写入。"""

    normalized = re.sub(
        r"[^a-z0-9_]+",
        "_",
        normalize_text(tool_name).lower(),
    )
    if (
        normalized in WRITE_TOOL_NAMES
        or normalized.endswith("_apply_patch")
    ):
        return True

    command = shell_command_text(tool_name, tool_input)
    if not command:
        return False
    return _shell_text_writes_files(command)


def _clip_card_text(value, limit=600):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}…"


def _card_values(value):
    if isinstance(value, (list, tuple, set)):
        values = [
            _clip_card_text(item)
            for item in value
            if str(item or "").strip()
        ]
        return values or ["无"]
    text = _clip_card_text(value)
    return [text] if text else ["无"]


def _card_label_width(label):
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in str(label))


def _card_field(label, value, width=None):
    values = _card_values(value)
    label_width = width if width is not None else _card_label_width(label)
    padding = " " * max(0, label_width - _card_label_width(label))
    prefix = f"- {label}：{padding} "
    lines = [prefix + values[0]]
    continuation = " " * (label_width + 5)
    lines.extend(continuation + item for item in values[1:])
    return lines


def _card_section(title):
    return ["", title]


def _hook_card_choices():
    """Hook systemMessage is informational; the host has no clickable choices."""
    return [HOOK_CARD_DIVIDER]


def _task_card_summary(prompt):
    return _clip_card_text(prompt, 160) or "未提供具体任务"


def _task_card_id(seed):
    digest = hashlib.sha256(str(seed or "").encode("utf-8")).hexdigest()
    return f"task-{digest[:10]}"


def _request_files(prompt):
    return sorted({
        str(value).strip()
        for value in TASK_FILE_REFERENCE_PATTERN.findall(str(prompt or ""))
        if str(value).strip()
    })[:12]


def _request_modules(prompt):
    modules = []
    text = str(prompt or "")
    for pattern in TASK_DOMAIN_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            modules.append(match.group(0))
    return list(dict.fromkeys(modules))[:8]


def _task_gate_risks(prompt):
    risks = [
        "将修改工作区文件，方案或修改范围变化时需要重新确认。",
    ]
    if matches_any_pattern(prompt, TASK_DANGEROUS_REQUEST_PATTERNS):
        risks.append("需求中包含破坏性操作描述，执行前必须再次核对具体命令和影响范围。")
    return risks


def render_task_gate_card(prompt, gate):
    """生成 Codex systemMessage 可稳定显示的纯文本需求确认卡。"""

    gate = gate if isinstance(gate, dict) else {}
    request_hash = (
        gate.get("request_sha256")
        or hashlib.sha256(normalize_text(prompt).encode("utf-8")).hexdigest()
    )
    reasons = gate.get("reasons") or ["本地规则判断本次任务需要先确认"]
    files = _request_files(prompt)
    modules = _request_modules(prompt)
    scope = []
    if modules:
        scope.append(f"模块：{'、'.join(modules)}")
    else:
        scope.append("模块：以当前需求涉及的相关代码为准")
    if files:
        scope.append(f"文件：{'、'.join(files)}")
    else:
        scope.append("文件：待读取相关代码后确认；未明确前不得写入")

    lines = [
        "「需求卡」",
        HOOK_CARD_DIVIDER,
    ]
    contract = gate.get("change_contract")
    has_contract = isinstance(contract, dict)
    base_fields = [("任务", _task_card_summary(prompt)),
                   ("任务ID", _task_card_id(request_hash)),
                   ("触发原因", reasons)]
    work_profile = gate.get("work_profile")
    if isinstance(work_profile, dict) and work_profile.get("required"):
        base_fields.insert(2, (
            "工作档",
            f"{work_profile.get('profile') or '待选择'}"
            f"（最低 {work_profile.get('minimum') or work_profile.get('profile') or 'light'}；"
            f"确认卡 {'是' if work_profile.get('card_required') else '否'}）",
        ))
        if work_profile.get("card_required"):
            try:
                confirmation_module = _shared_script_module("change_contract")
                draft = confirmation_module.draft_confirmation_card(
                    prompt,
                    allowed_files=(contract or {}).get("allowed_files") or files,
                    modules=modules,
                    profile=str(work_profile.get("profile") or "strict"),
                )
                base_fields.extend(confirmation_module.confirmation_card_fields(draft))
                gate["confirmation_card"] = draft
            except (ImportError, ValueError, TypeError):
                pass
    if not has_contract:
        base_fields.extend([
            ("影响范围", scope),
            ("目标外边界", "公共组件、兄弟元素和其他模块保持不变；连带影响需重新确认"),
            ("复用优先", "先检索并复用项目已有组件/样式；确需新组件必须说明缺口并获明确授权"),
        ])
    if has_contract:
        try:
            base_fields.extend(_shared_script_module("change_contract").card_fields(contract))
        except (ImportError, ValueError, TypeError):
            pass
    base_fields.extend([
        ("当前状态", "等待批准 · 尚未修改文件 · waiting_for_approval"),
        ("批准后", "仅执行本卡明确的目标与边界；扩大范围、修改公共组件或产生连带影响必须重新确认。"
         if has_contract else "仅执行当前展示的修改方案；新增范围必须重新确认。"),
        ("主要风险", _task_gate_risks(prompt)),
        ("验证", ["先运行与改动直接相关的测试或构建命令。", "确认通过后，再运行项目已有的完整测试。"]),
        ("回滚", "修改前保留原文件内容；验证失败时按文件恢复，并记录失败原因。"),
    ])
    width = max(_card_label_width(label) for label, _ in base_fields)
    for label, value in base_fields:
        lines.extend(_card_field(label, value, width))
    lines.extend(_hook_card_choices())
    return "\n".join(lines)


def _patch_text(tool_name, tool_input):
    """Read patch text from the field shapes used by supported hosts."""

    if not isinstance(tool_input, dict):
        return ""

    patch = tool_input.get("input") or tool_input.get("patch")
    if isinstance(patch, str) and patch.strip():
        return patch

    command = tool_input.get("command")
    normalized = re.sub(
        r"[^a-z0-9_]+",
        "_",
        normalize_text(tool_name).lower(),
    )
    is_patch_tool = (
        normalized in {"apply_patch", "applypatch"}
        or normalized.endswith("_apply_patch")
    )
    if (
        is_patch_tool
        and isinstance(command, str)
        and re.search(
            r"^\*\*\* (?:Begin Patch|Add File:|Update File:|Delete File:)",
            command,
            flags=re.MULTILINE,
        )
    ):
        return command
    return ""


def _tool_targets(tool_name, tool_input):
    if isinstance(tool_input, dict):
        targets = []
        for key in (
            "file_path",
            "path",
            "file",
            "target",
            "target_file",
        ):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                targets.append(value.strip())

        patch = _patch_text(tool_name, tool_input)
        if isinstance(patch, str):
            targets.extend(re.findall(
                r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+)$",
                patch,
                flags=re.MULTILINE,
            ))
            targets.extend(re.findall(
                r"^\*\*\* Move to:\s*(.+)$",
                patch,
                flags=re.MULTILINE,
            ))
        if targets:
            return list(dict.fromkeys(targets))[:12]

    command = shell_command_text(tool_name, tool_input)
    if command:
        return [_clip_card_text(command, 500)]
    return []


def _change_workspace(workspace=None):
    """Return one stable workspace identity for history isolation."""

    value = str(workspace or os.getcwd()).strip()
    try:
        return str(Path(value).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return value


def new_change_tracking(prompt=None, session_id=None, workspace=None):
    """Create a blank record for one user-visible write turn."""

    request = normalize_text(prompt)
    turn_seed = "|".join([
        str(session_id or ""),
        _change_workspace(workspace),
        str(time.time_ns()),
    ])
    turn_id = hashlib.sha256(turn_seed.encode("utf-8")).hexdigest()[:20]
    return {
        "turn_id": turn_id,
        "started_at": iso_now(),
        "request": request or None,
        "workspace": _change_workspace(workspace),
        "files": [],
        "file_groups": [],
        "summary": None,
        "tools": [],
        "had_write": False,
        "recorded": False,
        "recorded_at": None,
    }


def _change_request_for_prompt(state, prompt):
    """Keep the original task when the latest message only says continue."""

    normalized = normalize_text(prompt)
    tracking = state.get("change_tracking")
    previous = (
        normalize_text(tracking.get("request"))
        if isinstance(tracking, dict)
        else ""
    )
    fallback = (
        previous
        or normalize_text(state.get("initial_prompt"))
        or normalize_text(state.get("last_prompt"))
        or normalized
    )
    if not normalized:
        return fallback
    generic_continuation = (
        is_explicit_task_approval(normalized)
        or (
            matches_any_pattern(normalized, CONTINUATION_PATTERNS)
            and not matches_any_pattern(normalized, TASK_WRITE_ACTION_PATTERNS)
        )
    )
    return fallback if generic_continuation and fallback else normalized


def change_tracking(state):
    """Read and repair the per-session change tracker without losing data."""

    value = state.get("change_tracking")
    if not isinstance(value, dict):
        value = {}
    tracking = {
        "turn_id": value.get("turn_id"),
        "started_at": value.get("started_at"),
        "request": normalize_text(value.get("request")) or None,
        "workspace": _change_workspace(value.get("workspace") or os.getcwd()),
        "files": [
            str(item) for item in (value.get("files") or [])
            if str(item or "").strip()
        ],
        "file_groups": [
            item for item in (value.get("file_groups") or [])
            if isinstance(item, dict)
        ],
        "summary": normalize_text(value.get("summary")) or None,
        "tools": [
            str(item) for item in (value.get("tools") or [])
            if str(item or "").strip()
        ],
        "had_write": bool(value.get("had_write", False)),
        "recorded": bool(value.get("recorded", False)),
        "recorded_at": value.get("recorded_at"),
    }
    state["change_tracking"] = tracking
    return tracking


def _append_unique_strings(values, additions, limit=None):
    result = list(values or [])
    seen = {str(item) for item in result}
    for item in additions or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    if limit is not None:
        result = result[-limit:]
    return result


def _normalized_changed_path(value, workspace):
    """Prefer a workspace-relative path; retain external paths as absolute."""

    text = str(value or "").strip().strip("\"'")
    if not text:
        return ""
    try:
        root = Path(workspace).expanduser().resolve()
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        try:
            return candidate.relative_to(root).as_posix()
        except ValueError:
            return str(candidate)
    except (OSError, RuntimeError, ValueError):
        return text


def written_file_targets(tool_name, tool_input, workspace=None):
    """Extract changed paths without confusing shell command text for a file."""

    if not tool_writes_files(tool_name, tool_input):
        return []
    workspace = _change_workspace(workspace)
    raw_targets = []
    if isinstance(tool_input, dict):
        for key in (
            "file_path",
            "path",
            "file",
            "target",
            "target_file",
        ):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                raw_targets.append(value)
        patch = _patch_text(tool_name, tool_input)
        if isinstance(patch, str):
            raw_targets.extend(re.findall(
                r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+)$",
                patch,
                flags=re.MULTILINE,
            ))
            raw_targets.extend(re.findall(
                r"^\*\*\* Move to:\s*(.+)$",
                patch,
                flags=re.MULTILINE,
            ))
    paths = [
        _normalized_changed_path(item, workspace)
        for item in raw_targets
    ]
    paths = [item for item in paths if item]
    return list(dict.fromkeys(paths))[:50]


def change_history_file(workspace=None, agent_id=None, session_id=None):
    """Return the private history file for one workspace, Agent, and session."""

    workspace_key = os.path.normcase(_change_workspace(workspace))
    agent_key = normalize_agent_id(agent_id) or "default"
    session_key = normalize_session_id(session_id)
    identity = f"{workspace_key}\0{agent_key}"
    if session_key:
        identity = f"{identity}\0{session_key}"
    digest = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]
    return HISTORY_DIR / "change-summaries" / f"{digest}.json"


def read_change_history(workspace=None, agent_id=None, session_id=None):
    """Read recent summaries; a malformed file is treated as empty history."""

    workspace = _change_workspace(workspace)
    agent_id = normalize_agent_id(agent_id)
    session_id = normalize_session_id(session_id)
    path = change_history_file(workspace, agent_id, session_id)
    empty = {
        "schema_version": 1,
        "workspace": workspace,
        "agent_id": agent_id or "default",
        "session_id": session_id or "default",
        "entries": [],
    }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return empty
    if not isinstance(value, dict):
        return empty
    entries = [
        item for item in (value.get("entries") or [])
        if isinstance(item, dict) and str(item.get("summary") or "").strip()
    ]
    return {
        "schema_version": 1,
        "workspace": workspace,
        "agent_id": agent_id or "default",
        "session_id": session_id or "default",
        "entries": entries[:CHANGE_HISTORY_LIMIT],
    }


def _assistant_change_hint(message):
    """Extract one completed-change sentence from the Agent's final reply."""

    text = normalize_text(message)
    text = re.sub(
        r"^(?:#{1,6}\s*|[-*+]\s*|\d+[.)、]\s*)+",
        "",
        text,
    )
    text = re.sub(
        r"^(?:是的|对的|好的|可以)(?:[，,:：]\s*|\s+)|^已完成[，,:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^本次(?:修改|变更|改动)[:：]\s*", "", text)
    text = re.sub(
        r"^(?:按用户需求|按需求)[“\"].*?[”\"]\s*[:：]?\s*",
        "",
        text,
    )
    for sentence in re.split(
        r"(?<=[。！？])\s*|(?<=[.!?])\s+",
        text,
    ):
        sentence = sentence.strip()
        if len(sentence) < 6:
            continue
        if not re.search(
            r"(?:完成|修复|修改|更新|实现|优化|新增|删除|调整|接入|清理|"
            r"fixed|updated|implemented|added|removed|changed)",
            sentence,
            flags=re.IGNORECASE,
        ):
            continue
        return _clip_card_text(sentence, 100).rstrip("。！？.!?")
    return ""


def _fallback_change_file_group(path):
    """Return a stable, truthful grouping when the Agent gave no labels."""

    normalized = str(path or "").replace("\\", "/").lstrip("./")
    lower = normalized.lower()
    if lower.startswith("tests/") or "/__tests__/" in lower:
        return "自动测试"
    if lower.startswith("hooks/"):
        return "写入逻辑检查"
    if lower.startswith("scripts/"):
        return "运行脚本"
    if lower.startswith("skills/"):
        return "Agent 使用说明"
    if lower == "readme.md" or lower.startswith("docs/") or lower.endswith(".md"):
        return "使用说明"
    if lower.startswith("src/") or lower.startswith("app/"):
        return "页面代码"
    return "其他改动"


CHANGE_FILE_GROUP_LABELS = {
    "测试": "自动测试",
    "Hook": "写入逻辑检查",
    "脚本": "运行脚本",
    "Skill 文档": "Agent 使用说明",
    "文档": "使用说明",
    "源代码": "页面代码",
    "其他文件": "其他改动",
}


def _friendly_change_file_group(label):
    """Translate labels saved by older releases into plain-language names."""

    text = _clip_card_text(label, 80)
    return CHANGE_FILE_GROUP_LABELS.get(text, text)


def default_change_file_groups(files):
    """Group changed files by broad repository role without inventing intent."""

    groups = {}
    for value in files or []:
        path = str(value or "").strip()
        if not path:
            continue
        label = _fallback_change_file_group(path)
        groups.setdefault(label, [])
        if path not in groups[label]:
            groups[label].append(path)
    return [
        {"label": label, "files": grouped_files}
        for label, grouped_files in groups.items()
    ]


def normalize_change_file_groups(file_groups, files, workspace=None, strict=False):
    """Normalize Agent-provided groups and append every unmentioned file once."""

    workspace = workspace or os.getcwd()
    allowed_files = []
    for value in files or []:
        normalized = _normalized_changed_path(value, workspace)
        if normalized and normalized not in allowed_files:
            allowed_files.append(normalized)
    allowed = set(allowed_files)
    grouped_files = set()
    result = []
    for raw_group in file_groups or []:
        label = ""
        raw_files = []
        if isinstance(raw_group, dict):
            label = normalize_text(raw_group.get("label"))
            raw_files = raw_group.get("files") or []
            if isinstance(raw_files, str):
                raw_files = re.split(r"[,;]", raw_files)
        elif isinstance(raw_group, str):
            group_text = raw_group.strip()
            if "=" in group_text:
                label, paths = group_text.split("=", 1)
                raw_files = re.split(r"[,;]", paths)
            else:
                label, raw_files = "", [group_text]
        label = _clip_card_text(label, 80)
        if not label:
            if strict:
                raise ValueError("每个文件分组都必须提供名称")
            continue
        normalized_files = []
        for value in raw_files:
            path = _normalized_changed_path(value, workspace)
            if not path:
                continue
            if path not in allowed:
                if strict:
                    raise ValueError(f"文件分组包含本轮未修改的文件：{path}")
                continue
            if path in grouped_files or path in normalized_files:
                continue
            normalized_files.append(path)
        if not normalized_files:
            if strict:
                raise ValueError(f"文件分组“{label}”没有包含本轮修改的文件")
            continue
        existing = next(
            (item for item in result if item.get("label") == label),
            None,
        )
        if existing is None:
            existing = {"label": label, "files": []}
            result.append(existing)
        existing["files"].extend(normalized_files)
        grouped_files.update(normalized_files)

    remaining = [path for path in allowed_files if path not in grouped_files]
    if remaining and (result or strict):
        existing = next(
            (item for item in result if item.get("label") == "其他改动"),
            None,
        )
        if existing is None:
            result.append({"label": "其他改动", "files": remaining})
        else:
            existing["files"].extend(remaining)
    return result


def build_change_entry(tracking, assistant_message=None):
    """Build the persisted semantic summary and grouped file manifest."""

    files = list(tracking.get("files") or [])
    explicit_summary = normalize_text(tracking.get("summary"))
    hint = _assistant_change_hint(assistant_message)
    summary = explicit_summary or hint
    if summary:
        summary = re.sub(
            r"^(?:按用户需求|按需求)[“\"].*?[”\"]\s*[:：]?\s*",
            "",
            summary,
        ).strip()
    if not summary:
        summary = "已完成文件写入，但未记录具体修改说明。"

    workspace = tracking.get("workspace")
    explicit_groups = tracking.get("file_groups") or []
    file_groups = normalize_change_file_groups(
        explicit_groups,
        files,
        workspace=workspace,
    )
    if not file_groups:
        file_groups = default_change_file_groups(files)
    return {
        "summary": _clip_card_text(summary, 220),
        "file_groups": file_groups,
        "changed_files": files,
    }


def build_change_summary(tracking, assistant_message=None):
    """Return the truthful completion text stored in change history."""

    return build_change_entry(tracking, assistant_message)["summary"]


def _clean_change_history_summary(value):
    """Remove formatting debris left by earlier summary implementations."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    text = re.sub(r"```[A-Za-z0-9_+.-]*", "\n", text)
    text = re.sub(r"`{3,}", "\n", text)
    text = re.sub(
        r"(?:按用户需求|按需求)[“\"].*?[”\"][ \t]*[:：]?[ \t]*",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?:[📝🕘][ \t]*)?(?:本次|上次|上上次)修改[ \t]*[:：][ \t]*",
        "\n",
        text,
    )
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"([，。！？；：、])[ \t]+", r"\1", text)
    return text.strip(" \n:：")


def _change_history_summary_segments(value):
    """Split legacy summaries at natural sentence and line boundaries."""

    cleaned = _clip_card_text(
        _clean_change_history_summary(value),
        limit=220,
    )
    if not cleaned:
        return []
    segments = []
    for line in cleaned.splitlines():
        line = re.sub(r"^(?:[-*+•]\s+|\d+[.)、]\s*)", "", line.strip())
        if not line:
            continue
        for segment in re.split(r"(?<=[。！？!?；;])\s*", line):
            segment = segment.strip().rstrip("；;").strip()
            if segment:
                segments.append(segment)
    return segments


def _change_history_summary_lines(value):
    """Render one plain-language paragraph instead of a technical change log."""

    segments = _change_history_summary_segments(value)
    if not segments:
        return ["    未记录具体修改说明"]

    paragraph = ""
    for segment in segments:
        segment = segment.rstrip("；;")
        if paragraph and not re.search(r"[。！？!?]$", paragraph):
            paragraph += "；"
        paragraph += segment
    if not re.search(r"[。！？!?]$", paragraph):
        paragraph += "。"
    body_lines = _wrap_change_history_text(
        paragraph,
        width=max(36, CHANGE_HISTORY_TEXT_WIDTH - 4),
        limit=220,
    )
    return [f"    {body_lines[0]}"] + [
        f"    {line}" for line in body_lines[1:]
    ]


def _wrap_change_history_text(value, width=None, limit=220):
    """Wrap summary text by terminal display width, including CJK characters."""

    remaining = _clip_card_text(value, limit)
    if not remaining:
        return ["暂无记录"]
    width = width or CHANGE_HISTORY_TEXT_WIDTH

    lines = []
    while remaining:
        used_width = 0
        cut_index = 0
        preferred_cut = 0
        preferred_breaks = set(" \t、，。；：,.;:/-")
        for index, char in enumerate(remaining):
            char_width = (
                2
                if unicodedata.east_asian_width(char) in {"W", "F"}
                else 1
            )
            if cut_index and used_width + char_width > width:
                break
            used_width += char_width
            cut_index = index + 1
            if char in preferred_breaks:
                preferred_cut = cut_index

        if cut_index >= len(remaining):
            lines.append(remaining)
            break

        break_index = (
            preferred_cut
            if preferred_cut >= max(1, cut_index // 2)
            else cut_index
        )
        line = remaining[:break_index].rstrip()
        if line:
            lines.append(line)
        remaining = remaining[break_index:].lstrip()
    return lines


def render_change_history(entries):
    """Render the latest two changes as short paragraphs for the user."""

    labels = ("📝 本次修改", "🕘 上次修改")
    blocks = []
    for index, label in enumerate(labels):
        entry = entries[index] if index < len(entries) else None
        lines = [f"  {label}："]
        if not isinstance(entry, dict):
            lines.append("    暂无记录")
            blocks.append("\n".join(lines))
            continue
        lines.extend(_change_history_summary_lines(entry.get("summary")))
        blocks.append("\n".join(lines))
    return "\n" + "\n\n".join(blocks)


def annotate_change_summary(session_id, summary, file_groups):
    """Store the Agent's actual completion description before Stop renders it."""

    clean_summary = _clip_card_text(normalize_text(summary), 220)
    if not clean_summary:
        raise ValueError("--summary 不能为空")
    lock = acquire_lock()
    try:
        state = read_state(session_id=session_id)
        tracking = change_tracking(state)
        if not tracking.get("had_write"):
            raise ValueError("当前任务还没有成功写入文件")
        if tracking.get("recorded"):
            raise ValueError("当前任务的修改摘要已经记录")
        normalized_groups = normalize_change_file_groups(
            file_groups,
            tracking.get("files") or [],
            workspace=tracking.get("workspace"),
            strict=True,
        )
        tracking["summary"] = clean_summary
        tracking["file_groups"] = normalized_groups
        write_state(state, session_id=session_id)
    finally:
        release_lock(lock)
    return {
        "summary": clean_summary,
        "file_groups": normalized_groups,
        "changed_files": list(tracking.get("files") or []),
    }


def record_change_history(state, assistant_message=None):
    """Persist and render the latest two write summaries for this Stop."""

    tracking = change_tracking(state)
    if tracking.get("recorded") or not tracking.get("had_write"):
        return ""

    workspace = _change_workspace(
        tracking.get("workspace") or os.getcwd()
    )
    change_entry = build_change_entry(tracking, assistant_message)
    agent_id = normalize_agent_id(state.get("agent_id"))
    session_id = normalize_session_id(state.get("session_id"))
    entry = {
        "turn_id": tracking.get("turn_id") or hashlib.sha256(
            f"{workspace}|{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:20],
        "session_id": session_id,
        "workspace": workspace,
        "created_at": iso_now(),
        "summary": change_entry["summary"],
        "changed_files": change_entry["changed_files"],
        "file_groups": change_entry["file_groups"],
        "tools": list(tracking.get("tools") or []),
    }
    history = read_change_history(workspace, agent_id, session_id)
    previous = [
        item for item in history.get("entries", [])
        if item.get("turn_id") != entry["turn_id"]
    ]
    output_entries = [entry] + previous
    history["entries"] = output_entries[:CHANGE_HISTORY_LIMIT]
    history["updated_at"] = iso_now()
    try:
        atomic_write_json(
            change_history_file(workspace, agent_id, session_id),
            history,
        )
    except (OSError, ValueError, TypeError) as exc:
        log_event("change_history_write_error", {"error": str(exc)})

    tracking["recorded"] = True
    tracking["recorded_at"] = entry["created_at"]
    return render_change_history(output_entries[:CHANGE_HISTORY_LIMIT])


def _tool_gate_is_danger(reason):
    return "[AgentRelay 安全门禁]" in str(reason or "")


def render_tool_gate_card(tool_name, tool_input, reason):
    """生成 Codex systemMessage 可稳定显示的纯文本工具确认卡。"""

    danger = _tool_gate_is_danger(reason)
    solution_key = build_solution_key(tool_name, tool_input) or tool_name
    targets = _tool_targets(tool_name, tool_input)
    title = "「危险操作确认卡」" if danger else "「确认卡」"

    lines = [
        title,
        HOOK_CARD_DIVIDER,
    ]
    action_fields = [("任务", _task_card_summary(reason)),
                     ("任务ID", _task_card_id(solution_key)),
                     ("工具", normalize_text(tool_name) or "unknown"),
                     ("操作内容", targets or ["未提供具体路径或命令"]),
                     ("当前状态", "已阻断 · 等待批准")]
    action_fields.extend([
        ("主要风险", [_clip_card_text(reason, 800)]),
        ("批准边界", "仅限完全相同的工具输入执行一次；命令、路径、参数或影响范围变化必须重新确认。"),
        ("验证", "检查实际 diff 与目标外副作用，再运行相关测试；目标外变化必须报告。"),
        ("回滚", "修改类操作使用修改前备份或版本控制恢复；删除类操作可能无法恢复。"),
    ])
    width = max(_card_label_width(label) for label, _ in action_fields)
    for label, value in action_fields:
        lines.extend(_card_field(label, value, width))
    lines.extend(_hook_card_choices())
    return "\n".join(lines)


def visible_hook_cards_enabled():
    """Default to the direct Codex Hook; explicit targets retain their adapters."""
    target = normalize_text(os.environ.get("AGENTRELAY_TARGET")).lower()
    return target not in {"kimi", "opencode", "deepseek-cli"}


def codex_hook_cards_enabled():
    """仅在 Codex Hook 环境输出 systemMessage；其他 Agent 保持原协议。"""

    target = normalize_text(os.environ.get("AGENTRELAY_TARGET")).lower()
    if target:
        return target == "codex"
    return bool(
        os.environ.get("CODEX_HOME")
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CODEX_SESSION_ID")
    )


def emit_hook_context(event_name, context, system_message=None):
    """输出模型上下文，并在 Codex 中附带用户可见的 Hook 卡片。"""

    if not context:
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        },
    }
    if system_message and visible_hook_cards_enabled():
        payload["systemMessage"] = system_message
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def emit_stop_summary(message):
    """Print the non-blocking recent-change summary after a final Stop."""

    text = str(message or "").rstrip()
    if not text.strip() or not visible_hook_cards_enabled():
        return False
    print(
        json.dumps({"systemMessage": text}, ensure_ascii=False),
        flush=True,
    )
    return True


def emit_pre_tool_deny(reason, system_message=None):
    """安全降级为 deny；当前 Codex parser 不接受原生 ask。"""

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    if system_message and visible_hook_cards_enabled():
        payload["systemMessage"] = system_message
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def approval_controls_snapshot(state, prompt):
    """保存待审批门禁，避免创建新问题时丢失用户刚提交的批准。"""

    scope = pending_approval_scope(state)
    if not is_gate_response_candidate(state, prompt):
        return None

    gate = state.get("task_gate")
    approvals = state.get("tool_approval")
    pending_keys = (
        approvals.get("pending_keys", [])
        if isinstance(approvals, dict)
        else []
    )
    contract = state.get("change_contract")
    if not has_pending_approval(scope):
        return None

    return {
        "task_gate": dict(gate) if isinstance(gate, dict) else {},
        "change_contract": dict(contract) if isinstance(contract, dict) else None,
        "tool_approval": {
            "pending_keys": [
                item for item in pending_keys
                if isinstance(item, str) and item
            ],
            "approved_keys": [
                item for item in (
                    approvals.get("approved_keys", [])
                    if isinstance(approvals, dict)
                    else []
                )
                if isinstance(item, str) and item
            ],
        },
    }


def restore_approval_controls(state, snapshot):
    """把未完成审批恢复到 handle_user_prompt 可能新建的状态。"""

    if not snapshot:
        return
    state["task_gate"] = dict(snapshot.get("task_gate") or {})
    if isinstance(snapshot.get("change_contract"), dict):
        state["change_contract"] = dict(snapshot["change_contract"])
    approvals = snapshot.get("tool_approval") or {}
    state["tool_approval"] = {
        "pending_keys": list(approvals.get("pending_keys") or []),
        "approved_keys": list(approvals.get("approved_keys") or []),
    }


def _append_unique(values, value, limit):
    result = [
        item for item in (values or [])
        if isinstance(item, str) and item and item != value
    ]
    if value:
        result.append(value)
    return result[-limit:]


def card_delivery_notice(gate, request_hash):
    """One visible card per action; context must not request a second card."""
    try:
        first = _shared_script_module("approval_policy").mark_card_shown(gate, request_hash)
    except (ImportError, ValueError, TypeError):
        first = gate.get("card_shown_sha256") != request_hash
    if first:
        return True, (
            "[AgentRelay 任务门禁 · 严格变更约束 · 确认卡已显示]\n用户已看到唯一一张需求卡/确认卡，不要重复展示或要求二次确认。"
            "等待用户明确批准前只能只读检查，不得写入、调用 Fusion、Commander 或其他协作模型。"
        )
    return False, ""


def _approval_scope_labels(scope):
    labels = []
    if scope.get("task_request_sha256"):
        labels.append("当前任务方案")
    if scope.get("contract_sha256"):
        labels.append("当前 change_contract")
    tool_count = len(scope.get("tool_keys") or [])
    if tool_count:
        labels.append(f"{tool_count} 个已展示的危险命令")
    return labels


def semantic_approval_notice(session_id, state, prompt):
    """Ask the main Agent to resolve this reply using full conversation context."""

    scope = pending_approval_scope(state)
    if not has_pending_approval(scope):
        return ""
    source_hash = prompt_sha256(prompt)
    state["semantic_approval_candidate"] = {
        "source_sha256": source_hash,
        "scope": scope,
        "created_at": iso_now(),
    }
    tracker_path = str(Path(__file__).resolve())
    python_path = active_python_command()
    session_value = str(session_id)
    gate = state.get("task_gate")
    work_profile = gate.get("work_profile") if isinstance(gate, dict) else None
    minimum_profile = (
        str(work_profile.get("minimum") or work_profile.get("profile") or "")
        if isinstance(work_profile, dict) and work_profile.get("required")
        else ""
    )
    if not minimum_profile:
        pending_profile = _pending_work_profile(state)
        if pending_profile:
            minimum_profile = str(
                pending_profile.get("minimum")
                or pending_profile.get("profile")
                or ""
            )
    approve_parts = [
        python_path,
        tracker_path,
        "resolve-intent",
        "--session",
        session_value,
        "--decision",
        "approve",
        "--source-sha256",
        source_hash,
    ]
    if minimum_profile:
        approve_parts.extend(["--work-profile", minimum_profile])
    approve_parts.extend(["--reason", "用户明确批准当前待确认范围"])
    approve_command = format_command(approve_parts)
    reject_command = format_command([
        python_path,
        tracker_path,
        "resolve-intent",
        "--session",
        session_value,
        "--decision",
        "reject",
        "--source-sha256",
        source_hash,
        "--reason",
        "用户明确拒绝当前待确认范围",
    ])
    return (
        "[AgentRelay 语义审批桥]\n"
        "Hook 只识别到这是一条待审批回复，不会根据关键词替用户授权。\n"
        f"用户原话：{_clip_card_text(prompt, 500)}\n"
        f"待审批范围：{'；'.join(_approval_scope_labels(scope))}\n"
        f"消息摘要：{source_hash}\n"
        + (
            f"本次写任务最低工作档为 {minimum_profile}。批准命令中的工作档是"
            " Hook 给出的最低建议；你必须确认它适合当前任务，也可改成更强的 "
            "light/standard/strict/user_required，但不能降档。\n"
            if minimum_profile else ""
        )
        + "请结合确认卡、用户完整消息和当前对话自行判断。只有明确同意当前范围时，"
        f"运行：{approve_command}\n"
        "只有明确拒绝当前范围时，运行："
        f"{reject_command}\n"
        "问句、犹豫、否定、假设、补充条件或提出新范围时不要写回批准；先询问或更新确认卡。"
        "写回只对上述待审批范围生效，不能扩大到其他文件、命令或任务。"
    )


def _pending_work_profile(state, requested=None):
    """Derive the profile for an approval that predates profile selection."""

    contract = state.get("change_contract")
    contract_pending = (
        isinstance(contract, dict)
        and not contract.get("approved")
        and not contract.get("draft")
    )
    gate = state.get("task_gate")
    task_pending = (
        isinstance(gate, dict)
        and gate.get("status") == "awaiting_confirmation"
    )
    if not contract_pending and not task_pending:
        return None

    module = _shared_script_module("approval_policy")
    mode = module.load_policy(CODEX_DIR)["mode"]
    text = str(state.get("last_prompt") or "")
    files = (
        contract.get("allowed_files") or []
        if isinstance(contract, dict)
        else []
    )
    file_count = len({
        str(value).strip().lower()
        for value in files
        if str(value).strip()
    })
    profile = module.select_work_profile(
        text,
        mode,
        requested=normalize_text(requested).lower() or None,
        file_count=file_count,
    )
    if not profile.get("required") and contract_pending:
        target = str(contract.get("target_scope") or "").strip()
        profile = module.select_work_profile(
            "修改 " + " ".join(value for value in (text, target) if value),
            mode,
            requested=normalize_text(requested).lower() or None,
            file_count=file_count,
        )
    if not profile.get("required"):
        selected = normalize_text(requested).lower()
        if not selected:
            return None
        profile = {
            "required": True,
            "profile": selected,
            "minimum": "light",
            "card_required": selected != "light",
            "reasons": ["审批写任务时必须显式记录工作档"],
        }
    return profile


def _apply_work_profile_selection(gate, requested, reason=""):
    """Record the Agent's explicit profile without merging it into user approval."""

    module = _shared_script_module("approval_policy")
    work_profile = gate.get("work_profile")
    if not isinstance(work_profile, dict) or not work_profile.get("required"):
        raise ValueError("当前任务不需要选择工作档")
    requested = normalize_text(requested).lower()
    if requested not in module.WORK_PROFILES:
        raise ValueError(
            "工作档必须是 " + ", ".join(module.WORK_PROFILES)
        )
    minimum = str(
        work_profile.get("minimum") or work_profile.get("profile") or "light"
    )
    if minimum not in module.WORK_PROFILE_STRENGTH:
        raise ValueError(f"任务最低工作档无效：{minimum}")
    if (
        module.WORK_PROFILE_STRENGTH[requested]
        < module.WORK_PROFILE_STRENGTH[minimum]
    ):
        raise ValueError(
            f"工作档不能降档：任务最低要求 {minimum}，当前选择 {requested}"
        )
    selected = dict(work_profile)
    card_required = requested != "light"
    selected.update({
        "profile": requested,
        "card_required": card_required,
        "selection_source": "agent",
        "selected_at": iso_now(),
        "selection_reason": normalize_text(reason)[:500],
    })
    if card_required:
        card_reason = "主 Agent 选择非轻量工作档，需要先确认五栏需求卡"
        selected["reasons"] = list(dict.fromkeys([
            *(selected.get("reasons") or []),
            card_reason,
        ]))
        if gate.get("status") == "clear":
            gate["status"] = "awaiting_confirmation"
            gate["requires_confirmation"] = True
            gate["reasons"] = list(dict.fromkeys([
                *(gate.get("reasons") or []),
                card_reason,
            ]))
            gate["requested_at"] = iso_now()
            gate["notified_sha256"] = gate.get("request_sha256")
            gate["card_shown_sha256"] = None
    gate["work_profile"] = selected
    return selected


def _auto_record_light_profile(gate, session_id):
    """Record the light profile for simple writes without showing an Agent command."""

    work_profile = gate.get("work_profile")
    if not isinstance(work_profile, dict) or not work_profile.get("required"):
        return None
    minimum = normalize_text(
        work_profile.get("minimum") or work_profile.get("profile")
    ).lower()
    if minimum != "light" or work_profile.get("selection_source"):
        return None
    selected = dict(work_profile)
    selected.update({
        "profile": "light",
        "minimum": "light",
        "card_required": False,
        "selection_source": "hook_auto_light",
        "selected_at": iso_now(),
        "selection_reason": "任务最低档为 light，Hook 自动记录轻量工作档",
    })
    gate["work_profile"] = selected
    log_event(
        "work_profile_selected",
        {
            "session_id": normalize_session_id(session_id),
            "minimum": "light",
            "profile": "light",
            "selection_source": "hook_auto_light",
        },
    )
    return selected


def select_task_work_profile(session_id, requested, reason=""):
    """Persist an explicit profile choice for a clear, non-card write task."""

    session_id = normalize_session_id(session_id)
    if not session_id:
        raise ValueError("select-work-profile 需要有效 session_id")
    lock = acquire_lock()
    try:
        state = read_state(session_id=session_id)
        gate = state.get("task_gate")
        if not isinstance(gate, dict):
            raise ValueError("当前没有可选择的写任务")
        selected = _apply_work_profile_selection(gate, requested, reason)
        state["task_gate"] = gate
        card = None
        if (
            selected.get("card_required")
            and gate.get("status") == "awaiting_confirmation"
        ):
            card = render_task_gate_card(
                str(state.get("last_prompt") or ""),
                gate,
            )
        write_state(state, session_id=session_id)
    finally:
        release_lock(lock)
    log_event(
        "work_profile_selected",
        {
            "session_id": session_id,
            "profile": selected.get("profile"),
            "minimum": selected.get("minimum"),
            "selection_source": "agent",
        },
    )
    return {
        "profile": selected.get("profile"),
        "minimum": selected.get("minimum"),
        "card_required": bool(selected.get("card_required")),
        "status": gate.get("status"),
        "selection_source": "agent",
        **({"card": card} if card else {}),
    }


def resolve_pending_intent(
    session_id, decision, source_sha256, reason, work_profile=None,
):
    """Atomically apply the Agent's contextual approve/reject decision."""

    decision = normalize_text(decision).lower()
    source_sha256 = normalize_text(source_sha256).lower()
    reason = normalize_text(reason)
    session_id = normalize_session_id(session_id)
    if decision not in {"approve", "reject"}:
        raise ValueError("decision 必须是 approve 或 reject")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("source-sha256 格式无效")
    if not session_id:
        raise ValueError("resolve-intent 需要有效 session_id")

    resolved_scope = {}
    lock = acquire_lock()
    try:
        state = read_state(session_id=session_id)
        current_hash = prompt_sha256(state.get("last_prompt"))
        if not state.get("last_prompt") or source_sha256 != current_hash:
            raise ValueError("source-sha256 与当前最后一条用户消息不一致")
        if decision == "approve":
            blocker = approval_intent_blocker(state.get("last_prompt"))
            if blocker:
                raise ValueError(blocker)

        candidate = state.get("semantic_approval_candidate")
        if (
            not isinstance(candidate, dict)
            or candidate.get("source_sha256") != source_sha256
        ):
            raise ValueError("当前消息没有有效的语义审批候选")

        scope = pending_approval_scope(state)
        if not has_pending_approval(scope):
            raise ValueError("当前没有待审批任务、约束或危险命令")
        if candidate.get("scope") != scope:
            raise ValueError("待审批范围已变化，拒绝使用旧快照写回审批")

        gate = state.get("task_gate")
        if not isinstance(gate, dict):
            gate = {}
            state["task_gate"] = gate
        approvals = state.get("tool_approval")
        if not isinstance(approvals, dict):
            approvals = {}
            state["tool_approval"] = approvals
        pending_keys = list(scope.get("tool_keys") or [])
        task_pending = bool(scope.get("task_request_sha256"))
        contract_pending = bool(scope.get("contract_sha256"))

        if decision == "approve":
            approval_snapshot = _shared_script_module("approval_policy").snapshot(CODEX_DIR)
            approvals["approval_policy"] = approval_snapshot
            selected_profile = gate.get("work_profile")
            if (
                (task_pending or contract_pending)
                and (
                    not isinstance(selected_profile, dict)
                    or not selected_profile.get("required")
                )
            ):
                pending_profile = _pending_work_profile(
                    state,
                    requested=work_profile,
                )
                if pending_profile:
                    gate["work_profile"] = pending_profile
                    selected_profile = pending_profile
            if isinstance(selected_profile, dict) and selected_profile.get("required"):
                if not normalize_text(work_profile):
                    minimum = selected_profile.get("minimum") or selected_profile.get("profile")
                    raise ValueError(
                        "批准写任务时必须用 --work-profile 显式选择工作档，"
                        f"且不能低于 {minimum}"
                    )
                _apply_work_profile_selection(gate, work_profile, reason)
            if task_pending:
                gate["status"] = "approved"
                gate["requires_confirmation"] = True
                gate["approved_at"] = iso_now()
                gate["approval_policy"] = approval_snapshot
            if contract_pending:
                approve_change_contract(state)
            for key in pending_keys:
                approvals["approved_keys"] = _append_unique(
                    approvals.get("approved_keys", []),
                    key,
                    20,
                )
            approvals["pending_keys"] = []
        else:
            if task_pending:
                gate["status"] = "rejected"
                gate["requires_confirmation"] = True
                gate["rejected_at"] = iso_now()
                reasons = list(gate.get("reasons") or [])
                if "用户已拒绝当前待确认范围" not in reasons:
                    reasons.append("用户已拒绝当前待确认范围")
                gate["reasons"] = reasons
            contract = state.get("change_contract")
            if contract_pending and isinstance(contract, dict):
                contract["rejected_at"] = iso_now()
                contract["rejected"] = True
            approvals["pending_keys"] = []

        resolved_scope = {
            "task": task_pending,
            "contract": contract_pending,
            "tool_count": len(pending_keys),
        }
        resolution = {
            "decision": decision,
            "source_sha256": source_sha256,
            "reason": reason[:500],
            "scope": resolved_scope,
            "resolved_at": iso_now(),
        }
        selected = gate.get("work_profile")
        if decision == "approve" and isinstance(selected, dict) and selected.get("required"):
            resolution["work_profile"] = {
                "profile": selected.get("profile"),
                "minimum": selected.get("minimum"),
                "selection_source": selected.get("selection_source"),
            }
        state["last_intent_resolution"] = resolution
        state.pop("semantic_approval_candidate", None)
        write_state(state, session_id=session_id)
    finally:
        release_lock(lock)

    log_event(
        "approval_intent_resolved",
        {
            "session_id": session_id,
            "decision": decision,
            "source_sha256": source_sha256,
            "reason": reason[:500],
            "scope": resolved_scope,
        },
    )
    return resolution


def approval_settings_request(session_id, prompt):
    """Handle the portable settings menu before task classification.

    Only an exact slash command or a numeric answer to an open menu is consumed.
    Changing tolerance never approves an already pending task or dangerous tool.
    """
    try:
        module = _shared_script_module("approval_policy")
        lock = acquire_lock()
        try:
            state = read_state(session_id=session_id)
            request = module.command_request(
                prompt, CODEX_DIR, bool(state.get("approval_settings_pending")),
            )
            if request is None:
                if state.pop("approval_settings_pending", None):
                    write_state(state, session_id=session_id)
                return None
            action = request["action"]
            state["approval_settings_pending"] = action in {"menu", "invalid"}
            write_state(state, session_id=session_id)
        finally:
            release_lock(lock)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("approval_settings_error", {"error": str(exc)})
        return None
    context = {
        "menu": "[AgentRelay 权限设置]\n设置卡已显示。请输入 1、2、3，或 0 取消；不要替用户选择，也不要再次生成设置卡。",
        "invalid": "[AgentRelay 权限设置]\n输入无效，配置未修改。请用户回复 1、2、3，或 0 取消；不要替用户选择。",
        "cancel": "[AgentRelay 权限设置]\n用户已退出设置，配置未修改；无需继续设置流程。",
        "saved": f"[AgentRelay 权限设置]\n{request['message']}\n这是已经持久化并回读的设置结果，不要重复执行设置或把它当成任务批准。",
    }[action]
    emit_hook_context("UserPromptSubmit", context, system_message=request["message"])
    return read_state(session_id=session_id)


def update_task_gate_for_prompt(state, prompt):
    """更新任务门禁并返回需要注入给主 Agent 的说明。"""

    gate = state.get("task_gate")
    if not isinstance(gate, dict):
        gate = {}
        state["task_gate"] = gate
    gate.setdefault("status", "clear")
    gate.setdefault("level", "small")
    gate.setdefault("requires_confirmation", False)
    gate.setdefault("reasons", [])
    gate.setdefault("card_shown_sha256", None)

    approvals = state.get("tool_approval")
    if not isinstance(approvals, dict):
        approvals = {}
        state["tool_approval"] = approvals
    approvals.setdefault("pending_keys", [])
    approvals.setdefault("approved_keys", [])

    if gate.get("status") == "awaiting_confirmation":
        reasons = "；".join(str(value) for value in gate.get("reasons", []))
        if not reasons:
            reasons = "本地规则判定任务范围较大"
        return (
            "[AgentRelay 任务门禁]\n"
            f"本次请求需要先确认：{reasons}。\n"
            "先读取并遵循 AgentRelay Skill，向用户展示简短的需求卡/确认卡，"
            "说明修改范围、关键风险和验证方式。用户明确批准前，不要写入文件，"
            "也不要调用 Fusion、Commander 或其他协作模型。只读检查可以继续。"
        )

    classification = classify_task_request(
        prompt,
        contract=None if (state.get("change_contract") or {}).get("draft")
        else state.get("change_contract"),
    )
    if (
        gate.get("status") == "approved"
        and not classification["requires_confirmation"]
    ):
        return ""

    gate["level"] = classification["level"]
    gate["requires_confirmation"] = classification["requires_confirmation"]
    gate["reasons"] = classification["reasons"]
    gate["request_sha256"] = hashlib.sha256(
        normalize_text(prompt).encode("utf-8")
    ).hexdigest() if prompt else None
    gate["requested_at"] = iso_now() if classification["requires_confirmation"] else None
    gate["approved_at"] = None
    gate["work_profile"] = classification.get("work_profile")

    if not classification["requires_confirmation"]:
        gate["status"] = "clear"
        gate["notified_sha256"] = None
        gate["card_shown_sha256"] = None
        work_profile = gate.get("work_profile")
        if isinstance(work_profile, dict) and work_profile.get("required"):
            if _auto_record_light_profile(
                gate, str(state.get("session_id") or "")
            ):
                return ""
            minimum = work_profile.get("minimum") or work_profile.get("profile") or "light"
            tracker_path = str(Path(__file__).resolve())
            python_path = active_python_command()
            session_value = str(state.get("session_id") or "")
            return (
                "[AgentRelay 工作档]\n"
                f"本次是实际写任务，最低工作档为 {minimum}，不会显示用户确认卡。"
                "请主 Agent 按任务复杂度显式选择工作档，用户无需运行命令。可选择 "
                "standard、strict、user_required，但不能低于最低档。\n"
                "主 Agent 运行："
                + format_command([
                    python_path,
                    tracker_path,
                    "select-work-profile",
                    "--session",
                    session_value,
                    "--profile",
                    minimum,
                    "--reason",
                    "主 Agent 已按任务复杂度选择工作档",
                ])
            )
        return ""

    gate["status"] = "awaiting_confirmation"
    gate["notified_sha256"] = gate["request_sha256"]
    gate["card_shown_sha256"] = None
    reasons = "；".join(classification["reasons"])
    return (
        "[AgentRelay 任务门禁]\n"
        f"本地规则先要求确认：{reasons}。\n"
        "先读取并遵循 AgentRelay Skill，向用户展示简短的需求卡/确认卡，说明"
        "修改范围、关键风险和验证方式。用户明确批准前，只允许读取、搜索和"
        "安全测试，不要写入文件，也不要调用 Fusion、Commander 或其他协作模型。"
    )


def evaluate_pre_tool_gate(tool_name, tool_input, session_id):
    """原子检查危险命令和任务门禁，返回是否放行及拒绝原因。"""

    lock = acquire_lock()
    try:
        state = read_state(session_id=session_id)
        approvals = state.get("tool_approval")
        if not isinstance(approvals, dict):
            approvals = {}
            state["tool_approval"] = approvals
        approvals.setdefault("pending_keys", [])
        approvals.setdefault("approved_keys", [])

        solution_key = build_solution_key(tool_name, tool_input)
        danger_reason = dangerous_command_reason(tool_name, tool_input)
        try:
            mode = _shared_script_module("approval_policy").load_policy(CODEX_DIR)["mode"]
        except (ImportError, OSError, ValueError, TypeError):
            mode = "standard"
        command = ""
        command_cwd = (
            Path(str(tool_input.get("cwd") or os.getcwd()))
            if isinstance(tool_input, dict)
            else Path.cwd()
        )
        if tool_is_shell(tool_name):
            command = shell_command_text(tool_name, tool_input)
            try:
                if "deletion_policy.py" in command and _shared_script_module("deletion_policy").is_safe_delete_command(command, home=CODEX_DIR, mode=mode):
                    return True, None
            except (ImportError, OSError, ValueError, TypeError):
                pass
            if mode != "relaxed" and danger_reason == "删除文件或目录":
                if len(_shell_command_segments(command)) > 1:
                    return False, (
                        "[AgentRelay 安全门禁]\n危险命令与其他命令串联时，不允许整链一次批准。"
                        "请拆成独立步骤，先确认删除目标并移入回收站，再单独执行下载或验证。"
                    )
                replacement = None
                if mode == "standard":
                    try:
                        replacement = _shared_script_module("deletion_policy").safe_delete_command(
                            command, home=CODEX_DIR,
                            cwd=command_cwd,
                            mode=mode,
                        )
                    except ImportError:
                        pass
                if replacement:
                    return False, (
                        "[AgentRelay 回收站]\n该临时验证产物无需永久删除，也未显示额外审批卡。"
                        f"请只执行以下受控命令，将目标移入回收站并保留恢复清单：\n{replacement}"
                    )
        gate = state.get("task_gate")
        if not isinstance(gate, dict):
            gate = {}
        if mode == "relaxed" and danger_reason == "删除文件或目录" and tool_is_shell(tool_name):
            command = shell_command_text(tool_name, tool_input)
            try:
                if _shared_script_module("deletion_policy").parse_simple_delete(command) is not None:
                    return True, None
            except (ImportError, OSError, ValueError, TypeError):
                pass
        if danger_reason and solution_key:
            approved = [
                item for item in approvals.get("approved_keys", [])
                if isinstance(item, str) and item
            ]
            if solution_key in approved:
                approval_snapshot = approvals.get("approval_policy")
                if approval_snapshot and not _shared_script_module("approval_policy").resume_allowed(
                    CODEX_DIR, approval_snapshot,
                ):
                    approvals["approved_keys"] = []
                    approvals["pending_keys"] = _append_unique(
                        approvals.get("pending_keys", []), solution_key, 16,
                    )
                    write_state(state, session_id=session_id)
                    return False, (
                        "[AgentRelay 权限版本已变化]\n"
                        "当前权限模式比批准时更严格，旧批准不能恢复。"
                        "请重新确认当前任务和危险命令范围。"
                    )
                approved.remove(solution_key)
                approvals["approved_keys"] = approved
                write_state(state, session_id=session_id)
                if mode == "strict" and danger_reason == "删除文件或目录" and tool_is_shell(tool_name):
                    try:
                        replacement = _shared_script_module("deletion_policy").safe_delete_command(
                            command, home=CODEX_DIR,
                            cwd=command_cwd,
                            mode=mode,
                        )
                    except ImportError:
                        replacement = None
                    if replacement:
                        return False, (
                            "[AgentRelay 回收站]\n"
                            "strict 模式不执行原始永久删除，本次批准已用于该命令。"
                            "请只执行以下受控命令，将目标移入回收站并保留恢复清单：\n"
                            f"{replacement}"
                        )
                    return False, (
                        "[AgentRelay 安全门禁]\n"
                        "本次批准已用于该命令，但 strict 模式不允许执行原始永久删除，"
                        "且无法为当前目标生成受控回收命令。请改用明确的本地文件路径后重新确认。"
                    )
                return True, None

            approvals["pending_keys"] = _append_unique(
                approvals.get("pending_keys", []),
                solution_key,
                16,
            )
            write_state(state, session_id=session_id)
            return False, (
                "[AgentRelay 安全门禁]\n"
                f"检测到高风险操作：{danger_reason}。\n"
                "该工具调用尚未获得用户批准，已硬阻断。请向用户展示确认卡，"
                "说明命令用途、影响范围和验证方式。用户明确批准后，只允许"
                "完全相同的工具输入重试一次；修改命令或参数需要重新确认。"
            )

        gate = state.get("task_gate")
        if not isinstance(gate, dict):
            gate = {}
        if (
            gate.get("status") in {"awaiting_confirmation", "rejected"}
            and tool_writes_files(tool_name, tool_input)
        ):
            write_state(state, session_id=session_id)
            if gate.get("status") == "rejected":
                return False, (
                    "[AgentRelay 任务门禁]\n"
                    "用户已拒绝当前待确认范围，不能继续写入代码或修改文件。\n"
                    "如需继续，先提出新的方案并重新获得明确批准。"
                )
            scope_hint = "当前任务尚未获得用户批准，不能写入代码或修改文件。"
            contract = state.get("change_contract")
            if isinstance(contract, dict):
                scope_hint += (
                    f"\n目标：{contract.get('target_scope') or '按当前需求'}"
                    f"\n允许文件：{'、'.join(contract.get('allowed_files') or ['未明确，禁止写入'])}"
                    f"\n允许属性：{'、'.join(contract.get('allowed_properties') or ['仅限目标所需属性'])}"
                    f"\n禁止副作用：{'、'.join(contract.get('forbidden_effects') or ['公共组件、兄弟元素、其他模块'])}"
                    f"\n复用依据：{'、'.join(contract.get('reuse_evidence') or ['未提供，禁止另造同类组件'])}"
                    "\n扩大范围或共享影响必须重新确认。"
                )
            else:
                scope_hint += "\n目标外边界：公共组件、兄弟元素和其他模块保持不变；连带影响需重新确认。"
            return False, (
                "[AgentRelay 任务门禁]\n" + scope_hint +
                "\n确认卡已显示时请勿重复展示；等待用户明确批准，只读检查可以继续。"
            )
        if tool_writes_files(tool_name, tool_input):
            work_profile = gate.get("work_profile")
            auto_selected = None
            if not isinstance(work_profile, dict) or not work_profile.get("required"):
                work_profile = {
                    "required": True,
                    "profile": None,
                    "minimum": "light",
                    "card_required": False,
                    "reasons": ["实际写入前必须由主 Agent 显式选择工作档"],
                }
                gate["work_profile"] = work_profile
                state["task_gate"] = gate
            selection_source = work_profile.get("selection_source")
            if selection_source not in {"agent", "hook_auto_light"}:
                auto_selected = _auto_record_light_profile(gate, session_id)
                work_profile = gate.get("work_profile") or {}
                selection_source = work_profile.get("selection_source")
            if (
                isinstance(work_profile, dict)
                and work_profile.get("required")
                and selection_source not in {"agent", "hook_auto_light"}
            ):
                minimum = (
                    work_profile.get("minimum")
                    or work_profile.get("profile")
                    or "light"
                )
                session_value = str(session_id)
                tracker_path = str(Path(__file__).resolve())
                python_path = active_python_command()
                write_state(state, session_id=session_id)
                return False, (
                    "[AgentRelay 工作档]\n"
                    f"尚未显式选择工作档，首次写入已阻断。任务最低档为 {minimum}，"
                    "不能降档。请主 Agent 按任务复杂度选择工作档，用户无需运行命令。\n"
                    "主 Agent 运行："
                    + format_command([
                        python_path,
                        tracker_path,
                        "select-work-profile",
                        "--session",
                        session_value,
                        "--profile",
                        str(minimum),
                        "--reason",
                        "主 Agent 已按任务复杂度选择工作档",
                    ])
                    + "\n"
                    "纯问答、只读搜索和只读检查不需要选档。"
                )
            if auto_selected:
                write_state(state, session_id=session_id)
    finally:
        release_lock(lock)

    return True, None


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

    if is_gate_response_candidate(state, prompt):
        return True

    if matches_any_pattern(prompt, PAUSE_PROBLEM_PATTERNS):
        return False

    task_gate = state.get("task_gate")
    waiting_for_task = (
        isinstance(task_gate, dict)
        and task_gate.get("status") == "awaiting_confirmation"
    )
    approvals = state.get("tool_approval")
    waiting_for_tool = (
        isinstance(approvals, dict)
        and bool(approvals.get("pending_keys"))
    )
    if (
        (waiting_for_task or waiting_for_tool)
        and is_explicit_task_approval(prompt)
    ):
        return True

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
        # Shell 直出错误前缀（无 exit_code 结构时唯一可靠的失败信号），
        # 例如 zsh:1: no matches found: <...>
        r"zsh:\d+:",
        r"bash:\s*\S*:\s*line\s*\d+",
        r"no matches found",
        r"command not found",
        r"permission denied",
        r"segmentation fault",
        r"\bfatal:\s",
        r"\bpanic:",
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
    统一计算是否应向 Agent 发出“考虑首次求援”的候选提醒，
    以及提醒被放弃后的升级阶梯（第 2/3 次重发与强制执行）。

    Tracker 判断两个可机械验证的计数条件，并对用户消息做保守的
    直接行动句式预筛选：

    1. retry_count >= 3
    2. effective_time >= 15 分钟

    3. 当前消息疑似直接要求调用 AgentRelay

    第 3 项只是候选，不能由 DeepSeek / AgentRelay 关键词本身成立。
    用户是否真的要求当前调用必须由 Agent 结合语境判断。

    即使计数条件满足，Hook 也只发候选提醒；是否值得调用以及是否
    与当前未解决问题匹配，最终由 Agent 判断。首次提醒发出后，若
    模型放弃（问题仍未解决且未进入外援协作），按
    RELAY_ESCALATION_WAIT_SECONDS 的间隔升级重发，第 3 次提醒后
    仍无响应则转为 Hook 直接强制执行（force_due）。
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
    # Agent 审核。计数提醒的升级阶梯：首次提醒后若模型放弃（问题仍
    # 未解决且未进入外援协作），按 RELAY_ESCALATION_WAIT_SECONDS
    # 间隔重发第 2、3 次提醒，第 3 次之后转由 Hook 强制执行。
    signal_count = int(
        state.get(
            "relay_signal_count",
            0,
        )
        or 0
    )

    escalation_due = False
    force_due = False

    if (
        state.get("problem_active")
        and automatic_trigger
        and not already_triggered
        and not explicit_candidate
        and signal_count >= 1
        and state.get("relay_force_state") is None
    ):
        last_signal_at = state.get("relay_last_signal_at")
        wait_seconds = RELAY_ESCALATION_WAIT_SECONDS.get(
            signal_count
        )
        if (
            wait_seconds is not None
            and isinstance(last_signal_at, (int, float))
            and now_ts() - float(last_signal_at) >= wait_seconds
            and retry_count
            >= int(
                state.get(
                    "relay_signal_retry_count",
                    0,
                )
                or 0
            )
        ):
            if signal_count >= RELAY_MAX_SIGNALS:
                force_due = True
            else:
                escalation_due = True

    conditions["signal_count"] = signal_count
    conditions["escalation_due"] = escalation_due
    conditions["force_due"] = force_due

    conditions["should_trigger"] = (
        explicit_candidate
        or (
            automatic_trigger
            and not already_triggered
            and (
                signal_count == 0
                or escalation_due
            )
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

def relay_reminder_text(state, conditions):
    """
    按升级阶梯级别生成候选提醒文案。

    第 1 次：候选提醒，模型审核后自行决定是否调用。
    第 2 次：说明已等待 10 分钟，预告第 3 次与强制执行。
    第 3 次：最后一次提醒，预告 5 分钟后强制执行。
    """

    level = int(
        conditions.get(
            "signal_count",
            0,
        )
        or 0
    ) + 1

    problem_context = normalize_text(
        state.get("last_prompt")
    )

    if len(problem_context) > 500:
        problem_context = (
            problem_context[:497]
            + "..."
        )

    base = (
        f"retry_count: {conditions.get('retry_count')}。"
        "effective_time_seconds: "
        f"{int(conditions.get('effective_time_seconds', 0))}。"
        f"当前问题: {problem_context or '未记录用户问题摘要'}。"
        "模型判断优先级最高：必须先核对问题仍未解决、提醒与当前问题匹配、"
        "当前不是单纯等待，并判断外援是否确实值得调用。只有模型审核通过后才"
        "可根据 AgentRelay Skill 调用；审核不通过就忽略本提醒并继续正常处理。"
    )

    if level <= 1:
        return (
            "AgentRelay 候选求援提醒（不是调用命令）。"
            f"候选原因: {conditions.get('reason')}。"
            f"{base}"
            "Hook 不会调用 agent_relay.py；用户消息候选只来自保守的直接行动"
            "句式预筛选，单独出现 DeepSeek 或 AgentRelay 字样不能触发。"
        )

    if level == 2:
        return (
            "AgentRelay 第 2 次求援提醒（升级）。10 分钟前已发出第 1 次提醒，"
            f"问题仍未解决且未进入外援协作。{base}"
            "若本次仍不调用，5 分钟后将发出第 3 次（最后一次）提醒；"
            "第 3 次提醒后 5 分钟仍无响应，Hook 将直接代为强制执行外援调用，"
            "不再等待审核。"
        )

    return (
        "AgentRelay 第 3 次求援提醒（最后一次）。此前两次提醒均未触发"
        f"外援协作，问题仍未解决。{base}"
        "5 分钟后若仍未发起外援协作，Hook 将直接强制执行：由 Hook 代为调用"
        "在线支援模型，并把回答注入后续上下文，不再等待审核。"
    )


def forced_relay_artifacts(problem_id):
    """强制执行外援调用的输出与 pid 文件路径。"""

    safe_name = re.sub(
        r"[^0-9A-Za-z_-]",
        "_",
        str(problem_id or "unknown"),
    )
    target_dir = TRACKER_DIR / "forced_relay"
    return (
        target_dir / f"{safe_name}.out",
        target_dir / f"{safe_name}.pid",
    )


def find_relay_script():
    """定位 agent_relay.py，优先使用环境变量指定的路径。"""

    candidates = []

    env_script = os.environ.get(
        "AGENTRELAY_RELAY_SCRIPT"
    )
    if env_script:
        candidates.append(
            Path(env_script).expanduser()
        )

    candidates.append(
        CODEX_DIR
        / "skills"
        / "agent-relay"
        / "scripts"
        / "agent_relay.py"
    )
    candidates.append(
        HOME
        / ".agents"
        / "skills"
        / "agent-relay"
        / "scripts"
        / "agent_relay.py"
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    return None


def find_relay_python():
    """定位 AgentRelay 独立 Python；缺失时退回当前解释器。"""

    configured = os.environ.get("AGENTRELAY_PYTHON")
    candidates = (
        Path(configured).expanduser() if configured else None,
        CODEX_DIR / "agentrelay-env" / "bin" / "python",
        CODEX_DIR / "agentrelay-env" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate)

    return sys.executable


def pid_alive(pid):
    if os.name == "nt":
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def windows_pid_alive(pid):
    """Use a Win32 process handle because os.kill(pid, 0) is not a liveness probe."""
    if pid <= 0:
        return False
    try:
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return False
        try:
            return True
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def build_forced_relay_question(state):
    """根据当前问题状态拼装强制执行时发给支援模型的问题。"""

    prompt = normalize_text(
        state.get("last_prompt")
        or state.get("initial_prompt")
        or ""
    )

    if len(prompt) > 800:
        prompt = prompt[:797] + "..."

    retry_count = int(
        state.get("retry_count", 0) or 0
    )
    failed_count = int(
        state.get("failed_tool_call_count", 0) or 0
    )
    effective_seconds = int(
        float(
            state.get(
                "effective_time_seconds",
                0,
            )
            or 0
        )
    )
    failed_tool = (
        state.get("last_failed_solution_tool_name")
        or "未知工具"
    )

    return (
        "【AgentRelay 强制执行】本地 Agent 处理以下问题时陷入重复失败，"
        f"同一方向已重试 {retry_count} 次（失败工具调用 {failed_count} 次），"
        f"有效处理时长约 {effective_seconds} 秒，仍未解决。"
        f"问题：{prompt}"
        f"最近一次失败的工具类型：{failed_tool}。"
        "请直接给出最可能解决问题的可操作建议（命令、代码或排查步骤），"
        "不要寒暄，不要反问。"
    )


def launch_forced_relay(state):
    """
    后台启动 agent_relay.py 强制执行外援调用。

    Hook 自身有超时限制（Codex 默认 10 秒），而在线模型调用需要打开
    浏览器、耗时数分钟，因此这里只负责点火（Popen 立即返回），结果由
    collect_forced_relay_result 在后续 Hook 事件里检测并注入。

    返回要展示给 Agent 的通知文本；启动失败时返回失败说明。
    """

    problem_id = state.get("problem_id") or "unknown"
    out_file, pid_file = forced_relay_artifacts(
        problem_id
    )

    script = find_relay_script()
    if script is None:
        return (
            "AgentRelay 强制执行失败：未找到 agent_relay.py，"
            "无法代为调用在线支援模型。请按 AgentRelay Skill 手动调用。"
        )

    question = build_forced_relay_question(state)

    try:
        out_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        with open(
            out_file,
            "w",
            encoding="utf-8",
        ) as out_handle:
            process = subprocess.Popen(
                [
                    find_relay_python(),
                    "-X",
                    "utf8",
                    str(script),
                    question,
                ],
                stdout=out_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(script.parent),
                env=_utf8_environment(),
                **_detached_process_options(),
            )
        pid_file.write_text(
            str(process.pid),
            encoding="utf-8",
        )
    except OSError as exc:
        return (
            f"AgentRelay 强制执行失败：{exc}。"
            "请按 AgentRelay Skill 手动调用在线支援模型。"
        )

    state["relay_force_state"] = "launched"
    log_event(
        "relay_forced_launch",
        {
            "problem_id": problem_id,
            "pid": process.pid,
            "output_file": str(out_file),
            "retry_count": state.get("retry_count", 0),
        },
    )

    return (
        "AgentRelay 强制执行已启动：第 3 次提醒后 5 分钟仍无响应，"
        "Hook 已代为调用在线支援模型（后台执行，Playwright 会打开浏览器，"
        "完成后自动关闭）。回答就绪后会自动注入到后续上下文；"
        f"也可稍后查看 {out_file}。收到注入的回答后，请结合当前问题继续处理。"
    )


def collect_forced_relay_result(state):
    """
    检测后台强制执行是否结束，结束后把回答注入上下文。

    进程仍在运行时返回 None；已结束时更新状态（视为进入外援协作），
    返回投递文本。只投递一次。
    """

    if state.get("relay_force_state") != "launched":
        return None

    problem_id = state.get("problem_id") or "unknown"
    out_file, pid_file = forced_relay_artifacts(
        problem_id
    )

    if not out_file.exists():
        return None

    pid = None
    try:
        pid = int(
            pid_file.read_text(encoding="utf-8").strip()
        )
    except (OSError, ValueError):
        pid = None

    if pid and pid_alive(pid):
        return None

    try:
        answer = out_file.read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()
    except OSError:
        return None

    state["relay_force_state"] = "delivered"
    state["relay_triggered"] = True
    state["relay_collaboration_active"] = True
    state["relay_call_count"] = int(
        state.get("relay_call_count", 0) or 0
    ) + 1
    state["relay_trigger_reason"] = (
        state.get("relay_trigger_reason") or "forced"
    )
    state["relay_last_reason"] = "forced"

    log_event(
        "relay_forced",
        {
            "problem_id": problem_id,
            "retry_count": state.get("retry_count", 0),
            "answer_length": len(answer),
        },
    )

    if not answer:
        answer = "（外援调用已结束但没有输出。）"

    if len(answer) > 4000:
        answer = answer[:4000] + "\n…(截断)"

    return (
        "AgentRelay 强制执行结果（第 3 次提醒后未获响应，由 Hook 代为调用"
        "在线支援模型）：\n"
        f"{answer}\n"
        "请判断以上建议是否适用于当前环境，修改后重新验证。"
    )


def emit_relay_trigger(state, hook_event_name="PostToolUse"):
    """
    向 Agent 输出候选提醒，并按升级阶梯重发。

    三类输出：

    1. 权重到 2：先查本地归档
    2. 达到求援门槛或出现直接行动句式候选：考虑调用支援模型
       （第 1 次提醒；被放弃后按 RELAY_ESCALATION_WAIT_SECONDS
       升级重发第 2、3 次）
    3. 第 3 次提醒后仍无响应：Hook 直接强制执行外援调用，
       结果就绪后注入上下文（collect_forced_relay_result）

    注意：
    - 提醒本身不是调用命令，也不是已经确认的触发结论
    - 强制执行只在升级阶梯的最后一步发生，且每个问题只启动一次
    - 除强制执行投递外，不修改 state["relay_triggered"]
    """

    # Kimi Code 只有 Stop 事件的阻断原因会写入模型上下文（其余事件的
    # stdout 只进 transcript）；把非 Stop 事件的提醒推迟到 Stop 发出，
    # 由 shim 转成 exit 2 + stderr，避免提醒在 PostToolUse 阶段被丢弃。
    if (
        os.environ.get("AGENTRELAY_TARGET") == "kimi"
        and hook_event_name != "Stop"
    ):
        return False

    conditions = get_trigger_conditions(state)

    relay_due = bool(
        conditions.get("should_trigger")
    )

    archive_due = wants_archive_lookup(
        state,
        conditions,
    )

    notices = []

    # 强制执行结果投递优先：结果一旦就绪就注入，不等提醒门槛。
    delivery = collect_forced_relay_result(state)
    if delivery:
        notices.append(delivery)

    # 第 3 次提醒后等待期满仍无响应：直接强制执行。
    if (
        conditions.get("force_due")
        and state.get("relay_force_state") is None
    ):
        launch_notice = launch_forced_relay(state)
        if launch_notice:
            notices.append(launch_notice)

    reminder_recorded = False

    if relay_due:
        notices.append(
            relay_reminder_text(state, conditions)
        )
        if not conditions.get("explicit_candidate"):
            state["relay_signal_count"] = int(
                conditions.get("signal_count", 0) or 0
            ) + 1
            state["relay_last_signal_at"] = now_ts()
            state["relay_signal_retry_count"] = int(
                conditions.get("retry_count", 0) or 0
            )
            state["relay_signal_emitted"] = True
            state["relay_signal_problem_id"] = state.get(
                "problem_id"
            )
            reminder_recorded = True

    if archive_due:
        notices.append(
            archive_reminder_text(state)
        )

    if not notices:
        return False

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
    if reminder_recorded:
        log_event(
            "relay_signal",
            {
                "problem_id": state.get("problem_id"),
                "level": state.get("relay_signal_count"),
                "reason": conditions.get("reason"),
                "escalated": bool(
                    conditions.get("escalation_due")
                ),
            },
        )

    if archive_due:
        state["archive_signal_emitted"] = True
        state["archive_signal_problem_id"] = state.get(
            "problem_id"
        )

    state["explicit_relay_candidate"] = False

    write_state(state, session_id=state.get("session_id"))

    return True


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
        f'查询命令: "{sys.executable}" "{ARCHIVE_SCRIPT}" search "关键词"；'
        f'不确定项目名时先运行 "{sys.executable}" "{ARCHIVE_SCRIPT}" projects。'
        "命中时优先复用归档里已验证的结论，并在本次环境重新验证；"
        "没有命中再继续自行排查。归档只保存结论，不替 Agent 做决定，"
        "也不改变需求卡、确认卡和验证规则；weight=2 不会触发在线支援模型。"
    )


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
    try:
        _shared_script_module("delivery_audit").close(CODEX_DIR, str(state.get("session_id") or ""))
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("delivery_audit_close_error", {"error": str(exc)})
    state["pending_tools"] = {}
    state["relay_collaboration_active"] = False
    archive_current_problem(state)
    return state


def create_new_problem(
    prompt=None,
    session_id=None,
    agent_id=None,
    workspace=None,
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
    state["change_tracking"] = new_change_tracking(
        normalized_prompt,
        session_id=session_id,
        workspace=workspace,
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
    workspace=None,
):
    """
    用户提交新消息。

    处理：

    1. 没有当前问题 -> 创建新问题
    2. 当前问题已经解决 -> 创建新问题
    3. 当前问题继续对话 -> 更新 turn_count
    4. 显式 AgentRelay 请求由 Hook 做保守候选预检，最终仍由 Agent 判断
    """

    prompt = normalize_text(prompt)

    lock = acquire_lock()

    try:
        state = read_state(
            session_id=session_id
        )
        workspace = _change_workspace(workspace)
        tracking = change_tracking(state)
        approval_continuation = (
            is_gate_response_candidate(state, prompt)
            or is_explicit_task_approval(prompt)
        )
        reset_change_tracking = (
            not tracking.get("turn_id")
            or tracking.get("recorded")
            or (
                bool(state.get("turn_closed"))
                and not tracking.get("had_write")
                and not approval_continuation
            )
            or (
                tracking.get("workspace")
                and tracking.get("workspace") != workspace
                and not tracking.get("had_write")
            )
        )
        if reset_change_tracking:
            state["change_tracking"] = new_change_tracking(
                _change_request_for_prompt(state, prompt),
                session_id=session_id,
                workspace=workspace,
            )
        else:
            tracking["workspace"] = workspace
            if not tracking.get("request") and prompt:
                tracking["request"] = prompt

        pause_requested = (
            matches_any_pattern(prompt, PAUSE_PROBLEM_PATTERNS)
            and not is_gate_response_candidate(state, prompt)
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
                workspace=workspace,
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
                    workspace=workspace,
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
                workspace=workspace,
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
    workspace=None,
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
            preflight_state = state
            state = create_new_problem(
                session_id=session_id,
                agent_id=state.get("agent_id"),
                workspace=workspace,
            )
            # PreToolUse may arrive without a preceding UserPromptSubmit.
            # Keep the gate decision made immediately before this call instead
            # of replacing it with create_new_problem() defaults.
            if isinstance(preflight_state.get("task_gate"), dict):
                state["task_gate"] = preflight_state["task_gate"]

        # UserPromptSubmit 到工具启动之间是 Agent 的主动分析/思考时间。
        accrue_active_time(state, ended_at=started_at)

        pending_tools = state.get(
            "pending_tools",
            {},
        )

        pending_tools[tool_id] = {
            "tool_name": tool_name,
            "tool_input": tool_input,
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
    workspace=None,
    record_write=True,
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
        if pending and not tool_input:
            tool_input = pending.get("tool_input")

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

        # Only a successful PostToolUse can produce the user-facing change
        # summary. Failed writes and PostToolUseFailure stay out of history.
        if (
            record_write
            and not tool_failed
            and tool_writes_files(tool_name, tool_input)
        ):
            tracking = change_tracking(state)
            if (
                not tracking.get("turn_id")
                or tracking.get("recorded")
                or (
                    bool(state.get("turn_closed"))
                    and not tracking.get("had_write")
                )
            ):
                tracking = new_change_tracking(
                    _change_request_for_prompt(
                        state,
                        state.get("last_prompt"),
                    ),
                    session_id=session_id,
                    workspace=workspace or tracking.get("workspace"),
                )
                state["change_tracking"] = tracking
            tracking["workspace"] = _change_workspace(
                workspace or tracking.get("workspace")
            )
            tracking["files"] = _append_unique_strings(
                tracking.get("files"),
                written_file_targets(
                    tool_name,
                    tool_input,
                    tracking["workspace"],
                ),
                limit=50,
            )
            tracking["tools"] = _append_unique_strings(
                tracking.get("tools"),
                [tool_name],
                limit=12,
            )
            tracking["had_write"] = True

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
        informational_reply = (
            assistant_message.lstrip().startswith("<task-notification>")
            or is_informational_prompt(assistant_message)
            or bool(re.search(r"只是(?:询问|解释|历史待办)|不是(?:继续执行|用户授权)|无需(?:重复|继续)|不会再次", assistant_message))
        )
        state["last_assistant_message"] = assistant_message
        stop_event_state = state

        if informational_reply:
            change_summary = record_change_history(
                state,
                assistant_message,
            )
            state["turn_closed"] = True
            state["active_started_at"] = None
            try:
                _shared_script_module("delivery_audit").close(CODEX_DIR, str(session_id))
            except (ImportError, OSError, ValueError, TypeError) as exc:
                log_event("delivery_audit_close_error", {"error": str(exc)})
            write_state(state, session_id=session_id)
            emit_stop_summary(change_summary)
            return state

        # 助手已明确宣告解决时，优先按解决处理：
        # 此时再拦截本轮去提醒查归档/重试，只会打扰一个已经结束的问题。
        if (
            state.get("problem_active")
            and not assistant_indicates_resolution(assistant_message)
            and emit_relay_trigger(
                state,
                hook_event_name="Stop",
            )
        ):
            state["turn_closed"] = False
            start_active_time(state)
            write_state(state, session_id=session_id)
            return state

        change_summary = record_change_history(
            state,
            assistant_message,
        )

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
        emit_stop_summary(change_summary)

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
        resolve-intent --session SESSION_ID --decision approve|reject
                       --source-sha256 SHA256 --reason TEXT
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

        if command == "finish-check":
            message = ""
            remaining = list(args)
            if "--message" in remaining:
                index = remaining.index("--message")
                if index + 1 >= len(remaining):
                    print("Error: --message requires TEXT", file=sys.stderr)
                    return 1
                message = remaining[index + 1]
                remaining = remaining[:index] + remaining[index + 2:]
            if remaining:
                print(
                    "Error: unexpected arguments: " + " ".join(remaining),
                    file=sys.stderr,
                )
                return 1
            if not session_id:
                try:
                    session_id = _page_verification_module().resolve_current_session(
                        CODEX_DIR,
                        cwd=os.getcwd(),
                        environ=dict(os.environ),
                    )
                except (AttributeError, ImportError, OSError, ValueError, TypeError):
                    session_id = ""
            if not session_id:
                print(
                    "Error: cannot resolve a unique current session; pass --session",
                    file=sys.stderr,
                )
                return 1
            result = completion_preflight(session_id, message)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1

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

        if command == "resolve-intent":
            values = {}
            remaining = list(args)
            for option, key in (
                ("--decision", "decision"),
                ("--source-sha256", "source_sha256"),
                ("--reason", "reason"),
                ("--work-profile", "work_profile"),
            ):
                if option not in remaining:
                    continue
                index = remaining.index(option)
                if index + 1 >= len(remaining):
                    print(
                        f"Error: {option} requires a value",
                        file=sys.stderr,
                    )
                    return 1
                values[key] = remaining[index + 1]
                remaining = remaining[:index] + remaining[index + 2:]
            if remaining:
                print(
                    "Error: unexpected arguments: " + " ".join(remaining),
                    file=sys.stderr,
                )
                return 1
            if not values.get("decision") or not values.get("source_sha256"):
                print(
                    "Error: --decision and --source-sha256 are required",
                    file=sys.stderr,
                )
                return 1
            resolved_session = (
                session_id
                or normalize_session_id(os.environ.get("CODEX_SESSION_ID"))
            )
            try:
                result = resolve_pending_intent(
                    resolved_session,
                    values["decision"],
                    values["source_sha256"],
                    values.get("reason") or "",
                    values.get("work_profile"),
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            print(
                json.dumps(
                    {"ok": True, **result},
                    ensure_ascii=False,
                )
            )
            return 0

        if command == "select-work-profile":
            values = {}
            remaining = list(args)
            for option, key in (
                ("--profile", "profile"),
                ("--reason", "reason"),
            ):
                if option not in remaining:
                    continue
                index = remaining.index(option)
                if index + 1 >= len(remaining):
                    print(
                        f"Error: {option} requires a value",
                        file=sys.stderr,
                    )
                    return 1
                values[key] = remaining[index + 1]
                remaining = remaining[:index] + remaining[index + 2:]
            if remaining:
                print(
                    "Error: unexpected arguments: " + " ".join(remaining),
                    file=sys.stderr,
                )
                return 1
            if not values.get("profile"):
                print("Error: --profile is required", file=sys.stderr)
                return 1
            resolved_session = (
                session_id
                or normalize_session_id(os.environ.get("CODEX_SESSION_ID"))
            )
            try:
                result = select_task_work_profile(
                    resolved_session,
                    values["profile"],
                    values.get("reason") or "",
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            print(json.dumps({"ok": True, **result}, ensure_ascii=False))
            return 0

        if command == "change-summary":
            summary = ""
            file_groups = []
            remaining = list(args)
            while remaining:
                option = remaining.pop(0)
                if option not in {"--summary", "--file-group"}:
                    print(
                        "Error: unexpected arguments: " + option,
                        file=sys.stderr,
                    )
                    return 1
                if not remaining:
                    print(
                        f"Error: {option} requires a value",
                        file=sys.stderr,
                    )
                    return 1
                value = remaining.pop(0)
                if option == "--summary":
                    summary = value
                else:
                    file_groups.append(value)
            if not normalize_text(summary):
                print("Error: --summary is required", file=sys.stderr)
                return 1
            resolved_session = (
                session_id
                or normalize_session_id(os.environ.get("CODEX_SESSION_ID"))
                or normalize_session_id(os.environ.get("CODEX_THREAD_ID"))
            )
            if not resolved_session:
                try:
                    resolved_session = _page_verification_module().resolve_current_session(
                        CODEX_DIR,
                        cwd=os.getcwd(),
                        environ=dict(os.environ),
                    )
                except (AttributeError, ImportError, OSError, ValueError, TypeError):
                    resolved_session = ""
            if not resolved_session:
                print(
                    "Error: cannot resolve a unique current session; pass --session",
                    file=sys.stderr,
                )
                return 1
            try:
                result = annotate_change_summary(
                    resolved_session,
                    summary,
                    file_groups,
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            print(json.dumps({"ok": True, **result}, ensure_ascii=False))
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

def _shared_script_module(name: str):
    """Load an installed Skill script without duplicating tracker policy."""
    import importlib
    import importlib.util
    try:
        package_spec = importlib.util.find_spec("scripts")
        if package_spec is not None and importlib.util.find_spec(f"scripts.{name}") is not None:
            return importlib.import_module(f"scripts.{name}")
    except ImportError:
        pass
    for base in (CODEX_DIR / "skills" / "agent-relay", Path(__file__).resolve().parent.parent):
        if (base / "scripts" / f"{name}.py").is_file():
            sys.path.insert(0, str(base / "scripts"))
            return importlib.import_module(name)
    raise ModuleNotFoundError(f"AgentRelay shared script not installed: {name}")


def _page_verification_module():
    """Resolve scripts both from the source checkout and the installed Skill."""
    return _shared_script_module("page_verification")


def ensure_change_contract(state, prompt, data):
    """Create a draft for UI work even when the agent forgets to supply JSON."""
    if isinstance(data.get("change_contract"), dict) or isinstance(state.get("change_contract"), dict):
        return
    text = normalize_text(prompt)
    ui_terms = (
        r"UI|界面|页面|样式|骨架|按钮|滚动条|输入框|布局|颜色|组件|骨架屏|"
        r"导航|卡片|弹窗|字体|图标|文字|间距|圆角|阴影|对齐|宽度|高度|"
        r"点击态|点击事件|遮罩层|遮罩|轮廓|选中态|悬停态|聚焦态"
    )
    ui_action = (
        r"修改|改成|改为|调整|变更|替换|设置|增加|添加|删除|缩小|放大|"
        r"移动|优化|修复|复刻|注意|保持|保留|一致|相同|一样|"
        r"change|update|adjust|resize|move"
    )
    if not re.search(ui_terms, text, re.I) or not re.search(ui_action, text, re.I):
        return
    if not matches_any_pattern(text, TASK_WRITE_ACTION_PATTERNS) and not re.search(ui_action, text, re.I):
        return
    workspace = str(data.get("cwd") or os.getcwd())
    files = _request_files(text)
    try:
        module = _shared_script_module("change_contract")
        scope_match = re.search(
            r"(.+?)(?:的)?(?:按钮|滚动条|输入框|骨架屏|骨架|组件|颜色|布局|样式|"
            r"点击态|点击事件|遮罩层|遮罩|轮廓)",
            text,
        )
        scope = scope_match.group(0)[:160] if scope_match else ""
        contract = module.contract_for_prompt(text, workspace, files,
                                               references=["用户当前请求（具体参考待确认）"])
        contract["target_scope"] = scope
        contract["reuse_checked"] = False
        contract["draft"] = True
        state["change_contract"] = contract
    except (ImportError, ValueError, TypeError) as exc:
        log_event("change_contract_error", {"error": str(exc)})


def update_change_contract_for_prompt(state, prompt, data):
    """Honor an explicit contract proposal; approval freezes the baseline once."""
    candidate = data.get("change_contract")
    if isinstance(candidate, dict):
        current = state.get("change_contract")
        if isinstance(current, dict) and current.get("approved") and candidate != current:
            # A new scope is a new contract and must be approved again.
            candidate = dict(candidate, approved=False, baseline={})
        state["change_contract"] = dict(candidate)
    contract = state.get("change_contract")
    if not isinstance(contract, dict):
        return ""
    module = _shared_script_module("change_contract")
    if not contract.get("workspace"):
        contract["workspace"] = str(data.get("cwd") or os.getcwd())
    gate = state.setdefault("task_gate", {})
    gate["change_contract"] = contract
    if not contract.get("approved"):
        if contract.get("draft"):
            return ("[AgentRelay 变更约束草案]\n已自动记录 UI 目标与边界；清晰小改按权限模式执行，"
                    "复杂/共享影响或复用不明时再确认，不把辅助草案当第二次审批。")
        gate["status"] = "awaiting_confirmation"
        gate["requires_confirmation"] = True
        gate.setdefault("reasons", ["严格变更约束需要用户确认"])
        gate["request_sha256"] = hashlib.sha256(normalize_text(prompt).encode("utf-8")).hexdigest()
        return ("[AgentRelay 严格变更约束]\n确认卡必须明确 UI 参考、创作权限、允许文件和禁止范围；"
                "默认严格参考，不擅自优化，额外 Bug 只报告。批准后才可按约定写入。")
    return ""


def approve_change_contract(state):
    contract = state.get("change_contract")
    if (
        not isinstance(contract, dict)
        or contract.get("approved")
        or contract.get("draft")
    ):
        return
    module = _shared_script_module("change_contract")
    state["change_contract"] = module.approve(contract)
    state.setdefault("task_gate", {})["change_contract"] = state["change_contract"]


def change_contract_block(session_id, data, tool_name, tool_input):
    try:
        contract = read_state(session_id=session_id).get("change_contract")
        if not isinstance(contract, dict):
            return None
        return _shared_script_module("change_contract").check_tool_write(contract, tool_name, tool_input)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("change_contract_error", {"error": str(exc)})
        return None


def change_contract_post_tool_block(session_id, tool_name, tool_input):
    try:
        contract = read_state(session_id=session_id).get("change_contract")
        if not isinstance(contract, dict) or not contract.get("approved"):
            return None
        normalized = re.sub(r"[^a-z0-9_]+", "_", str(tool_name).lower())
        if normalized not in {"bash", "shell", "exec_command", "execute_command", "run_command"}:
            return None
        violations = _shared_script_module("change_contract").diff_violations(contract)
        if violations:
            return "[AgentRelay 变更约束验收失败]\n" + "\n".join(violations) + "\n命令已造成越界修改，需恢复或重新获得批准。"
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("change_contract_error", {"error": str(exc)})
    return None


def change_contract_completion_block(session_id, message):
    try:
        contract = read_state(session_id=session_id).get("change_contract")
        if not isinstance(contract, dict):
            return None
        return _shared_script_module("change_contract").completion_block(contract, message)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("change_contract_error", {"error": str(exc)})
        return None


AUDIT_COMMAND_PATTERN = re.compile(r"^/agent-audit(?:\s+(status))?\s*$", re.I)


def delivery_audit_status_request(session_id, prompt):
    """Show the real audit checklist; never turn the command itself into a pass."""
    if not AUDIT_COMMAND_PATTERN.match(str(prompt or "").strip()):
        return None
    try:
        module = _shared_script_module("delivery_audit")
        value = module.status(CODEX_DIR, session_id)
        message = module.render_status(CODEX_DIR, session_id)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("delivery_audit_error", {"error": str(exc)})
        message = "「AgentRelay · 交付审计」\n审计模块不可用，尚未验证；请重新安装 AgentRelay。"
        value = {"status": "unavailable"}
    if value.get("status") == "not_started":
        message += "\n当前没有已记录的修改。普通问答无需审计；有修改时 Hook 会自动建立待办。"
    else:
        message += ("\n这只是历史待办状态，不是继续执行的授权。若只是询问用途，仅解释即可；"
                    "只有你明确要求继续当前修改任务时，才读取 diff、执行匹配检查并登记真实证据。"
                    "不得因本提醒自动审计、修改或调用 Fusion。")
    emit_hook_context(
        "UserPromptSubmit",
        "[AgentRelay 交付审计]\n已向用户显示真实审计状态。此命令仅用于查看，不是用户授权执行审计。"
        "不得因为本提醒自动运行测试、修改文件、调用 Fusion 或开始新的审计；只有用户明确要求继续时才执行。"
        "若用户只是询问用途，仅回答用途。命令本身不代表审计通过。",
        system_message=message,
    )
    return read_state(session_id=session_id)


def _delivery_audit_can_activate(tracker_state, task_id, current):
    """Only a genuinely new write task may activate an audit; history cannot revive it."""
    if current.get("status") == "closed":
        return False
    existing_task = str(current.get("task_id") or "").strip()
    if existing_task and existing_task == str(task_id).strip():
        return bool(current.get("active_edit"))
    return bool(tracker_state.get("problem_active")) and not is_informational_prompt(
        str(tracker_state.get("last_prompt") or "")
    )


LOGIC_CHANGE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".go", ".rs",
    ".java", ".kt", ".c", ".cpp", ".h", ".sql", ".sh",
}


def delivery_audit_before_write(session_id, data, tool_name, tool_input):
    """Freeze the real pre-write baseline after every gate has allowed a call."""
    if not tool_writes_files(tool_name, tool_input):
        return None
    module = _shared_script_module("delivery_audit")
    tracker_state = read_state(session_id=session_id)
    request = str(tracker_state.get("initial_prompt") or tracker_state.get("last_prompt") or "")
    if request.lstrip().startswith("<task-notification>"):
        request = str(tracker_state.get("last_prompt") or "")
    task_id = str(tracker_state.get("task_id") or tracker_state.get("problem_id") or "")
    mode = _shared_script_module("approval_policy").load_policy(CODEX_DIR)["mode"]
    workspace = Path(str(data.get("cwd") or os.getcwd())).expanduser().resolve()
    current = module.read_state(CODEX_DIR, session_id)
    targets = [] if tool_is_shell(tool_name) else _tool_targets(tool_name, tool_input)
    target_paths = set()
    for value in targets:
        path = Path(value).expanduser()
        target_paths.add(path.resolve() if path.is_absolute() else (workspace / path).resolve())
    files = []
    for value in list(current.get("files") or []) + [str(path) for path in target_paths]:
        path = Path(value).expanduser()
        absolute = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        try:
            absolute.relative_to(workspace)
        except ValueError:
            continue
        if absolute.is_file() or absolute in target_paths:
            files.append(str(absolute))
    style_change = bool(re.search(
        r"颜色|圆角|按钮.*(?:方|圆)|配色|字号|字体|间距|边框|阴影|background|border-radius|font-size|padding|margin",
        request, re.I,
    )) and len(set(files)) == 1 and not re.search(r"公共组件|共享组件|全局|整个项目|全项目|跨模块|多页面|全部模块", request, re.I)
    candidate = {
        "request": request,
        "files": sorted(set(files)),
        "mode": mode,
        "style_change": style_change,
    }
    if mode == "relaxed" and not style_change and not module._complex(candidate):
        return None

    gate = tracker_state.get("task_gate")
    work_profile = gate.get("work_profile") if isinstance(gate, dict) else None
    selected_profile = (
        str(work_profile.get("profile") or "")
        if isinstance(work_profile, dict)
        else ""
    )
    logic_change = bool(
        not style_change
        and any(Path(value).suffix.lower() in LOGIC_CHANGE_SUFFIXES for value in files)
    )
    if style_change and mode != "strict" and not module._complex(candidate):
        return module.begin(
            CODEX_DIR, session_id, str(workspace), request,
            mode=mode, files=files, task_id=task_id, style_change=True,
            work_profile=selected_profile or None, logic_change=False,
            activate=_delivery_audit_can_activate(
                tracker_state, task_id, current,
            ),
        )
    return module.begin(
        CODEX_DIR, session_id, str(workspace), request,
        mode=mode, files=files, task_id=task_id,
        work_profile=selected_profile or None, logic_change=logic_change,
        activate=_delivery_audit_can_activate(tracker_state, task_id, current),
    )


def delivery_audit_after_write(session_id):
    """Advance only the candidate fingerprint after a successful write."""
    module = _shared_script_module("delivery_audit")
    if not module.read_state(CODEX_DIR, session_id):
        return None
    return module.refresh_candidate(CODEX_DIR, session_id)


def delivery_audit_completion_block(session_id, message, *, mutate=True):
    try:
        tracker_state = read_state(session_id=session_id)
        if tracker_state.get("problem_status") in {"resolved", "completed", "closed"}:
            return None
        task_gate = tracker_state.get("task_gate")
        if isinstance(task_gate, dict) and task_gate.get("status") == "awaiting_confirmation":
            # Requirement confirmation is not delivery. An old audit from a
            # previous task must not turn plan wording into a completion gate.
            return None
        module = _shared_script_module("delivery_audit")
        audit_state = module.read_state(CODEX_DIR, session_id)
        if not audit_state or audit_state.get("status") == "closed":
            return None
        if not audit_state.get("active_edit"):
            # History alone never restarts an audit after Stop or a question.
            return None
        if not tracker_state.get("problem_active") or tracker_state.get("turn_closed"):
            # A closed turn cannot reactivate an old audit merely because a new
            # assistant message mentions completion. A new write creates a new cycle.
            return None
        current_task_id = str(
            tracker_state.get("task_id") or tracker_state.get("problem_id") or ""
        ).strip()
        audit_task_id = str(audit_state.get("task_id") or "").strip()
        if not audit_task_id or (
            current_task_id and audit_task_id != current_task_id
        ):
            # Legacy evidence has no task identity. A different task id also
            # cannot prove that it belongs to the current problem.
            return None
        if not module.completion_claim(message):
            return None
        mode = _shared_script_module("approval_policy").load_policy(CODEX_DIR)["mode"]
        page = _page_verification_module()
        page_state = page.read_state(CODEX_DIR, session_id)
        if mutate and page_state.get("files") and not (audit_state.get("checks") or {}).get("visual"):
            visual = page_state.get("visual_review") or {}
            if visual.get("status") in {"passed", "user_review_required"}:
                module.record_check(
                    CODEX_DIR, session_id, "visual", visual["status"] == "passed",
                    {"status": visual["status"], "mode": visual.get("mode"),
                     "certification": visual.get("certification"),
                     "page_gate_passed": visual["status"] == "passed" and page.verify_evidence(CODEX_DIR, session_id) is None,
                     "user_review_required": visual["status"] != "passed"},
                )
        reason = module.completion_block(
            CODEX_DIR,
            session_id,
            mode,
            message,
            mutate=mutate,
        )
        if reason and module.status(CODEX_DIR, session_id).get("status") != "passed":
            reason += "\n输入 /agent-audit 查看当前待办。"
        return reason
    except ImportError as exc:
        log_event("delivery_audit_import_error", {"error": str(exc)})
        safe_session = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))
        pending = CODEX_DIR / "agent_relay_tracker" / "delivery-audit" / f"{safe_session}.json"
        if not pending.is_file():
            return None
        return ("[AgentRelay 交付审计] 已存在待审计任务，但审计模块不可用，"
                "不能声称已完成审计；请重新安装 AgentRelay。")
    except (OSError, ValueError, TypeError) as exc:
        log_event("delivery_audit_error", {"error": str(exc)})
        safe_session = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))
        pending = CODEX_DIR / "agent_relay_tracker" / "delivery-audit" / f"{safe_session}.json"
        if not pending.is_file():
            return None
        return ("[AgentRelay 交付审计] 无法读取或核验当前交付审计，不能声称完成。"
                "请修复审计模块或状态文件后重新运行 /agent-audit。")


def record_page_verification_edit(session_id, data, tool_name, tool_input, tool_output,
                                  before=False):
    """Keep page evidence separate from the long-lived problem tracker state."""
    try:
        module = _page_verification_module()
        if before:
            module.record_command_start(CODEX_DIR, session_id, str(data.get("cwd") or os.getcwd()),
                                        tool_name, tool_input)
        else:
            module.record_page_edit(CODEX_DIR, session_id, str(data.get("cwd") or os.getcwd()),
                                    tool_name, tool_input, tool_output)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("page_verification_record_error", {"error": str(exc)})


def page_completion_block_reason(session_id, message):
    try:
        tracker_state = read_state(session_id=session_id)
        if tracker_state.get("problem_status") in {"resolved", "completed", "closed"}:
            return None
        module = _page_verification_module()
        if module.completion_claim(message):
            audit_state = _shared_script_module("delivery_audit").read_state(CODEX_DIR, session_id)
            if audit_state.get("style_change") and audit_state.get("mode") != "strict":
                page_state = module.read_state(CODEX_DIR, session_id)
                target = page_state.get("target_review") or {}
                if not target.get("target_verified"):
                    return "[AgentRelay 轻量样式验收] 请先验证目标样式与关键边界，小修改无需完整审计。"
                if page_state.get("invariants_before") and any(
                    not item.get("passed") for item in page_state.get("unchanged_results") or []
                ):
                    return "[AgentRelay 轻量样式验收] 目标外位置尺寸或计算样式发生变化。"
                return None
            return module.verify_evidence(CODEX_DIR, session_id, message)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("page_verification_check_error", {"error": str(exc)})
    return None


def visual_review_block_reason(session_id, message):
    """Require scope-aware visual acceptance, not merely an existing screenshot."""
    try:
        page = _page_verification_module()
        visual = _shared_script_module("visual_review")
        if not page.completion_claim(message) and not visual.visual_claim(message):
            return None
        state = page.read_state(CODEX_DIR, session_id)
        if not state.get("files"):
            if re.search(r"视觉验收通过|页面[^\n。！？]{0,50}(?:检查通过|验证通过)|亲自检查了截图[^\n。！？]{0,60}(?:正常|无问题|没有大块空白)", message, re.I):
                return (
                    "[AgentRelay 视觉验收] 当前没有页面文件修改记录或逐项视觉认证，"
                    "不能把读取图片直接扩大为页面视觉验收通过。仅查看用户图片不需要页面审计。"
                )
            return None
        audit_state = _shared_script_module("delivery_audit").read_state(CODEX_DIR, session_id)
        if audit_state.get("style_change") and audit_state.get("mode") != "strict":
            target = state.get("target_review") or {}
            if not target.get("target_verified"):
                return "[AgentRelay 轻量样式验收] 请完成目标样式与关键边界检查；小修改无需完整审计。"
            if state.get("invariants_before") and any(
                not item.get("passed") for item in state.get("unchanged_results") or []
            ):
                return "[AgentRelay 轻量样式验收] 目标外位置尺寸或计算样式发生变化，需修复或重新确认。"
            return None
        return visual.completion_block(CODEX_DIR, session_id, message)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("visual_review_error", {"error": str(exc)})
        return None


def pending_page_verification_reason(session_id):
    try:
        tracker_state = read_state(session_id=session_id)
        if tracker_state.get("problem_status") in {"resolved", "completed", "closed"}:
            return None
        return _page_verification_module().verify_evidence(CODEX_DIR, session_id)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("page_verification_check_error", {"error": str(exc)})
        return None


def _preflight_item(stage, code, reason, next_command=""):
    return {
        "stage": stage,
        "code": code,
        "reason": str(reason),
        "next_command": str(next_command or ""),
    }


def _shared_script_path(name: str) -> str:
    for base in (CODEX_DIR / "skills" / "agent-relay", Path(__file__).resolve().parent.parent):
        path = base / "scripts" / f"{name}.py"
        if path.is_file():
            return str(path)
    return f"scripts/{name}.py"


def completion_preflight(session_id, message=""):
    """Read-only finish check that returns one exact next stage, never finalizes state."""
    gate_message = str(message or "").strip() or "验收预检：已修复、已检查并验证通过。"
    contract_reason = change_contract_completion_block(session_id, gate_message)
    if contract_reason:
        return {
            "ok": False,
            "session_id": session_id,
            "items": [_preflight_item(
                "contract",
                "change_contract_pending",
                contract_reason,
                "按确认卡补齐目标、边界和实际 diff 后重新验收",
            )],
        }

    page_reason = page_completion_block_reason(session_id, gate_message)
    if page_reason:
        try:
            detail = _page_verification_module().verification_preflight(
                CODEX_DIR,
                session_id,
                gate_message,
            )
        except (AttributeError, ImportError, OSError, ValueError, TypeError):
            detail = {}
        if detail and not detail.get("ok"):
            item = _preflight_item(
                str(detail.get("phase") or "page"),
                str(detail.get("code") or "page_verification_missing"),
                detail.get("reason") or page_reason,
                str(detail.get("next_command") or ""),
            )
        else:
            code = (
                "invariant_regression"
                if "目标外" in page_reason
                else "target_review_missing"
            )
            item = _preflight_item(
                "target" if code == "target_review_missing" else "invariants",
                code,
                page_reason,
                format_command([
                    active_python_command(),
                    "scripts/verify_page.py",
                    *(
                        ["--target-selector", "<目标选择器>", "--after"]
                        if code == "target_review_missing"
                        else ["--after"]
                    ),
                ]),
            )
        return {"ok": False, "session_id": session_id, "items": [item]}

    visual_reason = visual_review_block_reason(session_id, gate_message)
    if visual_reason:
        return {
            "ok": False,
            "session_id": session_id,
            "items": [_preflight_item(
                "visual",
                "visual_review_missing",
                visual_reason,
                format_command([
                    active_python_command(),
                    _shared_script_path("visual_review"),
                    "record",
                    "--session",
                    str(session_id),
                    "--home",
                    str(CODEX_DIR),
                ]),
            )],
        }

    audit_reason = delivery_audit_completion_block(
        session_id,
        gate_message,
        mutate=False,
    )
    if audit_reason:
        return {
            "ok": False,
            "session_id": session_id,
            "items": [_preflight_item(
                "delivery_audit",
                "delivery_audit_pending",
                audit_reason,
                format_command([
                    active_python_command(),
                    _shared_script_path("delivery_audit"),
                    "status",
                    "--session",
                    str(session_id),
                    "--home",
                    str(CODEX_DIR),
                ]),
            )],
        }

    return {"ok": True, "session_id": session_id, "items": []}


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

    try:
        bind_session = getattr(
            _page_verification_module(),
            "bind_workspace_session",
            None,
        )
        if callable(bind_session):
            bind_session(
                CODEX_DIR,
                session_id,
                str(data.get("cwd") or os.getcwd()),
                host=str(os.environ.get("AGENTRELAY_TARGET") or ""),
                environ=dict(os.environ),
            )
    except (ImportError, OSError, ValueError, TypeError) as exc:
        log_event("session_binding_error", {"error": str(exc)})

    if event_name == "SessionStart":
        return handle_session_start(
            session_id=session_id
        )

    if event_name == "UserPromptSubmit":
        prompt = extract_prompt(data)
        if str(prompt or "").lstrip().startswith("<task-notification>"):
            return read_state(session_id=session_id)
        if is_informational_prompt(prompt) and not approval_controls_snapshot(
            read_state(session_id=session_id), prompt,
        ):
            state = read_state(session_id=session_id)
            # A question about an already-visible gate still needs semantic
            # review so it can be explicitly treated as a non-approval.
            if not is_gate_response_candidate(state, prompt):
                # Questions and status notifications are not a license to resume work.
                emit_hook_context(
                    "UserPromptSubmit",
                    "[AgentRelay 问答模式]\n当前只是询问或系统通知，不继承旧审计，不自动修改、测试、调用 Fusion 或开始新审计。",
                )
                return state
        pending_page_notice = ""
        pending_page_reason = pending_page_verification_reason(session_id)
        if pending_page_reason and not is_explicit_task_approval(prompt) \
                and not AUDIT_COMMAND_PATTERN.match(str(prompt or "").strip()) \
                and not is_informational_prompt(prompt):
            pending_page_notice = pending_page_reason
        settings_state = approval_settings_request(session_id, prompt)
        if settings_state is not None:
            return settings_state
        audit_state = delivery_audit_status_request(session_id, prompt)
        if audit_state is not None:
            return audit_state
        approval_controls = approval_controls_snapshot(
            read_state(session_id=session_id),
            prompt,
        )
        state = handle_user_prompt(
            prompt,
            session_id=session_id,
            workspace=data.get("cwd"),
        )
        restore_approval_controls(state, approval_controls)
        if approval_controls:
            notice = semantic_approval_notice(session_id, state, prompt)
            if pending_page_notice:
                notice = pending_page_notice + "\n" + notice
            write_state(state, session_id=session_id)
            emit_hook_context("UserPromptSubmit", notice)
            return state
        ensure_change_contract(state, prompt, data)
        contract_notice = update_change_contract_for_prompt(state, prompt, data)
        notice = update_task_gate_for_prompt(
            state,
            prompt,
        )
        if contract_notice:
            notice = contract_notice + ("\n" + notice if notice else "")
        system_message = None
        gate = state.get("task_gate")
        if isinstance(gate, dict) and gate.get("status") == "awaiting_confirmation":
            show_card, delivery_notice = card_delivery_notice(gate, gate.get("request_sha256"))
            if show_card:
                system_message = render_task_gate_card(prompt, gate)
                notice = delivery_notice
        if pending_page_notice:
            notice = pending_page_notice + ("\n" + notice if notice else "")
        write_state(state, session_id=session_id)
        emit_hook_context(
            "UserPromptSubmit",
            notice,
            system_message=system_message,
        )
        return state

    if event_name == "PreToolUse":
        tool_name = extract_tool_name(
            data
        )
        tool_input = extract_tool_input(
            data
        )
        contract_reason = change_contract_block(session_id, data, tool_name, tool_input)
        if contract_reason:
            emit_pre_tool_deny(contract_reason, system_message=render_tool_gate_card(tool_name, tool_input, contract_reason))
            return read_state(session_id=session_id)
        allowed, deny_reason = evaluate_pre_tool_gate(
            tool_name,
            tool_input,
            session_id,
        )
        if not allowed:
            quiet_gate = deny_reason.startswith(
                ("[AgentRelay 回收站]", "[AgentRelay 工作档]")
            )
            emit_pre_tool_deny(
                deny_reason,
                system_message=None if quiet_gate else render_tool_gate_card(
                    tool_name,
                    tool_input,
                    deny_reason,
                ),
            )
            return read_state(session_id=session_id)

        record_page_verification_edit(session_id, data, tool_name, tool_input, None, before=True)
        try:
            delivery_audit_before_write(session_id, data, tool_name, tool_input)
        except (ImportError, OSError, ValueError, TypeError) as exc:
            log_event("delivery_audit_error", {"error": str(exc)})
            emit_pre_tool_deny(
                "[AgentRelay 交付审计]\n"
                "无法在首次写入前建立可靠审计基线，本次写入已阻断。"
                "请修复损坏的审计状态或重新开始当前写任务；"
                "在基线恢复前不能修改工作区。"
            )
            return read_state(session_id=session_id)
        return handle_pre_tool_use(
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=extract_tool_use_id(
                data
            ),
            session_id=session_id,
            workspace=data.get("cwd"),
        )

    if event_name in ("PostToolUse", "PostToolUseFailure"):
        # PostToolUseFailure 由 Kimi Code 在工具失败时触发（shim 已把
        # error 桥接为 tool_output）；按同一入口统计失败与重试。
        tool_name = extract_tool_name(data)
        tool_input = extract_tool_input(data)
        tool_output = extract_tool_output(data)
        if event_name == "PostToolUse":
            record_page_verification_edit(session_id, data, tool_name, tool_input, tool_output)
            if tool_writes_files(tool_name, tool_input):
                try:
                    delivery_audit_after_write(session_id)
                except (ImportError, OSError, ValueError, TypeError) as exc:
                    log_event("delivery_audit_error", {"error": str(exc)})
            contract_reason = change_contract_post_tool_block(session_id, tool_name, tool_input)
            if contract_reason:
                emit_hook_context("PostToolUse", contract_reason)
        return handle_post_tool_use(
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            tool_use_id=extract_tool_use_id(
                data
            ),
            session_id=session_id,
            workspace=data.get("cwd"),
            record_write=event_name == "PostToolUse",
        )

    if event_name == "Stop":
        message = extract_last_assistant_message(data)
        reason = change_contract_completion_block(session_id, message)
        if not reason:
            reason = page_completion_block_reason(session_id, message)
        if not reason:
            reason = visual_review_block_reason(session_id, message)
        if not reason:
            reason = delivery_audit_completion_block(session_id, message)
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False), flush=True)
            return read_state(session_id=session_id)
        return handle_stop(
            session_id=session_id,
            last_assistant_message=message,
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

        <当前 Python> agent_relay_tracker.py status
        <当前 Python> agent_relay_tracker.py retry
        <当前 Python> agent_relay_tracker.py branch
        <当前 Python> agent_relay_tracker.py resolve
        <当前 Python> agent_relay_tracker.py mark-relay xxx

    Hook：

        <当前 Python> agent_relay_tracker.py

    Hook 模式从 stdin 读取 JSON。
    """

    _configure_utf8_stdio()
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
