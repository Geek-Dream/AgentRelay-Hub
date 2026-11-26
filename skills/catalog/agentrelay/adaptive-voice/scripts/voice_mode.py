"""Adaptive Voice 共享模式：Web、斜杠命令、自然语言与 Hook 读写同一配置。

模式与用户画像是不同概念。模式可以随用户明确要求立即切换；画像必须另行
取得明确同意才保存。本模块不推断年龄、性别、职业或性格，也不改写回答。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    from . import config_manager
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import config_manager
    except ImportError:
        config_manager = None


MODES = ("normal", "eli5", "expert", "adaptive")
DEFAULT_MODE = "normal"
LABELS = {
    "normal": "正常",
    "eli5": "5岁能懂",
    "expert": "专业",
    "adaptive": "个性化",
}
DESCRIPTIONS = {
    "normal": "保持正常回答，不额外调整表达风格。",
    "eli5": "用大白话解释，并给生活化例子；术语必须当场解释。",
    "expert": "保留准确专业术语和技术细节，面向专业人士或 Agent 沟通。",
    "adaptive": "读取用户明确同意保存的偏好，没有画像时先询问，不猜测。",
}
ALIASES = {
    "normal": "normal", "普通": "normal", "正常": "normal", "默认": "normal",
    "eli5": "eli5", "大白话": "eli5", "五岁": "eli5", "5岁": "eli5",
    "儿童": "eli5", "小孩": "eli5",
    "expert": "expert", "专业": "expert", "术语": "expert", "agent": "expert",
    "adaptive": "adaptive", "自定义": "adaptive", "个性化": "adaptive",
    "我的风格": "adaptive",
}
SLASH_PATTERN = re.compile(
    r"^/(?:adaptive-voice|voice)(?:\s+(show|clear|reset|learn|help|[a-zA-Z]+|[^\s]+))?\s*$",
    re.I,
)
INSTRUCTIONS = {
    "eli5": (
        "先给一句话结论，再用生活化比喻和具体例子解释。句子短，一句话只讲一件事；"
        "专业术语必须立即用大白话解释。不省略关键事实、风险或可执行建议。"
    ),
    "expert": (
        "保留准确专业术语、技术细节和可验证依据，高信息密度，不解释基础概念。"
        "Agent 之间可以协商符号简写表，但双方明确确认前不得使用；符号只能替代"
        "重复名词，不能替代完整结论；对方不理解时立即展开全称。"
    ),
    "adaptive": (
        "读取 Adaptive Voice 当前画像：先看用户/调用方显式指定 agent-id 的 "
        "profiles/agents/<agent-id>.md，再看 profiles/default.md；按画像表达偏好回答。"
        "没有画像时询问一次是否建立；画像未说明的维度按正常回答处理。"
        "不要从年龄、性别、职业推断性格或能力。保存或修改画像必须取得明确同意。"
    ),
}


def normalize_mode(
    value: object, *, default: str = DEFAULT_MODE, strict: bool = False,
) -> str:
    """Return a supported mode; strict rejects invalid explicit settings."""
    if value is None:
        return default
    text = str(value).strip().lower()
    if text not in MODES:
        if strict:
            raise ValueError(f"模式必须是：{', '.join(MODES)}")
        return default
    return text


def normalize_config(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Only expose the shared mode, never user profile or other private fields."""
    source = value if isinstance(value, Mapping) else {}
    return {"mode": normalize_mode(source.get("mode"))}


def _standalone_path(home: Path | None = None) -> Path:
    root = home or Path(
        os.environ.get("ADAPTIVE_VOICE_HOME", str(Path(__file__).resolve().parent))
    ).expanduser()
    return root / "voice_mode.json"


def _standalone_read(home: Path | None = None) -> str:
    try:
        value = json.loads(_standalone_path(home).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_MODE
    except (OSError, ValueError):
        return DEFAULT_MODE
    if not isinstance(value, Mapping):
        return DEFAULT_MODE
    return normalize_mode(value.get("mode"))


def _standalone_write(mode: str, home: Path | None = None) -> dict[str, str]:
    path = _standalone_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"mode": mode}, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return {"mode": _standalone_read(home)}


def _strict_shared_config(home: Path | None = None) -> dict[str, Any]:
    """Do not turn load_config's unreadable-file fallback into a destructive save."""
    if config_manager is None:
        raise ValueError("AgentRelay 配置模块不可用")
    path = config_manager.config_path(home)
    if not path.exists():
        return config_manager.default_config()
    try:
        cipher = config_manager._fernet(home)
        if cipher is None:
            raise ValueError("缺少配置解密密钥")
        value = json.loads(cipher.decrypt(path.read_bytes()).decode("utf-8"))
    except Exception as exc:
        if isinstance(exc, (
            OSError, ValueError, TypeError,
            config_manager.InvalidToken, json.JSONDecodeError,
        )):
            raise ValueError("无法读取现有配置；输出风格未修改，原配置保持不变") from exc
        raise
    if not isinstance(value, dict):
        raise ValueError("现有配置格式无效；输出风格未修改，原配置保持不变")
    return config_manager.load_config(home)


def current_mode(home: Path | None = None) -> str:
    """Read shared AgentRelay config when present, otherwise the standalone file."""
    if config_manager is None:
        return _standalone_read(home)
    return normalize_mode((config_manager.load_config(home).get("voice") or {}).get("mode"))


def set_mode(mode: str, home: Path | None = None) -> dict[str, str]:
    """Persist an explicit mode and return the value read back from disk."""
    mode = normalize_mode(mode, strict=True)
    if config_manager is None:
        return _standalone_write(mode, home)
    config = _strict_shared_config(home)
    config["voice"] = {"mode": mode}
    config_manager.save_config(config, home)
    return normalize_config(config_manager.load_config(home).get("voice"))


def status(home: Path | None = None) -> str:
    mode = current_mode(home)
    lines = [f"当前输出风格：{mode}（{LABELS[mode]}）", DESCRIPTIONS[mode], "", "可选模式："]
    lines.extend(f"- {name}：{DESCRIPTIONS[name]}" for name in MODES)
    lines.extend([
        "",
        "切换：/adaptive-voice normal|eli5|expert|adaptive",
        "也可以在对话中直接说“用大白话并举例”、“专业点”、“恢复正常输出”。",
        "个性化画像只在明确同意后保存，与当前模式切换是两件不同的事。",
    ])
    return "\n".join(lines)


def instruction(mode: str) -> str:
    """Compact text injected into the next model turn; normal adds no noise."""
    normalized = normalize_mode(mode)
    if normalized == "normal":
        return ""
    return (
        f"[Adaptive Voice · {normalized}]\n"
        f"当前输出风格是{LABELS[normalized]}：{INSTRUCTIONS[normalized]}\n"
        "只改变表达，不改变事实、结论、技术判断或安全审批；用户本条消息的明确要求优先。"
    )


def parse_command(prompt: object) -> dict[str, str] | None:
    """Parse only explicit slash commands and clear natural-language mode requests."""
    text = str(prompt or "").strip()
    match = SLASH_PATTERN.fullmatch(text)
    if match:
        argument = (match.group(1) or "show").lower()
        if argument in {"show", "help"}:
            return {"action": "show", "mode": ""}
        if argument in {"clear", "reset"}:
            return {"action": "clear", "mode": DEFAULT_MODE}
        if argument == "learn":
            return {"action": "learn", "mode": ""}
        mode = ALIASES.get(argument)
        if mode is None:
            raise ValueError("可用模式：normal、eli5、expert、adaptive；查看：/adaptive-voice")
        return {"action": "set", "mode": mode}
    # A style keyword in a writing task (e.g. “写一个专业术语文档”) is not
    # a persistent setting change. Require an explicit directive or mode phrase.
    if any(mark in text for mark in ("?", "？", "\n")) or len(text) > 100:
        return None
    # A request to produce a document/message in a certain style is a task,
    # not an instruction to persist a new default. A pure directive can add
    # examples; a noun phrase describing the deliverable cannot be swallowed.
    content_request = re.search(r"(?:专业术语|大白话|正常输出|个性化)\s*写", text)
    if (
        content_request
        or re.search(r"(?:不要|别|无需|不用)\s*(?:请)?", text)
        or re.search(r"(?:写|生成|整理|翻译|创作)\s*(?:一|个|份|篇|段|下|一下|文案|文档)", text)
    ):
        return None
    directive = r"(?:请|以后|默认|全局)?(?:直接)?(?:用|换成|切成|切到|切换成|切换到|改为|改成|设为|设置为|开启|打开|恢复)(?:为|到|成|一下)?\s*"
    prefix = r"(?:adaptive[- ]voice\s*)?(?:(?:输出|回复|回答)\s*)?(?:(?:模式|风格|方式)\s*)?"
    choices = {
        "eli5": r"(?:大白话|说人话|5岁(?:小孩)?(?:能懂|能听懂)?|五岁(?:小孩)?(?:能懂|能听懂)?)",
        "expert": r"(?:专业|技术术语|expert)",
        "normal": r"(?:正常输出|普通输出|正常|普通|默认风格|normal)",
        "adaptive": r"(?:个性化|自定义风格|自适应风格|我的风格|adaptive)",
    }
    for mode, choice in choices.items():
        if re.search(directive + prefix + choice + r"(?:\s*(?:模式|风格|方式))?", text, re.I):
            return {"action": "set", "mode": mode}
    if re.fullmatch(r"专业点(?:\s*[,，]\s*用术语给\s*agent\s*看)?", text, re.I):
        return {"action": "set", "mode": "expert"}
    return None


def apply_command(request: Mapping[str, str], home: Path | None = None) -> dict[str, Any]:
    """Apply an explicit user request. Never treats a mode switch as profile consent."""
    action = request.get("action")
    if action == "set":
        saved = set_mode(str(request["mode"]), home)
        mode = saved["mode"]
        return {
            "persisted": True, "mode": mode,
            "message": f"[Adaptive Voice] 输出风格已经保存为 {mode}（{LABELS[mode]}），后续对话生效。\n{DESCRIPTIONS[mode]}",
        }
    if action == "clear":
        set_mode(DEFAULT_MODE, home)
        return {"persisted": True, "mode": DEFAULT_MODE, "message": "[Adaptive Voice] 已恢复正常输出。"}
    if action == "learn":
        return {
            "persisted": False, "mode": current_mode(home),
            "message": "[Adaptive Voice] 保存画像需要你明确提供内容并同意写入位置；模式切换不会自动保存个人信息。",
        }
    return {"persisted": False, "mode": current_mode(home), "message": status(home)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adaptive Voice 输出风格切换")
    parser.add_argument("action", choices=("show", "set", "clear", "learn"))
    parser.add_argument("mode", nargs="?", choices=MODES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "set" and not args.mode:
        parser.error("set 需要指定模式")
    request = {"action": args.action, "mode": args.mode or ""}
    result = apply_command(request)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
