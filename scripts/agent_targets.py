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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from collections.abc import Iterable
from pathlib import Path

try:
    from . import approval_policy
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import approval_policy


class TargetError(RuntimeError):
    """某个目标的注册或验证无法安全继续时抛出。"""


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


HOOK_TIMEOUT = 15
PYTHON_UTF8_ARGS = ("-X", "utf8")
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
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
            "skill_dir": kimi_code_home() / "skills" / "agent-relay",
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
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
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
    event: str = "",
) -> str:
    parts = [
        str(venv_python),
        *PYTHON_UTF8_ARGS,
        str(shim_path),
        "--target",
        target,
    ]
    if event:
        parts.extend(["--event", event])
    return build_command(parts)


def command_has_target(command: str, target: str) -> bool:
    """Return whether a registered command explicitly selects the target host."""
    text = str(command).replace('"', "").replace("'", "").replace(",", " ")
    return bool(re.search(
        rf"--target(?:=|\s+){re.escape(target)}(?=$|[\s,\]}}])",
        text,
    ))


def command_uses_shim_target(command: str, target: str) -> bool:
    text = str(command)
    return "agentrelay_hook_shim" in text and command_has_target(text, target)


def install_managed_file(source: Path, destination: Path) -> bool:
    """原子安装文件；旧版本不一致时先备份。"""
    if source.is_symlink():
        raise TargetError(f"拒绝安装符号链接文件：{source}")
    if not source.is_file():
        raise TargetError(f"发布包缺少必要文件：{source}")
    if destination.is_symlink():
        raise TargetError(f"拒绝覆盖符号链接目标：{destination}")
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


def _regular_tree_files(root: Path, label: str) -> list[Path]:
    """Return regular files from a tree while rejecting links and special files."""

    if root.is_symlink():
        raise TargetError(f"{label}不能是符号链接：{root}")
    if not root.exists():
        raise TargetError(f"{label}不存在：{root}")
    if not root.is_dir():
        raise TargetError(f"{label}不是目录：{root}")

    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise TargetError(f"{label}包含符号链接：{path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise TargetError(f"{label}包含非普通文件：{path}")
    return files


def install_skill_tree_to(
    source: Path,
    destination: Path,
    extra_files: Path | Iterable[Path] | None = None,
) -> dict[str, int]:
    """Recursively install a Skill tree without following links.

    Existing files are backed up before replacement. Extra files keep their own
    basename, which lets catalog installs attach a shared LICENSE to each Skill.
    """

    source = Path(source)
    destination = Path(destination)
    source_files = _regular_tree_files(source, "Skill 源目录")

    if destination.is_symlink():
        raise TargetError(f"拒绝安装到符号链接目标：{destination}")
    if destination.exists() and not destination.is_dir():
        raise TargetError(f"Skill 安装目标不是目录：{destination}")
    if destination.exists():
        _regular_tree_files(destination, "Skill 安装目标")

    if destination.exists() and destination.resolve() == source.resolve():
        return {"installed": 0, "files": len(source_files)}

    try:
        destination.resolve().relative_to(source.resolve())
    except ValueError:
        pass
    else:
        raise TargetError("Skill 安装目标不能位于源目录内部")

    if extra_files is None:
        extras: list[Path] = []
    elif isinstance(extra_files, (str, Path)):
        extras = [Path(extra_files)]
    else:
        extras = [Path(path) for path in extra_files]

    planned: list[tuple[Path, Path]] = []
    seen_destinations: set[Path] = set()
    for path in source_files:
        relative = path.relative_to(source)
        target_path = destination / relative
        if target_path in seen_destinations:
            raise TargetError(f"Skill 安装计划包含重复文件：{target_path}")
        seen_destinations.add(target_path)
        planned.append((path, target_path))

    for path in extras:
        if path.is_symlink():
            raise TargetError(f"附加文件不能是符号链接：{path}")
        if not path.is_file():
            raise TargetError(f"附加文件不存在：{path}")
        target_path = destination / path.name
        if target_path in seen_destinations:
            raise TargetError(f"附加文件与 Skill 内文件重名：{path.name}")
        seen_destinations.add(target_path)
        planned.append((path, target_path))

    installed = 0
    for source_path, destination_path in planned:
        if install_managed_file(source_path, destination_path):
            installed += 1
    return {"installed": installed, "files": len(planned)}


def replace_skill_tree_to(source: Path, destination: Path) -> dict[str, int]:
    """Atomically replace a downloaded Skill tree and retain the old tree."""

    source = Path(source)
    destination = Path(destination)
    source_files = _regular_tree_files(source, "Skill 源目录")
    if destination.is_symlink():
        raise TargetError(f"拒绝安装到符号链接目标：{destination}")
    if destination.exists() and not destination.is_dir():
        raise TargetError(f"Skill 安装目标不是目录：{destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.agentrelay-staging.",
        dir=destination.parent,
    ))
    staging_tree = staging / destination.name
    backup: Path | None = None
    try:
        install_skill_tree_to(source, staging_tree)
        staged_count, staged_digest = _tree_sha256_tree(staging_tree)
        source_count, source_digest = _tree_sha256_tree(source)
        if (
            staged_count != source_count
            or staged_digest != source_digest
        ):
            raise TargetError("下载 Skill 的暂存内容校验失败")

        if destination.exists():
            _regular_tree_files(destination, "Skill 安装目标")
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = destination.with_name(
                f"{destination.name}.before-agentrelay-update.{timestamp}"
            )
            os.replace(destination, backup)
        try:
            os.replace(staging_tree, destination)
        except OSError:
            if backup is not None and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {"installed": len(source_files), "files": len(source_files)}


def _tree_sha256_tree(root: Path) -> tuple[int, str]:
    import hashlib

    files = _regular_tree_files(root, "Skill 目录")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return len(files), digest.hexdigest()


def install_catalog_skill_to(
    target: dict,
    project_root: Path,
    skill_id: str,
    catalog: dict | None = None,
    archive_source: Path | None = None,
) -> Path:
    """Install one bundled, downloaded, or locally discovered Skill."""

    try:
        from . import skill_catalog
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import skill_catalog

    try:
        catalog = catalog or skill_catalog.load_catalog(project_root)
        entry = skill_catalog.catalog_skill_entry(catalog, skill_id)
        if not skill_catalog.skill_is_installable(entry):
            reason = str(entry.get("restriction") or "")
            detail = f"：{reason}" if reason else ""
            raise skill_catalog.CatalogError(
                f"Skill cannot be installed: {skill_id}{detail}"
            )
        destination = Path(target["skill_dir"]).parent / skill_id
        if entry.get("install_method") == skill_catalog.OFFICIAL_DOWNLOAD_METHOD:
            with skill_catalog.official_skill_source(
                skill_id, archive_source=archive_source
            ) as source:
                replace_skill_tree_to(source, destination)
            return destination
        source = skill_catalog.skill_source_dir(project_root, catalog, skill_id)
        extra_files = skill_catalog.skill_extra_files(
            project_root, catalog, skill_id
        )
    except skill_catalog.CatalogError as exc:
        raise TargetError(str(exc)) from exc
    install_skill_tree_to(
        source,
        destination,
        extra_files,
    )
    return destination


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
            *PYTHON_UTF8_ARGS,
            str(Path(__file__).resolve()),
            "register-hermes",
            str(config_path),
            json.dumps(entries, ensure_ascii=False),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise TargetError(
            f"Hermes 配置合并失败：{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip() == "changed"


PI_EXTENSION_TEMPLATE = """// AgentRelay 上下文注入扩展（由 AgentRelay 安装器生成，重新安装会覆盖此文件）
import {{ execFile, spawnSync }} from "node:child_process";
import {{ existsSync }} from "node:fs";
import {{ homedir }} from "node:os";
import {{ dirname, join }} from "node:path";
import {{ fileURLToPath }} from "node:url";
import {{ Type }} from "typebox";

const IS_WIN = process.platform === "win32";
const HOME = homedir();
const CODEX_HOME = process.env.CODEX_HOME || join(HOME, ".codex");
const FUSION_TOOL_NAME = "agent_fusion_delegate";
const VENV = join(CODEX_HOME, "agentrelay-env");
const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));
const PI_AGENT_DIR = dirname(EXTENSION_DIR);
const PI_SKILLS = join(PI_AGENT_DIR, "skills");
const HOME_PI_SKILLS = join(HOME, ".pi", "agent", "skills");
const BACKSLASH = String.fromCharCode(92);

function firstExisting(candidates) {{
  return candidates.find((candidate) => {{
    try {{
      return Boolean(candidate && existsSync(candidate));
    }} catch {{
      return false;
    }}
  }}) ?? "";
}}

const SHIM = firstExisting([
  process.env.AGENTRELAY_SHIM,
  join(PI_SKILLS, "agent-relay", "scripts", "agentrelay_hook_shim.py"),
  join(HOME_PI_SKILLS, "agent-relay", "scripts", "agentrelay_hook_shim.py"),
  join(CODEX_HOME, "skills", "agent-relay", "scripts", "agentrelay_hook_shim.py"),
]);
const FUSION = firstExisting([
  process.env.AGENTRELAY_FUSION,
  join(PI_SKILLS, "agent-fusion", "scripts", "agent_fusion.py"),
  join(HOME_PI_SKILLS, "agent-fusion", "scripts", "agent_fusion.py"),
  join(CODEX_HOME, "skills", "agent-fusion", "scripts", "agent_fusion.py"),
]);
const APPROVAL = firstExisting([
  process.env.AGENTRELAY_APPROVAL,
  join(PI_SKILLS, "agent-relay", "scripts", "approval_policy.py"),
  join(HOME_PI_SKILLS, "agent-relay", "scripts", "approval_policy.py"),
  join(CODEX_HOME, "skills", "agent-relay", "scripts", "approval_policy.py"),
]);
const VENV_PYTHONS = IS_WIN
  ? [
      join(VENV, "Scripts", "python.exe"),
      join(VENV, "bin", "python"),
    ]
  : [
      join(VENV, "bin", "python"),
      join(VENV, "Scripts", "python.exe"),
    ];
const PYTHON_CANDIDATES = [
  process.env.AGENTRELAY_PYTHON,
  ...VENV_PYTHONS,
  IS_WIN ? "py" : "python3",
  IS_WIN ? "python" : "python",
].filter(Boolean);
const PYTHON_ARGS = ["-X", "utf8"];

const fusionDelegateSchema = Type.Object({{
  task: Type.String({{
    description: "Self-contained unit of work: what to do, and where.",
  }}),
  context: Type.Optional(Type.String({{
    description: "Plan, constraints, and decisions the sidekick cannot see.",
  }})),
  expect: Type.Optional(Type.String({{
    description: "What to report back.",
  }})),
}});

let resolvedPython;

function pythonCommand() {{
  if (resolvedPython !== undefined) return resolvedPython;
  for (const candidate of PYTHON_CANDIDATES) {{
    const looksLikePath = candidate.includes("/")
      || candidate.includes(BACKSLASH)
      || candidate.includes(":");
    if (looksLikePath && !existsSync(candidate)) continue;
    try {{
      const probe = spawnSync(candidate, [...PYTHON_ARGS, "--version"], {{
        encoding: "utf-8",
        timeout: 2000,
      }});
      if (!probe?.error && probe?.status === 0) {{
        resolvedPython = candidate;
        return candidate;
      }}
    }} catch {{
      // Try the next interpreter instead of blocking the host Agent.
    }}
  }}
  resolvedPython = null;
  return null;
}}

function runPython(script, args, options) {{
  if (!script || !existsSync(script)) return null;
  const python = pythonCommand();
  if (!python) return null;
  try {{
    return spawnSync(python, [...PYTHON_ARGS, script, ...args], options);
  }} catch {{
    return null;
  }}
}}

function relay(payload) {{
  let rawPayload;
  try {{
    rawPayload = JSON.stringify(payload);
  }} catch {{
    return {{ context: "", card: "", denial: "", stopBlock: "" }};
  }}
  const res = runPython(
    SHIM,
    ["--target", "pi", "--event", payload.hook_event_name || "UserPromptSubmit"],
    {{
      input: rawPayload,
      encoding: "utf-8",
      timeout: 12000,
    }},
  );
  if (!res || res.status !== 0 || !res.stdout) {{
    return {{ context: "", card: "", denial: "", stopBlock: "" }};
  }}
  try {{
    const out = JSON.parse(String(res.stdout).trim());
    return {{
      context: out?.hookSpecificOutput?.additionalContext ?? out?.additionalContext ?? "",
      card: out?.systemMessage ?? "",
      denial: out?.hookSpecificOutput?.permissionDecision === "deny"
        ? String(out?.hookSpecificOutput?.permissionDecisionReason || out?.systemMessage || "操作未获批准")
        : "",
      stopBlock: out?.decision === "block"
        ? String(out?.reason || out?.systemMessage || "任务尚未完成")
        : "",
    }};
  }} catch {{
    return {{ context: "", card: "", denial: "", stopBlock: "" }};
  }}
}}

function fusion(args) {{
  if (!FUSION) {{
    return {{ result: null, status: null, error: "Agent Fusion 文件未安装" }};
  }}
  const res = runPython(FUSION, args, {{
    encoding: "utf-8",
    timeout: 5000,
  }});
  if (!res) {{
    return {{ result: null, status: null, error: "未找到可用的 AgentRelay Python" }};
  }}
  return {{
    result: parseFusionOutput(res?.stdout),
    status: res?.status ?? null,
    error: String(res?.stderr ?? "").trim(),
  }};
}}

function approval(args) {{
  const res = runPython(APPROVAL, args, {{ encoding: "utf-8", timeout: 5000 }});
  return {{
    stdout: String(res?.stdout ?? "").trim(),
    stderr: String(res?.stderr ?? res?.error?.message ?? "").trim(),
    status: res?.status ?? null,
  }};
}}

function approvalResult(outcome) {{
  try {{
    return JSON.parse(outcome.stdout);
  }} catch {{
    return null;
  }}
}}

const APPROVAL_MENU_MODES = [
  {{ id: "1", mode: "strict", label: "严格", description: "写入先确认；完整需求/diff/行为/视觉/主审/独立审计通过才完成" }},
  {{ id: "2", mode: "standard", label: "标准（默认）", description: "小改轻量检查；单点样式只验目标/边界，复杂/共享/高风险自动升级" }},
  {{ id: "3", mode: "relaxed", label: "宽松", description: "小任务不做正式审计；大型/多文件/多目标/共享/高风险才审计" }},
];

function approvalMenuText(item) {{
  return item.id + ". " + item.label + " - " + item.description;
}}

async function approvalMenu(ctx) {{
  const selected = await ctx.ui.select(
    "Agent Approval 权限设置\\n选择 1、2、3；取消不会修改配置。切换模式不会批准当前待确认任务。",
    APPROVAL_MENU_MODES.map(approvalMenuText),
  );
  const item = APPROVAL_MENU_MODES.find((entry) => approvalMenuText(entry) === selected);
  if (!item) return;
  await approvalCommand(item.id, ctx);
}}

async function approvalCommand(args, ctx) {{
  const value = String(args || "").trim();
  if (!value) {{
    if (!ctx.hasUI) {{
      ctx.ui.notify("非交互模式请使用：/agent-approval [1|2|3]", "warning");
      return;
    }}
    await approvalMenu(ctx);
    return;
  }}
  if (!/^[1-3]$/.test(value)) {{
    ctx.ui.notify("权限模式请输入 1、2、3：1=严格，2=标准（默认），3=宽松", "warning");
    return;
  }}
  const outcome = approval([value]);
  const result = approvalResult(outcome);
  if (outcome.status !== 0 || !result?.mode) {{
    ctx.ui.notify("权限设置保存失败：" + (outcome.stderr || "无法读取设置结果"), "error");
    return;
  }}
  const verify = approvalResult(approval(["show"]));
  if (verify?.mode !== result.mode) {{
    ctx.ui.notify("权限设置回读不一致，请检查配置目录写入权限", "error");
    return;
  }}
  const item = APPROVAL_MENU_MODES.find((entry) => entry.mode === result.mode);
  ctx.ui.notify(
    "Agent Approval 已保存并回读验证\\n"
      + item.id + ". " + item.label + "：" + item.description + "\\n"
      + "配置：" + result.path + "\\n"
      + "当前待确认任务仍需单独批准；危险命令和越界门禁不变。",
    "info",
  );
}}

function parseFusionOutput(output) {{
  const stdout = String(output ?? "").trim();
  const line = stdout.split(/\\r?\\n/).find((item) => item.startsWith("AGENT_FUSION_RESULT="));
  let result = null;
  if (line) {{
    try {{
      result = JSON.parse(line.slice("AGENT_FUSION_RESULT=".length));
    }} catch {{
      result = null;
    }}
  }}
  return result;
}}

function runPythonAsync(script, args, options = {{}}) {{
  return new Promise((resolve) => {{
    if (!script || !existsSync(script)) {{
      resolve({{ result: null, status: null, error: "Agent Fusion 文件未安装" }});
      return;
    }}
    const python = pythonCommand();
    if (!python) {{
      resolve({{ result: null, status: null, error: "未找到可用的 AgentRelay Python" }});
      return;
    }}
    try {{
      execFile(
        python,
        [...PYTHON_ARGS, script, ...args],
        {{
          encoding: "utf-8",
          maxBuffer: 32 * 1024 * 1024,
          signal: options.signal,
        }},
        (error, stdout, stderr) => {{
          const status = typeof error?.code === "number" ? error.code : (error ? 1 : 0);
          resolve({{
            result: parseFusionOutput(stdout),
            status,
            error: String(stderr || error?.message || "").trim(),
            cancelled: error?.name === "AbortError",
          }});
        }},
      );
    }} catch (error) {{
      resolve({{
        result: null,
        status: 1,
        error: String(error?.message || error),
      }});
    }}
  }});
}}

async function fusionAsync(args, signal) {{
  if (!FUSION) {{
    return {{ result: null, status: null, error: "Agent Fusion 文件未安装" }};
  }}
  return runPythonAsync(FUSION, args, {{ signal }});
}}

function fusionStatus() {{
  return fusion(["status", "--json"]).result?.state ?? null;
}}

function syncFusionTool(pi, state) {{
  try {{
    const active = pi.getActiveTools();
    const present = active.includes(FUSION_TOOL_NAME);
    const available = Boolean(state?.delegable && FUSION && pythonCommand());
    if (available && !present) {{
      pi.setActiveTools([...active, FUSION_TOOL_NAME]);
    }} else if (!available && present) {{
      pi.setActiveTools(active.filter((tool) => tool !== FUSION_TOOL_NAME));
    }}
  }} catch {{
    // Extension tools may not be bound yet during initial loading.
  }}
}}

function fusionGuidance(state) {{
  if (!state?.delegable) return "";
  const toolMode = String(state.tool_mode || "readonly");
  return [
    "You are the main agent in a two-agent harness. `agent_fusion_delegate` hands work to AgentRelay Fusion, a persistent sidekick with its own tools and context. It cannot see this conversation, so put what it needs in `context`.",
    "Take minimal actions yourself. Default to delegating and monitoring, and keep the decisions that matter: the plan, the interpretation of ambiguity, and the final review.",
    "Use the sidekick before repeating bulky or isolated execution work locally: a test suite, a wide search, batch reading, a mechanical sweep, independent reproduction or verification, or a well-scoped implementation.",
    "When the deliverable is the judgment itself, do the work yourself.",
    "Delegate by how much output the work produces, not how many steps it takes: one command printing hundreds of lines belongs with the sidekick; a lookup answered in a line or two does not.",
    toolMode === "coding"
      ? "The sidekick edits files and runs commands unattended; review its changes and verification before accepting them."
      : "The sidekick is read-only: it investigates and verifies, while you apply every change.",
    "Continue local work if Fusion is unavailable or fails; do not block the main task.",
  ].join("\\n");
}}

function contextSessionId(ctx) {{
  const managed = typeof ctx?.sessionManager?.getSessionId === "function"
    ? ctx.sessionManager.getSessionId()
    : "";
  return String(
    managed
      || ctx?.sessionId
      || ctx?.session_id
      || process.env.PI_SESSION_ID
      || ""
  ).trim();
}}

function assistantText(message) {{
  const content = message?.content;
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .filter((part) => part?.type === "text" && typeof part?.text === "string")
    .map((part) => part.text)
    .join("")
    .trim();
}}

function lastAssistantText(messages) {{
  if (!Array.isArray(messages)) return "";
  for (let index = messages.length - 1; index >= 0; index -= 1) {{
    const message = messages[index];
    if (message?.role !== "assistant") continue;
    const text = assistantText(message);
    if (text) return text;
  }}
  return "";
}}

function fusionDelegateContext(params) {{
  const context = String(params?.context || "").trim();
  const expect = String(params?.expect || "").trim();
  if (!expect) return context;
  const expected = "Expected return:\\n" + expect;
  return context ? context + "\\n\\n" + expected : expected;
}}

function fusionEvidence(result) {{
  const parts = ["Fusion evidence"];
  if (result?.session_id) parts.push("session=" + String(result.session_id));
  if (result?.tool_mode) parts.push("mode=" + String(result.tool_mode));
  if (result?.steps !== undefined) parts.push("steps=" + String(result.steps));
  if (Array.isArray(result?.tools_used)) {{
    parts.push("tools_used=" + String(result.tools_used.length));
  }}
  if (result?.handoff_path) parts.push("handoff=" + String(result.handoff_path));
  return parts.join("; ");
}}

const FUSION_REASONS = {{
  FUSION_DISABLED: "Fusion 已关闭",
  FUSION_PROVIDER_NOT_CONFIGURED: "尚未选择 Provider",
  FUSION_PROVIDER_NOT_FOUND: "配置的 Provider 已不存在",
  FUSION_PROVIDER_DISABLED: "配置的 Provider 已停用",
  FUSION_PROVIDER_NOT_READY: "Provider 缺少必要地址或模型名称",
  FUSION_PROVIDER_AMBIGUOUS: "Provider ID 同时匹配多个类型，请使用类型前缀",
  COMMANDER_ACTIVE: "Commander 正在运行，拥有最高优先级并强制停用 Fusion",
  COMMANDER_CHILD: "当前是 Commander 子 Agent，禁止调用 Fusion",
  FUSION_CHILD: "当前是 Fusion 子会话，禁止递归调用 Fusion 或 Commander",
  FUSION_TOOL_MODE_UNSUPPORTED: "网页 Provider 不支持 coding 工具模式，请改为 readonly",
  FUSION_BUSY: "该 Fusion 会话已有任务正在运行",
}};

function fusionReasonText(state) {{
  const reason = String(state?.reason || "");
  if (!reason || reason === "READY") return "就绪";
  return FUSION_REASONS[reason] || ("状态码 " + reason);
}}

function describeFusion(result) {{
  if (!result) return "Agent Fusion 状态不可读";
  if (result.status !== "success") return String(result.message || result.error || "Agent Fusion 操作失败");
  const state = result.state || {{}};
  const provider = state.provider?.ref || state.provider_id || "未配置";
  const status = state.delegable ? "可调用" : "不可调用";
  return "Agent Fusion：" + status
    + "；开关偏好 " + (state.enabled ? "开启" : "关闭")
    + "；运行态 " + (state.effective_enabled ? "开启" : "关闭")
    + "；工具模式 " + String(state.tool_mode || "readonly")
    + "；最多 " + String(state.max_steps || 8) + " 步"
    + "；Provider " + provider
    + "；说明：" + fusionReasonText(state);
}}

function fusionMenuText(item) {{
  return item.label + " - " + item.description;
}}

function fusionMenuChoice(items, selected) {{
  return items.find((item) => fusionMenuText(item) === selected);
}}

function fusionProviderDescription(kind) {{
  if (kind === "web") return "网页 Provider，沿用现有登录会话，不切换默认网站";
  if (kind === "local") return "本地模型 Provider，只调用本机配置，不访问云端";
  if (kind === "api") return "API Provider，使用已配置的接口、协议和模型";
  return "已配置的外部模型 Provider";
}}

async function fusionProviderMenu(ctx) {{
  const state = fusionStatus();
  if (!state) {{
    ctx.ui.notify("无法读取 Agent Fusion 状态，请检查 AgentRelay 安装", "error");
    return;
  }}
  const current = String(state.provider_id || "");
  const providers = Array.isArray(state.available_providers) ? state.available_providers : [];
  const items = providers
    .filter((provider) => provider && provider.ref)
    .map((provider) => ({{
      id: String(provider.ref),
      label: String(provider.ref) + (String(provider.ref) === current ? "（当前）" : ""),
      description: fusionProviderDescription(String(provider.kind || "")),
    }}));
  items.push({{
    id: "",
    label: "返回",
    description: "不修改当前 Provider",
  }});
  const selected = await ctx.ui.select(
    providers.length > 0
      ? "选择 Agent Fusion Provider（只列出本机已配置项，不探测登录）"
      : "尚未配置 Provider，请先在 AgentRelay 配置中心添加网页、本地或 API Provider",
    items.map(fusionMenuText),
  );
  const item = fusionMenuChoice(items, selected);
  if (!item || !item.id) return;
  const outcome = fusion(["provider", item.id]);
  const level = outcome.result?.status === "success" ? "info" : "error";
  ctx.ui.notify(
    describeFusion(outcome.result || {{ status: "failed", error: outcome.error }}),
    level,
  );
}}

async function confirmFusionCoding(ctx) {{
  const choices = [
    "确认启用 coding - 允许 Fusion 修改工作区文件并执行命令",
    "取消 - 保持当前工具模式",
  ];
  const selected = await ctx.ui.select(
    "确定要启用 coding 模式吗？\\n"
      + "coding 模式会修改文件和执行命令，风险明显高于 readonly。",
    choices,
  );
  return selected === choices[0];
}}

async function fusionMenu(ctx) {{
  const state = fusionStatus();
  const current = state
    ? "当前：开关" + (state.enabled ? "开启" : "关闭")
      + "；工具模式 " + String(state.tool_mode || "readonly")
      + "；最多 " + String(state.max_steps || 8) + " 步"
      + "；" + (state.delegable ? "可调用" : fusionReasonText(state))
    : "当前：状态不可读";
  const items = [
    {{
      id: "status",
      label: "查看当前状态",
      description: "检查开关、Commander 最高优先级互斥和 Provider 是否可用",
    }},
    {{
      id: "on",
      label: "开启 Fusion",
      description: "保存开启偏好；Commander 运行期间仍会强制停用",
    }},
    {{
      id: "off",
      label: "关闭 Fusion",
      description: "保存关闭偏好；普通任务不再调用外部模型",
    }},
    {{
      id: "provider",
      label: "选择 Provider",
      description: "从本机已配置的网页、本地或 API Provider 中选择独立会话使用的模型",
    }},
    {{
      id: "mode-readonly",
      label: "切换为 readonly",
      description: "允许读取、列目录和搜索，不允许写文件或执行命令，适合默认安全协作",
    }},
    {{
      id: "mode-coding",
      label: "切换为 coding",
      description: "允许修改工作区文件并执行 shell 命令，启用前会再次要求确认风险",
    }},
    {{
      id: "reset-session",
      label: "重置当前会话",
      description: "清除当前工作区和会话 ID 对应的持久上下文，下一次从空白 Fusion 会话开始",
    }},
    {{
      id: "clear-provider",
      label: "清除 Provider",
      description: "保留 Fusion 开关，但立即停止外部模型调用",
    }},
  ];
  const selected = await ctx.ui.select("Agent Fusion 设置\\n" + current, items.map(fusionMenuText));
  const item = fusionMenuChoice(items, selected);
  if (!item) return;
  if (item.id === "provider") {{
    await fusionProviderMenu(ctx);
    return;
  }}
  if (item.id === "mode-coding") {{
    if (!await confirmFusionCoding(ctx)) return;
  }}
  let command = null;
  if (item.id === "clear-provider") {{
    command = ["provider", "none"];
  }} else if (item.id === "mode-readonly") {{
    command = ["mode", "readonly"];
  }} else if (item.id === "mode-coding") {{
    command = ["mode", "coding", "--confirm-coding"];
  }} else if (item.id === "reset-session") {{
    const workspace = ctx?.cwd ?? process.cwd();
    const sessionId = ctx?.sessionId ?? ctx?.session_id ?? "";
    command = ["reset", "--workspace", workspace];
    if (sessionId) command.push("--session-id", sessionId);
  }} else {{
    command = [item.id];
  }}
  const outcome = fusion(command);
  const level = outcome.result?.status === "success" ? "info" : "error";
  ctx.ui.notify(
    describeFusion(outcome.result || {{ status: "failed", error: outcome.error }}),
    level,
  );
}}

async function fusionCommand(args, ctx) {{
  const parts = String(args || "").trim().split(/\\s+/).filter(Boolean);
  if (parts.length === 0) {{
    if (!ctx.hasUI) {{
      ctx.ui.notify(
        "非交互模式请使用：/agent-fusion [status|on|off|provider <ref|none>|mode <readonly|coding>|reset]",
        "warning",
      );
      return;
    }}
    await fusionMenu(ctx);
    return;
  }}
  let command = null;
  if (parts.length === 1 && parts[0] === "status") {{
    command = ["status", "--json"];
  }} else if (parts.length === 1 && (parts[0] === "on" || parts[0] === "off")) {{
    command = [parts[0]];
  }} else if (parts[0] === "provider" && parts.length === 2) {{
    command = ["provider", parts[1]];
  }} else if (parts.length === 2 && parts[0] === "mode"
             && (parts[1] === "readonly" || parts[1] === "coding")) {{
    if (parts[1] === "coding") {{
      if (!ctx.hasUI) {{
        ctx.ui.notify(
          "非交互启用 coding 请使用：/agent-fusion mode coding --confirm-coding",
          "warning",
        );
        return;
      }}
      if (!await confirmFusionCoding(ctx)) return;
      command = ["mode", "coding", "--confirm-coding"];
    }} else {{
      command = ["mode", "readonly"];
    }}
  }} else if (parts.length === 3 && parts[0] === "mode"
             && parts[1] === "coding" && parts[2] === "--confirm-coding") {{
    command = ["mode", "coding", "--confirm-coding"];
  }} else if (parts.length === 1 && parts[0] === "reset") {{
    command = ["reset", "--workspace", ctx?.cwd ?? process.cwd()];
    const sessionId = ctx?.sessionId ?? ctx?.session_id ?? "";
    if (sessionId) command.push("--session-id", sessionId);
  }}
  if (!command) {{
    ctx.ui.notify(
      "用法：/agent-fusion [status|on|off|provider <ref|none>|mode <readonly|coding>|reset]",
      "warning",
    );
    return;
  }}
  const outcome = fusion(command);
  const level = outcome.result?.status === "success" ? "info" : "error";
  ctx.ui.notify(
    describeFusion(outcome.result || {{ status: "failed", error: outcome.error }}),
    level,
  );
}}

export default function agentRelay(pi) {{
  let lastReplyText = "";
  let finalStopEmitted = false;
  let agentSettledSupported = false;
  let pendingAgentEndCheck = null;
  let pendingAgentEndContext = null;

  function clearPendingAgentEndCheck() {{
    if (pendingAgentEndCheck !== null) clearTimeout(pendingAgentEndCheck);
    pendingAgentEndCheck = null;
    pendingAgentEndContext = null;
  }}

  function emitFinalStop(ctx, event) {{
    if (finalStopEmitted) return;
    finalStopEmitted = true;
    clearPendingAgentEndCheck();
    const message = lastAssistantText(event?.messages) || lastReplyText;
    const result = relay({{
      hook_event_name: "Stop",
      session_id: contextSessionId(ctx),
      cwd: ctx?.cwd ?? process.cwd(),
      last_assistant_message: message,
    }});
    const notice = result.card || result.stopBlock;
    if (notice) {{
      ctx?.ui?.notify(notice, result.stopBlock ? "warning" : "info");
    }}
  }}

  function checkPendingAgentEnd() {{
    pendingAgentEndCheck = null;
    const ctx = pendingAgentEndContext;
    if (!ctx || finalStopEmitted) return;
    try {{
      if (ctx.isIdle() === true) {{
        pendingAgentEndContext = null;
        emitFinalStop(ctx);
        return;
      }}
    }} catch {{
      pendingAgentEndContext = null;
      return;
    }}
    pendingAgentEndCheck = setTimeout(checkPendingAgentEnd, 25);
    if (typeof pendingAgentEndCheck.unref === "function") {{
      pendingAgentEndCheck.unref();
    }}
  }}

  pi.registerTool({{
    name: FUSION_TOOL_NAME,
    label: "Delegate to AgentRelay Fusion",
    description:
      "Hand a unit of execution work to AgentRelay Fusion, a persistent sidekick with its own tools and context.",
    promptSnippet: "Delegate slow, bulky, or isolated execution work to AgentRelay Fusion.",
    promptGuidelines: [
      "Default to delegating and monitoring bulky or repetitive work: test suites, wide searches, batch reading, mechanical sweeps, independent reproduction or verification, and well-scoped implementations.",
      "Keep the plan, interpretation of ambiguity, and final review in the main agent. When the judgment itself is the deliverable, do the work yourself.",
      "Delegate by output volume, not step count. A command printing hundreds of lines belongs with the sidekick; a lookup answered in a line or two does not.",
      "Fusion cannot see this conversation. Put the plan and constraints in `context`, state the expected return in `expect`, and review what comes back.",
    ],
    parameters: fusionDelegateSchema,
    executionMode: "sequential",
    async execute(_toolCallId, params, signal, onUpdate, ctx) {{
      const state = fusionStatus();
      syncFusionTool(pi, state);
      if (!state?.delegable) {{
        const reason = state?.reason || "FUSION_UNAVAILABLE";
        return {{
          content: [{{
            type: "text",
            text: "AgentRelay Fusion is not available (" + fusionReasonText(state)
              + "). Continue this task locally.",
          }}],
          details: {{ status: "skipped", reason, state }},
        }};
      }}

      const task = String(params?.task || "").trim();
      if (!task) {{
        return {{
          content: [{{
            type: "text",
            text: "Fusion delegation requires a non-empty task. Continue locally.",
          }}],
          details: {{ status: "skipped", reason: "FUSION_TASK_EMPTY", state }},
        }};
      }}

      const workspace = String(ctx?.cwd || process.cwd());
      const sessionId = contextSessionId(ctx);
      const context = fusionDelegateContext(params);
      const args = ["delegate", task, "--workspace", workspace];
      if (sessionId) args.push("--session-id", sessionId);
      args.push("--context", context);

      onUpdate?.({{
        content: [{{
          type: "text",
          text: "Delegating to AgentRelay Fusion"
            + (state.provider?.ref ? " via " + String(state.provider.ref) : "")
            + "...",
        }}],
        details: {{ status: "running", task, session_id: sessionId }},
      }});

      const outcome = await fusionAsync(args, signal);
      const result = outcome.result;
      if (!result) {{
        const error = outcome.error || "AgentRelay Fusion returned no readable result";
        return {{
          content: [{{
            type: "text",
            text: "Fusion delegation failed: " + error + ". Continue this task locally.",
          }}],
          details: {{
            status: "failed",
            error,
            exit_status: outcome.status,
            cancelled: Boolean(outcome.cancelled),
            state,
          }},
        }};
      }}
      if (result.status !== "success") {{
        const error = String(result.message || result.error || "AgentRelay Fusion failed");
        return {{
          content: [{{
            type: "text",
            text: "Fusion delegation did not complete: " + error
              + ". Continue this task locally.",
          }}],
          details: result,
        }};
      }}

      const answer = String(result.answer || "").trim()
        || "Fusion completed without a final text answer.";
      return {{
        content: [{{
          type: "text",
          text: answer + "\\n\\n[" + fusionEvidence(result) + "]",
        }}],
        details: result,
      }};
    }},
  }});

  pi.registerCommand("agent-approval", {{
    description: "设置权限与交付审计：1=严格完整审计，2=标准轻量/必要升级，3=宽松保留底线",
    handler: approvalCommand,
  }});

  pi.registerCommand("设置", {{
    description: "打开 Agent Approval 权限设置",
    handler: approvalCommand,
  }});

  pi.registerCommand("agent-fusion", {{
    description: "管理 AgentRelay Fusion；无参数进入中文选择菜单",
    handler: fusionCommand,
  }});

  pi.on("session_start", () => {{
    syncFusionTool(pi, fusionStatus());
  }});

  pi.on("before_agent_start", (event, ctx) => {{
    clearPendingAgentEndCheck();
    lastReplyText = "";
    finalStopEmitted = false;
    const fusionState = fusionStatus();
    syncFusionTool(pi, fusionState);
    const relayResult = relay({{
      hook_event_name: "UserPromptSubmit",
      session_id: contextSessionId(ctx),
      cwd: ctx?.cwd ?? process.cwd(),
      prompt: event?.prompt ?? event?.message ?? "",
    }});
    if (relayResult.card) ctx?.ui?.notify(relayResult.card, "info");
    const fusionContent = fusionGuidance(fusionState);
    const result = {{}};
    if (fusionContent) {{
      result.systemPrompt = String(event?.systemPrompt || "") + "\\n\\n" + fusionContent;
    }}
    if (relayResult.context) {{
      result.message = {{
        customType: "agent-relay",
        content: relayResult.context,
        display: false,
      }};
    }}
    return Object.keys(result).length > 0 ? result : undefined;
  }});

  pi.on("tool_call", (event, ctx) => {{
    const result = relay({{
      hook_event_name: "PreToolUse",
      session_id: contextSessionId(ctx),
      cwd: ctx?.cwd ?? process.cwd(),
      tool_name: event?.toolName,
      tool_input: event?.input ?? {{}},
      tool_use_id: event?.toolCallId,
    }});
    if (result.card) ctx?.ui?.notify(result.card, result.denial ? "warning" : "info");
    if (result.denial) return {{ block: true, reason: result.denial }};
    return undefined;
  }});

  pi.on("tool_result", (event, ctx) => {{
    const result = relay({{
      hook_event_name: "PostToolUse",
      session_id: contextSessionId(ctx),
      cwd: ctx?.cwd ?? process.cwd(),
      tool_name: event?.toolName,
      tool_input: event?.input ?? {{}},
      tool_use_id: event?.toolCallId,
      tool_response: {{
        exit_code: event?.isError ? 1 : 0,
      }},
    }});
    if (result.card) ctx?.ui?.notify(result.card, "info");
    return undefined;
  }});

  pi.on("message_end", (event) => {{
    if (event?.message?.role !== "assistant") return undefined;
    const text = assistantText(event.message);
    if (text) lastReplyText = text;
    return undefined;
  }});

  pi.on("agent_settled", (event, ctx) => {{
    agentSettledSupported = true;
    clearPendingAgentEndCheck();
    emitFinalStop(ctx, event);
  }});

  pi.on("agent_end", (event, ctx) => {{
    lastReplyText = lastAssistantText(event?.messages) || lastReplyText;
    if (event?.willContinue === true || agentSettledSupported || finalStopEmitted) return;
    if (!ctx || typeof ctx.isIdle !== "function") {{
      emitFinalStop(ctx, event);
      return;
    }}
    clearPendingAgentEndCheck();
    pendingAgentEndContext = ctx;
    pendingAgentEndCheck = setTimeout(checkPendingAgentEnd, 0);
    if (typeof pendingAgentEndCheck.unref === "function") {{
      pendingAgentEndCheck.unref();
    }}
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
        raw_value = value.strip()
        if raw_value.startswith('"') and raw_value.endswith('"'):
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, str):
                return None
            parsed_value = parsed
        elif raw_value.startswith("'") and raw_value.endswith("'"):
            parsed_value = raw_value[1:-1]
        else:
            parsed_value = raw_value
        values[key.strip()] = parsed_value
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


def write_pi_extension(
    extension_path: Path,
    venv_python: Path,
    shim_path: Path,
    fusion_path: Path | None = None,
) -> bool:
    """Write a portable Pi extension without embedding this machine's paths.

    The path arguments remain part of the public helper signature for installer
    compatibility. The generated extension resolves them again at runtime.
    """
    _ = venv_python, shim_path, fusion_path
    content = PI_EXTENSION_TEMPLATE.format()
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
        if target_id == "claude":
            invalid = []
            for event in events:
                hook = find_agent_relay_hook(hooks[event])
                if hook is None or not command_uses_shim_target(
                    hook.get("command", ""), "claude"
                ):
                    invalid.append(event)
            if invalid:
                return (
                    False,
                    "Claude Hook 未显式使用 agentrelay_hook_shim.py --target claude；"
                    f"缺少目标参数的事件：{', '.join(invalid)}",
                )
            if venv_python is not None and shim_path is not None:
                expected = shim_command(venv_python, shim_path, "claude")
                mismatched = [
                    event for event in events
                    if find_agent_relay_hook(hooks[event]).get("command") != expected
                ]
                if mismatched:
                    return (
                        False,
                        "Claude Hook 命令与当前安装路径不一致："
                        f"{', '.join(mismatched)}",
                    )
        return True, f"已在 {path} 注册 {len(events)} 个事件"
    if target_id == "hermes":
        text = path.read_text(encoding="utf-8")
        missing = [event for event in HERMES_EVENTS if event not in text]
        if not command_uses_shim_target(text, "hermes") or missing:
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
            if values and command_uses_shim_target(
                str(values.get("command", "")), "kimi"
            ):
                covered.add(values.get("event"))
        missing = [event for event in KIMI_EVENTS if event not in covered]
        if missing:
            return False, f"缺少事件：{', '.join(missing)}"
        return True, f"已在 {path} 注册 {len(KIMI_EVENTS)} 个事件"
    if target_id == "pi":
        if not path.is_file():
            return False, f"扩展文件不存在：{path}"
        text = path.read_text(encoding="utf-8")
        if "agentrelay_hook_shim.py" not in text or not command_has_target(text, "pi"):
            return False, "Pi 扩展未显式调用 agentrelay_hook_shim.py --target pi"
        return True, f"扩展文件已生成：{path}"
    return False, f"未知目标：{target_id}"


def relay_installed(target_id: str, meta: dict | None = None) -> bool:
    """轻量检查 AgentRelay 是否已接入该 Agent：Skill 就位且 Hook 已注册。

    只做文件级检查，不启动 Hook 干跑（干跑留给 verify_target）。
    """
    if meta is None:
        meta = agent_targets()[target_id]
    skill_dir = Path(meta["skill_dir"])
    skill_ok = (skill_dir / "SKILL.md").is_file()
    fusion_ok = (
        (skill_dir.with_name("agent-fusion") / "SKILL.md").is_file()
        and (skill_dir.with_name("agent-fusion") / "scripts" / "agent_fusion.py").is_file()
        and (skill_dir.with_name("agent-fusion") / "scripts" / "fusion_runner.py").is_file()
    )
    registered_ok, _ = registered_in_config(target_id, meta, None, None)
    return skill_ok and fusion_ok and registered_ok


def dry_run_hook(
    command: list[str],
    payload: dict,
    timeout: int = 15,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, str]:
    """向 Hook 命令喂一个合成事件，返回执行状态、说明和 stdout。"""
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Hook 干跑无法执行：{exc}", ""
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return (
            False,
            f"Hook 干跑退出码 {result.returncode}：{detail[-1] if detail else '无输出'}",
            result.stdout,
        )
    return True, "Hook 干跑通过（合成事件正常执行并退出）", result.stdout


def visible_card_in_stdout(stdout_text: str) -> bool:
    try:
        payload = json.loads(stdout_text)
    except (json.JSONDecodeError, TypeError):
        return False
    message = payload.get("systemMessage") if isinstance(payload, dict) else None
    if not isinstance(message, str) or not message.strip():
        return False
    return any(
        marker in message for marker in ("需求卡", "确认卡", "审批卡", "任务门禁")
    )


def verify_pi_visible_cards(
    extension_path: Path,
    *,
    main_hook: Path,
    shim_path: Path,
    workspace: Path,
    isolated_home: Path,
    venv_python: Path,
    session_id: str,
) -> tuple[bool, str]:
    """Execute the generated Pi extension against the real Hook and shim."""
    node = shutil.which("node")
    if not node:
        return False, "未安装 Node.js，无法执行 Pi 扩展的真实通知协议验证"
    bridge = extension_path.parent.parent / "node_modules" / "typebox" / "index.js"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    (bridge.parent / "package.json").write_text(
        json.dumps({"name": "typebox", "type": "module", "main": "index.js"}),
        encoding="utf-8",
    )
    bridge.write_text(
        'const schema = () => ({}); schema.Object = schema; schema.String = schema; '
        'schema.Optional = schema; export const Type = schema;\n',
        encoding="utf-8",
    )
    driver = isolated_home / "pi-visible-card-driver.mjs"
    driver.write_text(
        "import agentRelay from " + json.dumps(extension_path.as_uri()) + ";\n"
        "const handlers = new Map();\n"
        "const notifications = [];\n"
        "const pi = {\n"
        "  registerTool() {}, registerCommand() {},\n"
        "  on(name, handler) { handlers.set(name, handler); },\n"
        "  getActiveTools() { return []; }, setActiveTools() {},\n"
        "};\n"
        "const ctx = {\n"
        "  cwd: " + json.dumps(str(workspace)) + ",\n"
        "  sessionManager: { getSessionId() { return " + json.dumps(session_id) + "; } },\n"
        "  hasUI: true,\n"
        "  ui: { notify(message) { notifications.push(String(message)); } },\n"
        "};\n"
        "await agentRelay(pi);\n"
        "await handlers.get('before_agent_start')({\n"
        "  prompt: '请完整实现前后端和数据库功能，并补充测试和部署文档', systemPrompt: ''\n"
        "}, ctx);\n"
        "const toolGate = await handlers.get('tool_call')({\n"
        "  toolName: 'exec_command', input: {cmd: 'git reset --hard'}, toolCallId: 'verify-danger'\n"
        "}, ctx);\n"
        "const promptCard = notifications.some((text) => text.includes('需求卡'));\n"
        "const toolCard = notifications.some((text) => text.includes('危险操作确认卡'));\n"
        "console.log(JSON.stringify({promptCard, toolCard, blocked: toolGate?.block === true, notifications}));\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "CODEX_HOME": str(isolated_home),
        "AGENTRELAY_MAIN_HOOK": str(main_hook),
        "AGENTRELAY_SHIM": str(shim_path),
        "AGENTRELAY_PYTHON": str(venv_python),
    }
    try:
        result = subprocess.run(
            [node, str(driver)], cwd=isolated_home, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Pi 扩展协议验证无法执行：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, "Pi 扩展执行失败：" + (detail[-1][:500] if detail else "无输出")
    try:
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, "Pi 扩展未返回可核对的通知证据"
    if not evidence.get("promptCard") or not evidence.get("toolCard") or not evidence.get("blocked"):
        return False, "Pi 扩展的真实 Hook 链未同时产生需求卡、危险操作卡和工具阻断"
    return True, "真实 Pi 扩展已串联真实 shim，需求卡、危险操作卡和工具阻断均已验证"


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
    fusion_dir = skill_dir.with_name("agent-fusion")
    fusion_ok = (
        (fusion_dir / "SKILL.md").is_file()
        and (fusion_dir / "scripts" / "agent_fusion.py").is_file()
        and (fusion_dir / "scripts" / "fusion_mode.py").is_file()
        and (fusion_dir / "scripts" / "fusion_runner.py").is_file()
    )
    checks.append({
        "name": "Fusion Skill",
        "ok": fusion_ok,
        "detail": str(fusion_dir),
        "reason": "缺少 Fusion 文件时持久 sidekick、工具模式和 /agent-fusion 开关不可用",
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

    session_id = (
        f"agentrelay-verify-{target_id}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "cwd": str(workspace),
        "prompt": "AgentRelay 安装验证，无需处理",
    }
    if target_id in {"claude", "pi", "hermes", "kimi"}:
        command = [
            str(venv_python),
            *PYTHON_UTF8_ARGS,
            str(shim_path),
            "--target",
            target_id,
            "--event",
            "UserPromptSubmit",
        ]
        probe_payload = dict(payload)
        if target_id == "hermes":
            # Hermes 真实 payload 不带事件名，验证 shim 的注入路径。
            probe_payload.pop("hook_event_name", None)
        hook_ok, hook_detail, _ = dry_run_hook(command, probe_payload)
    else:
        command = [str(venv_python), *PYTHON_UTF8_ARGS, str(main_hook)]
        hook_ok, hook_detail, _ = dry_run_hook(command, payload)
    checks.append({
        "name": "Hook 干跑",
        "ok": hook_ok,
        "detail": hook_detail,
        "reason": "干跑失败说明 Hook 命令无法被该 Agent 正常拉起",
    })

    if target_id in {"codex", "claude", "pi", "hermes"}:
        with tempfile.TemporaryDirectory(prefix="agentrelay-visible-card-") as isolated:
            isolated_home = Path(isolated)
            approval_policy.save_policy(isolated_home, "strict")
            probe_env = {
                **os.environ,
                "CODEX_HOME": str(isolated_home),
                "AGENTRELAY_MAIN_HOOK": str(main_hook),
            }
            if target_id == "pi":
                card_ok, card_detail = verify_pi_visible_cards(
                    Path(meta["config_path"]), main_hook=main_hook,
                    shim_path=shim_path, workspace=workspace,
                    isolated_home=isolated_home, venv_python=venv_python,
                    session_id=f"{session_id}-visible-card",
                )
                cards = []
            else:
                if target_id == "codex":
                    card_command = [
                        str(venv_python),
                        *PYTHON_UTF8_ARGS,
                        str(main_hook),
                    ]
                    # Codex invokes the main Hook directly; isolate HOME while
                    # leaving target-specific environment variables absent.
                    probe_env["HOME"] = str(isolated_home)
                    probe_env["AGENTRELAY_TARGET"] = "codex"
                    approval_policy.save_policy(isolated_home / ".codex", "strict")
                    probe_env.pop("CODEX_HOME", None)
                else:
                    card_command = [
                        str(venv_python),
                        *PYTHON_UTF8_ARGS,
                        str(shim_path),
                        "--target",
                        target_id,
                    ]
                cards = [
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": f"{session_id}-visible-card",
                        "cwd": str(workspace),
                        "prompt": "请完整实现前后端和数据库功能，并补充测试和部署文档",
                    },
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": f"{session_id}-visible-danger",
                        "cwd": str(workspace),
                        "tool_name": "exec_command",
                        "tool_input": {"cmd": "git reset --hard"},
                        "tool_use_id": f"{session_id}-danger-tool",
                    },
                ]
                if target_id == "hermes":
                    card_results = []
                    for item in cards:
                        event = item["hook_event_name"]
                        item = {key: value for key, value in item.items() if key != "hook_event_name"}
                        result = dry_run_hook(
                            [*card_command, "--event", event], item, env=probe_env,
                        )
                        card_results.append(result)
                else:
                    card_results = [
                        dry_run_hook(card_command, item, env=probe_env)
                        for item in cards
                    ]
                card_ok = all(
                    item[0] and visible_card_in_stdout(item[2])
                    for item in card_results
                )
                card_detail = (
                    "真实 shim 协议已返回需求卡与 PreToolUse 危险操作卡"
                    if card_ok else "需求卡或 PreToolUse 危险操作卡缺少 systemMessage："
                    + "；".join(item[1] for item in card_results)
                )
        checks.append({
            "name": "可见卡协议",
            "ok": card_ok,
            "detail": card_detail,
            "reason": "仅能退出不够；目标宿主还必须实际收到用户可见的审批卡",
        })

    try:
        status = subprocess.run(
            [
                str(venv_python),
                *PYTHON_UTF8_ARGS,
                str(tracker),
                "status",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    _configure_utf8_stdio()
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
