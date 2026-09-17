#!/usr/bin/env python3
"""AgentRelay 本地归档：把已完成/已归档任务按项目记录在 Skill 目录内。

归档位置永远和 Skill 在同一个文件夹：

    <skill>/归档/<项目名>/已完成任务.jsonl
    <skill>/归档/<项目名>/归档.md

只保存任务结论、修改文件、验证结果和回滚提示，不保存 Cookie、密钥、
登录状态或原始对话。任务是否完成由模型判断并回写，用户也可以直接要求
“归档”“记录一下”。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ARCHIVE_DIRNAME = "归档"
RECORDS_FILENAME = "已完成任务.jsonl"
SUMMARY_FILENAME = "归档.md"
MAX_RECORDS_PER_PROJECT = 200


def skill_root() -> Path:
    """返回 Skill 根目录；归档必须落在它下面。"""
    override = os.environ.get("AGENTRELAY_SKILL_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def archive_root() -> Path:
    return skill_root() / ARCHIVE_DIRNAME


def sanitize_project(project: str) -> str:
    """项目名只保留安全字符，避免跳出归档目录。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", str(project or "").strip())
    cleaned = cleaned.strip(".-")
    return cleaned or "default"


def infer_project(workspace: str | Path | None = None, explicit: str | None = None) -> str:
    """推断项目名：显式参数 > 环境变量 > git 仓库名 > 工作目录名。"""
    if explicit:
        return sanitize_project(explicit)
    from_env = os.environ.get("AGENTRELAY_PROJECT")
    if from_env:
        return sanitize_project(from_env)
    if workspace:
        candidate = Path(workspace).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        marker = resolved
        while marker != marker.parent and not (marker / ".git").exists():
            marker = marker.parent
        if (marker / ".git").exists():
            return sanitize_project(marker.name)
        return sanitize_project(resolved.name)
    return sanitize_project(Path.cwd().name)


@dataclass
class ArchiveRecord:
    """一条归档记录。字段保持扁平，方便人和模型直接读。"""

    record_id: str
    project: str
    title: str
    status: str = "已完成"
    created_at: str = ""
    request: str = ""
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    rollback: str = ""
    risks: list[str] = field(default_factory=list)
    task_type: str = ""
    weight: int = 0
    retry_count: int = 0
    provider: str = ""
    archived_by: str = "model"
    tags: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new_id(project: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 同一秒内可能连续归档多条，必须带随机后缀，否则会互相覆盖。
    return f"{sanitize_project(project)}-{stamp}-{os.urandom(3).hex()}"


def project_directory(project: str) -> Path:
    return archive_root() / sanitize_project(project)


def records_path(project: str) -> Path:
    return project_directory(project) / RECORDS_FILENAME


def summary_path(project: str) -> Path:
    return project_directory(project) / SUMMARY_FILENAME


def list_projects() -> list[str]:
    root = archive_root()
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if item.is_dir())


def load_records(project: str) -> list[ArchiveRecord]:
    path = records_path(project)
    if not path.is_file():
        return []
    records: list[ArchiveRecord] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
        if not isinstance(value, dict) or not value.get("title"):
            continue
        known = {key: value[key] for key in ArchiveRecord.__dataclass_fields__ if key in value}
        known.setdefault("record_id", _new_id(project))
        known.setdefault("project", sanitize_project(project))
        records.append(ArchiveRecord(**known))
    return records


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_markdown(project: str, records: Iterable[ArchiveRecord]) -> str:
    items = list(records)
    lines = [f"# 归档 · {sanitize_project(project)}", "", f"共 {len(items)} 条记录。", ""]
    for record in reversed(items):
        lines.append(f"## {record.title}")
        lines.append("")
        lines.append(f"- 状态：{record.status}")
        lines.append(f"- 时间：{record.created_at or '未记录'}")
        if record.task_type:
            lines.append(f"- 任务类型：{record.task_type}")
        if record.archived_by:
            lines.append(f"- 归档方式：{'用户要求' if record.archived_by == 'user' else '模型判断完成'}")
        if record.summary:
            lines.append(f"- 结论：{record.summary}")
        if record.changed_files:
            lines.append(f"- 修改文件：{', '.join(record.changed_files)}")
        if record.verification:
            lines.append(f"- 验证：{'；'.join(record.verification)}")
        if record.rollback:
            lines.append(f"- 回滚：{record.rollback}")
        if record.risks:
            lines.append(f"- 风险：{'；'.join(record.risks)}")
        if record.tags:
            lines.append(f"- 标签：{', '.join(record.tags)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def archive_task(
    *,
    title: str,
    project: str | None = None,
    workspace: str | Path | None = None,
    request: str = "",
    summary: str = "",
    status: str = "已完成",
    changed_files: Iterable[str] = (),
    verification: Iterable[str] = (),
    rollback: str = "",
    risks: Iterable[str] = (),
    task_type: str = "",
    weight: int = 0,
    retry_count: int = 0,
    provider: str = "",
    archived_by: str = "model",
    tags: Iterable[str] = (),
    record_id: str | None = None,
) -> ArchiveRecord:
    """追加一条归档记录，并刷新人类可读的 `归档.md`。"""
    if not str(title or "").strip():
        raise ValueError("归档缺少任务标题")
    resolved_project = infer_project(workspace, project)
    record = ArchiveRecord(
        record_id=record_id or _new_id(resolved_project),
        project=resolved_project,
        title=str(title).strip()[:200],
        status=str(status or "已完成").strip(),
        created_at=_now(),
        request=str(request or "")[:4000],
        summary=str(summary or "")[:4000],
        changed_files=[str(item) for item in changed_files if str(item).strip()],
        verification=[str(item) for item in verification if str(item).strip()],
        rollback=str(rollback or "")[:2000],
        risks=[str(item) for item in risks if str(item).strip()],
        task_type=str(task_type or "")[:120],
        weight=max(0, int(weight or 0)),
        retry_count=max(0, int(retry_count or 0)),
        provider=str(provider or "")[:120],
        archived_by="user" if str(archived_by).lower() in {"user", "用户"} else "model",
        tags=[str(item) for item in tags if str(item).strip()],
    )
    existing = [item for item in load_records(resolved_project) if item.record_id != record.record_id]
    existing.append(record)
    trimmed = existing[-MAX_RECORDS_PER_PROJECT:]
    _write_text_atomic(
        records_path(resolved_project),
        "\n".join(json.dumps(asdict(item), ensure_ascii=False) for item in trimmed) + "\n",
    )
    _write_text_atomic(summary_path(resolved_project), render_markdown(resolved_project, trimmed))
    return record


def search_records(project: str, query: str, limit: int = 5) -> list[ArchiveRecord]:
    """关键词粗排：命中标题、结论、文件或标签的优先。"""
    tokens = [
        token.casefold()
        for token in re.findall(r"[0-9A-Za-z_+#.-]+|[\u4e00-\u9fff]{2,}", str(query or ""))
    ]
    scored: list[tuple[int, ArchiveRecord]] = []
    for record in load_records(project):
        haystack = " ".join([
            record.title, record.summary, record.request,
            " ".join(record.changed_files), " ".join(record.tags),
        ]).casefold()
        score = sum(haystack.count(token) for token in tokens) if tokens else 0
        if score:
            scored.append((score, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in scored[: max(1, int(limit))]]


def _split_csv(values: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if part:
                result.append(part)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentRelay 本地归档（记录在 Skill 目录内）")
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="追加一条归档记录")
    add.add_argument("--title", required=True, help="任务标题")
    add.add_argument("--project", default=None, help="项目名；默认自动推断")
    add.add_argument("--workspace", default=None, help="项目工作区路径，用于推断项目名")
    add.add_argument("--request", default="", help="原始需求")
    add.add_argument("--summary", default="", help="结论摘要")
    add.add_argument("--status", default="已完成", help="状态，例如 已完成 / 已回滚")
    add.add_argument("--file", action="append", default=[], help="修改文件，可重复或用逗号分隔")
    add.add_argument("--verification", action="append", default=[], help="验证结果，可重复")
    add.add_argument("--rollback", default="", help="回滚说明")
    add.add_argument("--risk", action="append", default=[], help="风险，可重复")
    add.add_argument("--task-type", default="", help="任务类型")
    add.add_argument("--weight", type=int, default=0)
    add.add_argument("--retry-count", type=int, default=0)
    add.add_argument("--provider", default="")
    add.add_argument("--archived-by", default="model", choices=["model", "user"])
    add.add_argument("--tag", action="append", default=[])

    listing = sub.add_parser("list", help="列出某个项目的归档")
    listing.add_argument("--project", default=None)
    listing.add_argument("--limit", type=int, default=20)

    search = sub.add_parser("search", help="在归档里搜索")
    search.add_argument("query")
    search.add_argument("--project", default=None)
    search.add_argument("--limit", type=int, default=5)

    sub.add_parser("projects", help="列出已有归档的项目")
    sub.add_parser("path", help="显示归档目录")
    return parser


def _print_record(record: ArchiveRecord) -> None:
    print(f"· [{record.status}] {record.title}")
    way = "用户要求" if record.archived_by == "user" else "模型判断"
    print(f"  项目: {record.project} | 时间: {record.created_at} | 归档: {way}")
    if record.summary:
        print(f"  结论: {record.summary}")
    if record.changed_files:
        print(f"  文件: {', '.join(record.changed_files)}")
    if record.verification:
        print(f"  验证: {'；'.join(record.verification)}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "add":
        record = archive_task(
            title=args.title, project=args.project, workspace=args.workspace,
            request=args.request, summary=args.summary, status=args.status,
            changed_files=_split_csv(args.file), verification=_split_csv(args.verification),
            rollback=args.rollback, risks=_split_csv(args.risk), task_type=args.task_type,
            weight=args.weight, retry_count=args.retry_count, provider=args.provider,
            archived_by=args.archived_by, tags=_split_csv(args.tag),
        )
        print(f"[归档] {record.project} · {record.title}")
        print(f"[位置] {records_path(record.project)}")
        return 0
    if args.command == "list":
        project = infer_project(explicit=args.project)
        all_records = load_records(project)
        if not all_records:
            print(f"项目 {project} 还没有归档记录。")
            return 0
        print(f"项目 {project} 共 {len(all_records)} 条归档：")
        for record in all_records[-max(1, int(args.limit)):]:
            _print_record(record)
        return 0
    if args.command == "search":
        project = infer_project(explicit=args.project)
        hits = search_records(project, args.query, args.limit)
        if not hits:
            print(f"项目 {project} 的归档里没有匹配「{args.query}」的记录。")
            return 0
        for record in hits:
            _print_record(record)
        return 0
    if args.command == "projects":
        projects = list_projects()
        print("\n".join(projects) if projects else "还没有任何项目归档。")
        return 0
    if args.command == "path":
        print(archive_root())
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
