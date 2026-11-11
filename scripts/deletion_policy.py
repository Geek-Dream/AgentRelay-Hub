"""Mode-bound deletion policy with recoverable, audited trash operations."""
from __future__ import annotations

import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable


SAFE_TEMP_NAMES = re.compile(
    r"(?:agent[-_]?relay|screen[-_]?shot|capture|snapshot|review|"
    r"verif(?:y|ication)|evidence|check|gstack|pytest|cache)",
    re.IGNORECASE,
)
SAFE_TEMP_TARGET = re.compile(r"(?:^|[-_])(?:dcm|dc-query)(?:[-_]|$)", re.IGNORECASE)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _temporary_roots() -> list[Path]:
    import tempfile
    roots = []
    for value in (tempfile.gettempdir(), os.environ.get("TMPDIR"), "/tmp", "/private/tmp"):
        if not value:
            continue
        try:
            root = Path(value).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def classify_target(value: str, *, home: Path, cwd: Path) -> dict[str, Any]:
    """Only named AgentRelay evidence in temp/cache paths may be quiet."""
    raw = Path(value).expanduser()
    path = raw if raw.is_absolute() else cwd / raw
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return {"kind": "unknown", "path": str(path), "quiet": False}
    tracker = home.expanduser().resolve() / "agent_relay_tracker"
    named_evidence = bool(SAFE_TEMP_NAMES.search(path.name) or SAFE_TEMP_TARGET.search(path.parent.name))
    image_evidence = path.suffix.lower() in IMAGE_SUFFIXES and named_evidence
    temporary = any(_inside(resolved, root) for root in _temporary_roots())
    owned = _inside(resolved, tracker) or _inside(resolved, home / ".cache" / "agentrelay")
    quiet = image_evidence and (temporary or owned)
    return {
        "kind": "temporary_evidence" if quiet else "persistent",
        "path": str(resolved),
        "quiet": quiet,
        "exists": path.exists() or path.is_symlink(),
        "is_symlink": path.is_symlink(),
    }


def _strip_command_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _windows_command_has_control_operator(command: str) -> bool:
    """Reject command chains and PowerShell comma lists before token parsing."""
    quote = ""
    escaped = False
    for char in command:
        if quote:
            if escaped:
                escaped = False
            elif char == "`":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in {"&", "|", ";", "<", ">", ","} or char in {"\r", "\n"}:
            return True
    return False


def _command_words(
    command: str,
    *,
    platform_name: str | None = None,
) -> list[str] | None:
    if (platform_name or os.name) == "nt":
        if _windows_command_has_control_operator(command):
            return None
        try:
            words = shlex.split(command, posix=False)
        except ValueError:
            return None
        return [_strip_command_quotes(word) for word in words]
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _command_name(value: str) -> str:
    return (
        ntpath.basename(value)
        or os.path.basename(value)
        or value
    ).lower()


def _simple_delete_targets(
    words: list[str],
    *,
    platform_name: str | None = None,
) -> list[dict[str, Any]] | None:
    if not words or any(word in {"&&", "||", ";", "|", "|&", "&", ">", ">>", "<"} for word in words):
        return None
    command_name = _command_name(words[0])
    windows = (platform_name or os.name) == "nt"
    if windows:
        supported = {
            "del", "erase", "rd", "remove-item", "rm", "rmdir",
        }
    else:
        supported = {"rm", "rmdir", "unlink"}
    if command_name not in supported:
        return None

    targets = []
    options_done = False
    expect_path = False
    for word in words[1:]:
        if expect_path:
            targets.append(word)
            expect_path = False
            continue
        if not options_done and word == "--":
            options_done = True
            continue
        if windows and command_name in {"remove-item", "rm"} and not options_done:
            option = word.lstrip("-").lower()
            if word.startswith("-") and option in {"path", "literalpath"}:
                if expect_path:
                    return None
                expect_path = True
                continue
            if word.startswith("-") and option in {
                "confirm", "force", "recurse", "verbose",
            }:
                continue
            if word.startswith("-"):
                return None
        elif windows and not options_done and word.startswith("/"):
            option = word.lower()
            allowed = (
                {"/a", "/f", "/p", "/q", "/s"}
                if command_name in {"del", "erase"}
                else {"/p", "/q", "/s"}
            )
            if option in allowed:
                continue
            return None
        elif not options_done and word.startswith("-"):
            if command_name == "unlink":
                return None
            if word.startswith("--"):
                if word not in {"--force", "--verbose", "--recursive", "--dir", "--preserve-root"}:
                    return None
            elif not set(word[1:]).issubset(set("fvrRdiI")):
                return None
            continue
        targets.append(word)
    if expect_path:
        return None
    if not targets or any(any(char in word for char in "*?[]{}") for word in targets):
        return None
    return [{"command": command_name, "target": word} for word in targets]


def parse_simple_delete(
    command: str,
    *,
    platform_name: str | None = None,
) -> list[dict[str, Any]] | None:
    """Parse only a single native delete invocation; never rewrite a chain."""
    words = _command_words(command, platform_name=platform_name)
    if words is None:
        return None
    return _simple_delete_targets(words, platform_name=platform_name)


def _unique_destination(root: Path, source: Path) -> Path:
    candidate = root / source.name
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    suffix = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    candidate = root / f"{source.name}.{suffix}"
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = root / f"{source.name}.{suffix}-{counter}"
        counter += 1
    return candidate


def move_to_trash(targets: Iterable[str], *, home: Path, cwd: Path) -> dict[str, Any]:
    """Move explicit local targets into the per-user recoverable AgentRelay trash."""
    root = home.expanduser().resolve() / "agent_relay_tracker" / "trash"
    files = root / "files"
    manifests = root / "manifests"
    files.mkdir(parents=True, exist_ok=True)
    manifests.mkdir(parents=True, exist_ok=True)
    sources = []
    for value in targets:
        raw = Path(value).expanduser()
        source = raw if raw.is_absolute() else cwd.expanduser().resolve() / raw
        if source.is_symlink():
            raise ValueError(f"回收站不处理符号链接，避免目标歧义：{source}")
        if not source.exists():
            raise ValueError(f"删除目标不存在，未执行任何操作：{source}")
        if _inside(source.resolve(), root) or _inside(source.resolve(), home.expanduser().resolve() / ".git"):
            raise ValueError(f"不能回收回收站自身或 Git 内部数据：{source}")
        sources.append(source)
    completed = []
    try:
        for source in sources:
            # Reserve the destination after earlier moves so same-named directories
            # such as multiple __pycache__ folders cannot collide.
            destination = _unique_destination(files, source)
            item = {"source": str(source), "destination": str(destination)}
            shutil.move(source, destination)
            completed.append(item)
    except Exception:
        for item in reversed(completed):
            if Path(item["destination"]).exists() and not Path(item["source"]).exists():
                shutil.move(item["destination"], item["source"])
        raise
    receipt = {
        "version": 1, "operation": "move_to_trash", "created_at": time.time(),
        "restore": "将 destination 移回 source；目标已存在时先停止，不覆盖",
        "items": completed,
    }
    manifest = manifests / f"trash-{int(time.time() * 1000)}-{os.getpid()}.json"
    manifest.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "moved_to_trash", "manifest": str(manifest), "items": completed}


def _format_command(
    parts: Iterable[object],
    *,
    platform_name: str | None = None,
) -> str:
    values = [str(part) for part in parts]
    if (platform_name or os.name) == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)


def safe_delete_command(
    command: str,
    *,
    home: Path,
    cwd: Path,
    mode: str,
    platform_name: str | None = None,
) -> str | None:
    """Return one auditable replacement command, never a rewritten shell chain."""
    entries = parse_simple_delete(command, platform_name=platform_name)
    if entries is None or mode == "relaxed":
        return None
    targets = [entry["target"] for entry in entries]
    results = [classify_target(value, home=home, cwd=cwd) for value in targets]
    if any(item["is_symlink"] for item in results):
        return None
    if mode == "standard" and any(not item["quiet"] for item in results):
        return None
    script = Path(__file__).resolve()
    return _format_command([
        os.environ.get("AGENTRELAY_PYTHON") or __import__("sys").executable,
        str(script), "execute", "--home", str(home), "--cwd", str(cwd),
        "--mode", mode, "--", *targets,
    ], platform_name=platform_name)


def is_safe_delete_command(
    command: str,
    *,
    home: Path,
    mode: str,
    platform_name: str | None = None,
) -> bool:
    """Recognize only the exact managed recycler emitted for this home/mode."""
    if mode not in {"standard", "strict"}:
        return False
    words = _command_words(command, platform_name=platform_name)
    if words is None:
        return False
    if len(words) < 11 or Path(words[1]).expanduser().resolve() != Path(__file__).resolve():
        return False
    if words[2:4] != ["execute", "--home"] or Path(words[4]).expanduser().resolve() != home.expanduser().resolve():
        return False
    if words[5] != "--cwd" or words[7:9] != ["--mode", mode] or words[9] != "--":
        return False
    return all(classify_target(value, home=home, cwd=Path(words[6]))["quiet"] or mode == "strict"
               for value in words[10:])


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    execute = sub.add_parser("execute")
    execute.add_argument("--home", type=Path, required=True)
    execute.add_argument("--cwd", type=Path, required=True)
    execute.add_argument("--mode", choices=["standard", "strict"], required=True)
    execute.add_argument("targets", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "execute":
        try:
            result = move_to_trash(args.targets, home=args.home, cwd=args.cwd)
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
