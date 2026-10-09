#!/usr/bin/env python3
"""Agent 安装目标：注册 Hook、安装 Skill、安装后验证。

每个目标 Agent 的 Hook 机制不同：
- codex：$CODEX_HOME/hooks.json（现有流程，在 install.py 中）。
- claude：~/.claude/settings.json 的 hooks 块，stdin/stdout 协议与 Codex 兼容。
- hermes：~/.hermes/config.yaml 的 hooks 块，stdout 需要 {"context": ...}，由 shim 翻译。
- pi：~/.pi/agent/extensions/agent-relay.ts，TS 扩展调起 shim 并注入 message。
- opencode：无上下文注入事件，仅安装 Skill 供手动调用。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


class TargetError(RuntimeError):
    """某个目标的注册或验证无法安全继续时抛出。"""


HOOK_TIMEOUT = 15
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
HERMES_EVENTS = {
    "pre_llm_call": "UserPromptSubmit",
    "post_tool_call": "PostToolUse",
    "subagent_stop": "Stop",
}
KIMI_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUseFailure",
    "Stop",
)


def kimi_code_home() -> Path:
    return Path(
        os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))
    ).expanduser()


def codex_home() -> Path:
    return Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()


def agent_targets(home: Path | None = None) -> dict[str, dict]:
    """返回目标注册表；home 用于测试注入，默认按当前环境解析。"""
    base = (home or codex_home()).expanduser()
    user_home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(user_home / ".config")))
    return {
        "codex": {
            "id": "codex",
            "label": "Codex CLI",
            "detect": "codex",
            "level": "full",
            "level_note": "完整支持：Hook 计时、提醒和上下文注入",
            "skill_dir": base / "skills" / "agent-relay",
            "hook_dir": base / "hooks",
            "config_path": base / "hooks.json",
        },
        "claude": {
            "id": "claude",
            "label": "Claude Code",
            "detect": "claude",
            "level": "full",
            "level_note": "完整支持：写入 ~/.claude/settings.json 的 hooks 块",
            "skill_dir": user_home / ".claude" / "skills" / "agent-relay",
            "hook_dir": base / "hooks",
            "config_path": user_home / ".claude" / "settings.json",
        },
        "pi": {
            "id": "pi",
            "label": "Pi",
            "detect": "pi",
            "level": "full",
            "level_note": "完整支持：生成 ~/.pi/agent/extensions/agent-relay.ts",
            "skill_dir": user_home / ".pi" / "agent" / "skills" / "agent-relay",
            "hook_dir": base / "hooks",
            "config_path": user_home / ".pi" / "agent" / "extensions" / "agent-relay.ts",
        },
        "opencode": {
            "id": "opencode",
            "label": "OpenCode",
            "detect": "opencode",
            "level": "skill-only",
            "level_note": "仅安装 Skill：OpenCode 没有 prompt 提交时注入上下文的 Hook 事件，自动提醒不可用",
            "skill_dir": xdg / "opencode" / "skills" / "agent-relay",
            "hook_dir": base / "hooks",
            "config_path": None,
        },
        "hermes": {
            "id": "hermes",
            "label": "Hermes",
            "detect": "hermes",
            "level": "full",
            "level_note": "完整支持：写入 ~/.hermes/config.yaml 的 hooks 块；首次触发需在终端确认 consent",
            "skill_dir": user_home / ".hermes" / "skills" / "agent-relay",
            "hook_dir": base / "hooks",
            "config_path": user_home / ".hermes" / "config.yaml",
        },
        "kimi": {
            "id": "kimi",
            "label": "Kimi Code",
            "detect": "kimi",
            "level": "full",
            "level_note": "完整支持：写入 $KIMI_CODE_HOME/config.toml 的 [[hooks]]；提醒经 Stop 事件注入上下文，失败重试经 PostToolUseFailure 计数",
            "skill_dir": user_home / ".agents" / "skills" / "agent-relay",
            "hook_dir": base / "hooks",
            "config_path": kimi_code_home() / "config.toml",
        },
    }


UNSUPPORTED_AGENTS = (
    {
        "id": "deepseek-cli",
        "label": "DeepSeek CLI",
        "reason": "deepseek-cli 没有 Hook、插件或 Skill 机制，无法接入自动提醒；"
                  "DeepSeek 网页版请通过“线上模型”作为外援 Provider 使用。",
    },
)


def detect_agents(targets: dict[str, dict]) -> dict[str, bool]:
    return {
        target_id: bool(shutil.which(str(meta["detect"])))
        for target_id, meta in targets.items()
    }


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def backup_file(path: Path, tag: str) -> Path | None:
    if not path.is_file():
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{tag}.{timestamp}")
    shutil.copy2(path, backup)
    return backup


def find_agent_relay_hook(entries: list):
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if not isinstance(nested, list):
            continue
        for hook in nested:
            if isinstance(hook, dict) and (
                "agent_relay_hook" in str(hook.get("command", ""))
                or "agentrelay_hook_shim" in str(hook.get("command", ""))
            ):
                return hook
    return None


def build_command(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def shim_command(
    venv_python: Path,
    shim_path: Path,
    target: str,
    event: str,
) -> str:
    return build_command(
        [str(venv_python), str(shim_path), "--target", target, "--event", event]
    )


def install_managed_file(source: Path, destination: Path) -> bool:
    """原子安装文件；旧版本不一致时先备份。"""
    if not source.is_file():
        raise TargetError(f"发布包缺少必要文件：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if source.read_bytes() == destination.read_bytes():
            return False
        backup_file(destination, "before-agentrelay-update")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def install_skill_to(
    target: dict,
    project_root: Path,
    skill_files: tuple[str, ...],
    script_files: tuple[str, ...],
    config_files: tuple[str, ...],
    reference_files: tuple[str, ...],
    skill_name: str = "agent-relay",
) -> None:
    """把 Skill 目录完整装到目标 Agent 的 skills 目录。"""
    destination_root = Path(target["skill_dir"])
    for filename in skill_files:
        install_managed_file(
            project_root / "skills" / skill_name / filename,
            destination_root / filename,
        )
    for filename in (*script_files, *config_files):
        install_managed_file(
            project_root / "scripts" / filename,
            destination_root / "scripts" / filename,
        )
    for filename in reference_files:
        install_managed_file(
            project_root / "skills" / skill_name / "references" / filename,
            destination_root / "references" / filename,
        )


def register_claude(settings_path: Path, command: str) -> bool:
    """向 Claude Code 的 settings.json 合并 hooks；保留用户已有配置。"""
    if settings_path.is_file():
        try:
            config = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TargetError(
                f"无法安全合并格式无效的 {settings_path}：{exc}"
            ) from exc
        if not isinstance(config, dict):
            raise TargetError(f"{settings_path} 的根节点必须是 JSON 对象")
    else:
        config = {}

    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TargetError(f"{settings_path} 中的 hooks 必须是 JSON 对象")

    changed = False
    for event in CLAUDE_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise TargetError(f"{settings_path} 中的 hooks.{event} 必须是 JSON 数组")
        existing = find_agent_relay_hook(entries)
        if existing is not None:
            if existing.get("command") != command:
                existing.update(
                    {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
                )
                changed = True
            continue
        entries.append(
            {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}
        )
        changed = True

    if not changed:
        return False
    backup_file(settings_path, "before-agentrelay-install")
    atomic_write_json(settings_path, config)
    return True


def register_hermes_with_yaml(config_path: Path, entries: dict[str, list[dict]]) -> bool:
    """合并 ~/.hermes/config.yaml 的 hooks 块。需要在有 PyYAML 的环境运行。"""
    try:
        import yaml
    except ImportError as exc:
        raise TargetError("合并 Hermes 配置需要 PyYAML（AgentRelay 虚拟环境已安装）") from exc

    if config_path.is_file():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise TargetError(f"{config_path} 的根节点必须是 YAML 映射")
    else:
        data = {}

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TargetError(f"{config_path} 中的 hooks 必须是 YAML 映射")

    changed = False
    for event, items in entries.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise TargetError(f"{config_path} 中的 hooks.{event} 必须是 YAML 列表")
        for item in items:
            current = next(
                (x for x in existing if isinstance(x, dict)
                 and ("agentrelay_hook_shim" in str(x.get("command", ""))
                      or "--target hermes" in str(x.get("command", "")))),
                None,
            )
            if current is None:
                existing.append(item)
                changed = True
            elif current.get("command") != item["command"]:
                current.update(item)
                changed = True

    if not changed:
        return False
    backup_file(config_path, "before-agentrelay-install")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return True


def register_hermes(config_path: Path, venv_python: Path, shim_path: Path) -> bool:
    """通过虚拟环境 Python 执行 YAML 合并，避免要求系统 Python 安装 PyYAML。"""
    entries = {
        event: [{"command": shim_command(venv_python, shim_path, "hermes", canonical),
                 "timeout": HOOK_TIMEOUT}]
        for event, canonical in HERMES_EVENTS.items()
    }
    try:
        import yaml  # noqa: F401
    except ImportError:
        pass
    else:
        return register_hermes_with_yaml(config_path, entries)

    result = subprocess.run(
        [
            str(venv_python),
            str(Path(__file__).resolve()),
            "register-hermes",
            str(config_path),
            json.dumps(entries, ensure_ascii=False),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TargetError(
            f"Hermes 配置合并失败：{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip() == "changed"


PI_EXTENSION_TEMPLATE = """// AgentRelay 上下文注入扩展（由 AgentRelay 安装器生成，重新安装会覆盖此文件）
import {{ spawnSync }} from "node:child_process";

const PYTHON = {python};
const SHIM = {shim};

function relay(payload) {{
  try {{
    const res = spawnSync(PYTHON, [SHIM, "--target", "pi", "--event", "UserPromptSubmit"], {{
      input: JSON.stringify(payload),
      encoding: "utf-8",
      timeout: 12000,
    }});
    if (!res || res.status !== 0 || !res.stdout) return "";
    const out = JSON.parse(String(res.stdout).trim());
    return out?.hookSpecificOutput?.additionalContext ?? out?.additionalContext ?? "";
  }} catch {{
    return "";
  }}
}}

export default function agentRelay(pi) {{
  pi.on("before_agent_start", (event, ctx) => {{
    const content = relay({{
      hook_event_name: "UserPromptSubmit",
      session_id: ctx?.sessionId ?? ctx?.session_id ?? "",
      cwd: ctx?.cwd ?? process.cwd(),
      prompt: event?.prompt ?? event?.message ?? "",
    }});
    if (!content) return;
    return {{ message: {{ customType: "agent-relay", content, display: false }} }};
  }});
}}
"""


def _split_toml_hook_blocks(text: str) -> tuple[list[str], list[list[str]]]:
    """把 config.toml 文本拆成序言和 [[hooks]] 块（每块为行列表）。"""
    preamble: list[str] = []
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.strip() == "[[hooks]]":
            current = []
            blocks.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
    return preamble, blocks


def _parse_simple_block(lines: list[str]) -> dict[str, str] | None:
    """解析只含简单 key = value 行的块；遇到多行值等复杂结构返回 None（不碰）。"""
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            return None
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _validate_toml(text: str, path: Path) -> None:
    try:
        import tomllib
    except ImportError:
        return
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TargetError(f"{path} 不是有效 TOML：{exc}") from exc


def register_kimi(config_path: Path, venv_python: Path, shim_path: Path) -> bool:
    """向 Kimi Code 的 config.toml 追加 [[hooks]] 条目（保留用户已有配置）。"""
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    if text.strip():
        _validate_toml(text, config_path)

    preamble, blocks = _split_toml_hook_blocks(text)
    changed = False
    for event in KIMI_EVENTS:
        command = shim_command(venv_python, shim_path, "kimi", event)
        matched = False
        for block in blocks:
            values = _parse_simple_block(block)
            if not values or "agentrelay_hook_shim" not in str(values.get("command", "")):
                continue
            if values.get("event") != event:
                continue
            matched = True
            if values.get("command") == command and str(values.get("timeout")) == str(HOOK_TIMEOUT):
                continue
            for index, line in enumerate(block):
                stripped = line.strip()
                if stripped.startswith("command") and "=" in stripped:
                    block[index] = f"command = {json.dumps(command, ensure_ascii=False)}"
                elif stripped.startswith("timeout") and "=" in stripped:
                    block[index] = f"timeout = {HOOK_TIMEOUT}"
            changed = True
        if matched:
            continue
        blocks.append([
            f'event = "{event}"',
            f"command = {json.dumps(command, ensure_ascii=False)}",
            f"timeout = {HOOK_TIMEOUT}",
            "",
        ])
        changed = True

    if not changed:
        return False
    backup_file(config_path, "before-agentrelay-install")
    rebuilt = "\n".join(preamble).rstrip("\n")
    parts = [rebuilt] if rebuilt else []
    for block in blocks:
        parts.append("[[hooks]]\n" + "\n".join(block).rstrip("\n"))
    merged = "\n\n".join(part for part in parts if part) + "\n"
    _validate_toml(merged, config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(merged, encoding="utf-8")
    return True


def write_pi_extension(extension_path: Path, venv_python: Path, shim_path: Path) -> bool:
    content = PI_EXTENSION_TEMPLATE.format(
        python=json.dumps(str(venv_python)),
        shim=json.dumps(str(shim_path)),
    )
    if extension_path.is_file() and extension_path.read_text(encoding="utf-8") == content:
        return False
    backup_file(extension_path, "before-agentrelay-update")
    extension_path.parent.mkdir(parents=True, exist_ok=True)
    extension_path.write_text(content, encoding="utf-8")
    return True


def registered_in_config(target_id: str, meta: dict, venv_python: Path, shim_path: Path) -> tuple[bool, str]:
    """检查该 Agent 的配置文件里是否已注册 AgentRelay Hook。"""
    config_path = meta.get("config_path")
    if config_path is None:
        return True, "该目标无需注册 Hook 配置"
    path = Path(config_path)
    if not path.is_file():
        return False, f"配置文件不存在：{path}"
    if target_id in {"codex", "claude"}:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, f"配置文件不是有效 JSON：{exc}"
        hooks = config.get("hooks", {})
        events = CLAUDE_EVENTS if target_id == "claude" else (
            "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"
        )
        missing = [
            event for event in events
            if not isinstance(hooks.get(event), list)
            or find_agent_relay_hook(hooks[event]) is None
        ]
        if missing:
            return False, f"hooks 缺少事件：{', '.join(missing)}"
        return True, f"已在 {path} 注册 {len(events)} 个事件"
    if target_id == "hermes":
        text = path.read_text(encoding="utf-8")
        missing = [event for event in HERMES_EVENTS if event not in text]
        if "agentrelay_hook_shim" not in text or missing:
            detail = "未找到 AgentRelay shim 条目"
            if missing:
                detail += f"；缺少事件：{', '.join(missing)}"
            return False, detail
        return True, f"已在 {path} 注册 {len(HERMES_EVENTS)} 个事件"
    if target_id == "kimi":
        text = path.read_text(encoding="utf-8")
        _, blocks = _split_toml_hook_blocks(text)
        covered = set()
        for block in blocks:
            values = _parse_simple_block(block)
            if values and "agentrelay_hook_shim" in str(values.get("command", "")):
                covered.add(values.get("event"))
        missing = [event for event in KIMI_EVENTS if event not in covered]
        if missing:
            return False, f"缺少事件：{', '.join(missing)}"
        return True, f"已在 {path} 注册 {len(KIMI_EVENTS)} 个事件"
    if target_id == "pi":
        return (True, f"扩展文件已生成：{path}") if path.is_file() else (False, f"扩展文件不存在：{path}")
    return False, f"未知目标：{target_id}"


def relay_installed(target_id: str, meta: dict | None = None) -> bool:
    """轻量检查 AgentRelay 是否已接入该 Agent：Skill 就位且 Hook 已注册。

    只做文件级检查，不启动 Hook 干跑（干跑留给 verify_target）。
    """
    if meta is None:
        meta = agent_targets()[target_id]
    skill_ok = (Path(meta["skill_dir"]) / "SKILL.md").is_file()
    registered_ok, _ = registered_in_config(target_id, meta, None, None)
    return skill_ok and registered_ok


def dry_run_hook(command: list[str], payload: dict, timeout: int = 15) -> tuple[bool, str]:
    """向 Hook 命令喂一个合成事件，确认它能正常执行并退出。"""
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Hook 干跑无法执行：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, f"Hook 干跑退出码 {result.returncode}：{detail[-1] if detail else '无输出'}"
    return True, "Hook 干跑通过（合成事件正常执行并退出）"


def verify_target(
    target_id: str,
    meta: dict,
    venv_python: Path,
    main_hook: Path,
    shim_path: Path,
    tracker: Path,
    workspace: Path,
) -> list[dict]:
    """安装后验证：Skill 就位、配置已注册、Hook 干跑、Tracker 可读。"""
    checks: list[dict] = []

    skill_dir = Path(meta["skill_dir"])
    skill_ok = (skill_dir / "SKILL.md").is_file() and (
        skill_dir / "scripts" / "agent_relay.py"
    ).is_file()
    checks.append({
        "name": "Skill 文件",
        "ok": skill_ok,
        "detail": str(skill_dir),
        "reason": "缺少 SKILL.md 时该 Agent 不会加载 AgentRelay 工作流",
    })

    if meta["level"] == "skill-only":
        checks.append({
            "name": "自动提醒",
            "ok": True,
            "detail": meta["level_note"],
            "reason": "OpenCode 没有上下文注入事件，只能手动调用脚本",
        })
        return checks

    registered_ok, registered_detail = registered_in_config(
        target_id, meta, venv_python, shim_path
    )
    checks.append({
        "name": "Hook 配置",
        "ok": registered_ok,
        "detail": registered_detail,
        "reason": "缺少注册时重试计数、计时和候选提醒不会运行",
    })

    session_id = f"agentrelay-verify-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "cwd": str(workspace),
        "prompt": "AgentRelay 安装验证，无需处理",
    }
    if target_id in {"hermes", "kimi"}:
        command = [str(venv_python), str(shim_path), "--target", target_id,
                   "--event", "UserPromptSubmit"]
        probe_payload = dict(payload)
        if target_id == "hermes":
            # Hermes 真实 payload 不带事件名，验证 shim 的注入路径。
            probe_payload.pop("hook_event_name", None)
        hook_ok, hook_detail = dry_run_hook(command, probe_payload)
    else:
        command = [str(venv_python), str(main_hook)]
        hook_ok, hook_detail = dry_run_hook(command, payload)
    checks.append({
        "name": "Hook 干跑",
        "ok": hook_ok,
        "detail": hook_detail,
        "reason": "干跑失败说明 Hook 命令无法被该 Agent 正常拉起",
    })

    try:
        status = subprocess.run(
            [str(venv_python), str(tracker), "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        tracker_ok = status.returncode == 0
        tracker_detail = (
            "Tracker status 正常"
            if tracker_ok
            else f"Tracker status 退出码 {status.returncode}：{status.stderr.strip()[:120]}"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        tracker_ok, tracker_detail = False, f"Tracker 无法执行：{exc}"
    checks.append({
        "name": "Tracker 状态",
        "ok": tracker_ok,
        "detail": tracker_detail,
        "reason": "Tracker 不可读时不会累计重试次数和有效处理时间",
    })

    if target_id == "hermes":
        checks.append({
            "name": "Hermes consent",
            "ok": True,
            "detail": "Hermes 首次运行 Hook 时会在终端弹 consent 确认，请选择允许",
            "reason": "未确认的 Hook 不会被执行",
        })

    session_file = codex_home() / "agent_relay_tracker" / "sessions" / f"{session_id}.json"
    try:
        if session_file.is_file():
            session_file.unlink()
    except OSError:
        pass
    return checks


def cli_main(argv: list[str]) -> int:
    """供 install.py 通过虚拟环境 Python 调用的子命令入口。"""
    if len(argv) >= 3 and argv[0] == "register-hermes":
        config_path = Path(argv[1])
        entries = json.loads(argv[2])
        changed = register_hermes_with_yaml(config_path, entries)
        print("changed" if changed else "unchanged")
        return 0
    print("用法：agent_targets.py register-hermes <config.yaml> <entries-json>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
