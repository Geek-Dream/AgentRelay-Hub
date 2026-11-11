"""Bundled and official Agent Skill catalog loading and selection helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterable


CATALOG_ID = "vuejs-ai"
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MANAGED_SKILL_IDS = frozenset({"agent-relay", "agent-fusion", "agent-image"})
PRIVATE_SKILL_IDS = frozenset({"bitch-skill"})
EXCLUDED_SKILL_IDS = MANAGED_SKILL_IDS | PRIVATE_SKILL_IDS
PREFERRED_DEFAULT_SKILL_IDS = (
    "browser-automatic",
    "docx",
    "pdf",
    "pptx",
    "xlsx",
    "vue-best-practices",
    "web-artifacts-builder",
)
INSTALLED_SOURCE = "installed"
BUNDLED_SOURCE = "bundled"
OFFICIAL_SOURCE = "official"
COPY_ALLOWED = "allowed"
COPY_RESTRICTED = "restricted"
COPY_UNKNOWN = "unknown"
COPY_INSTALL_METHOD = "copy"
OFFICIAL_DOWNLOAD_METHOD = "official-download"
AGENTRELAY_BUNDLED_CATALOG = "agentrelay"
AGENTRELAY_BUNDLED_SKILLS = {
    "browser-automatic": {
        "name": "Browser Automatic",
        "description": "使用 Playwright 执行网页截图、内容提取和多步骤浏览器操作。",
        "path": "browser-automatic",
        "license": "LICENSE",
        "license_spdx": "MIT",
        "files": 3,
        "bytes": 15_693,
        "sha256": "555117f28c276fc64a9695514325a5a53f9ed90da2ab58e6083572060b06f262",
    },
}
AGENTRELAY_BUNDLED_SKILL_IDS = frozenset(AGENTRELAY_BUNDLED_SKILLS)
OFFICIAL_SKILL_REPOSITORY = "https://github.com/anthropics/skills"
OFFICIAL_SKILL_COMMIT = "2c5958ecac7cee2b583967a2c4981d0f4a75c579"
OFFICIAL_SKILL_ARCHIVE_ROOT = "anthropics-skills-2c5958e"
OFFICIAL_SKILL_ARCHIVE_URL = (
    "https://codeload.github.com/anthropics/skills/tar.gz/"
    f"{OFFICIAL_SKILL_COMMIT}"
)
OFFICIAL_SKILL_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024
OFFICIAL_SKILL_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
OFFICIAL_SKILL_ARCHIVE_MAX_MEMBERS = 2_000
OFFICIAL_SKILL_TREE_MAX_FILES = 512
OFFICIAL_SKILL_TREE_MAX_BYTES = 16 * 1024 * 1024
OFFICIAL_SKILLS = {
    "docx": {
        "name": "DOCX",
        "description": "创建、读取和编辑 Word（.docx/.dotx）文档。",
        "path": "skills/docx",
        "license": "LICENSE.txt",
        "license_spdx": "Apache-2.0",
        "files": 61,
        "bytes": 1_138_571,
        "sha256": "616f869ece12bbba1d69e181dec5feb06b5d480ca6ee93440b8d9a0587d97bdd",
    },
    "pdf": {
        "name": "PDF",
        "description": "读取、提取、合并、拆分、填写和生成 PDF 文件。",
        "path": "skills/pdf",
        "license": "LICENSE.txt",
        "license_spdx": "Apache-2.0",
        "files": 12,
        "bytes": 68_568,
        "sha256": "5fc260e2f8a025f958809333ecd08fc01185fe126721a44d079ab96f6002b488",
    },
    "pptx": {
        "name": "PPTX",
        "description": "创建、读取和编辑 PowerPoint（.pptx/.potx）演示文稿。",
        "path": "skills/pptx",
        "license": "LICENSE.txt",
        "license_spdx": "Apache-2.0",
        "files": 56,
        "bytes": 1_149_051,
        "sha256": "f61356e73cdededc04f90ceef5adfb72ad5fae528c857939a562fe83ee0afb6b",
    },
    "xlsx": {
        "name": "XLSX",
        "description": "读取、编辑和分析 Excel（.xlsx/.xlsm/.csv）表格与公式。",
        "path": "skills/xlsx",
        "license": "LICENSE.txt",
        "license_spdx": "Apache-2.0",
        "files": 53,
        "bytes": 1_112_769,
        "sha256": "5c03f03a13a4af44e1c22e8060e2ca14dc674f7008f8e9daa93d8f9ac401053c",
    },
    "web-artifacts-builder": {
        "name": "Web Artifacts Builder",
        "description": "构建独立、可运行的 Web Artifact 和交互式前端产物。",
        "path": "skills/web-artifacts-builder",
        "license": "LICENSE.txt",
        "license_spdx": "Apache-2.0",
        "files": 5,
        "bytes": 45_840,
        "sha256": "0f102ba19f13b4deb19f2248a5014952d2af1521129a5a0e4f3d805f74632b75",
    },
}
OFFICIAL_SKILL_IDS = frozenset(OFFICIAL_SKILLS)
LICENSE_FILE_PATTERN = re.compile(
    r"^(?:license|licence|copying|notice)(?:\.[a-z0-9_-]+)?$",
    re.IGNORECASE,
)
RESTRICTED_COPY_MARKERS = (
    "retain copies of these materials outside the services",
    "reproduce or copy these materials",
    "distribute, sublicense, or transfer these materials",
)
PERMISSIVE_LICENSE_MARKERS = (
    "mit license",
    "apache license",
    "bsd 2-clause license",
    "bsd 3-clause license",
    "isc license",
    "mozilla public license",
)


class CatalogError(ValueError):
    """A bundled Skill catalog is missing, unsafe, or inconsistent."""


def official_skill_entry(
    skill_id: str,
    installed_in: Iterable[str] | None = None,
) -> dict:
    """Return catalog metadata for an independently downloaded official Skill."""

    metadata = OFFICIAL_SKILLS.get(skill_id)
    if metadata is None:
        raise CatalogError(f"Unknown official Skill: {skill_id}")
    return {
        "id": skill_id,
        "name": metadata["name"],
        "version": "",
        "description": metadata["description"],
        "source_type": OFFICIAL_SOURCE,
        "installed_in": list(installed_in or []),
        "copy_policy": COPY_RESTRICTED,
        "installable": True,
        "install_method": OFFICIAL_DOWNLOAD_METHOD,
        "official_repository": OFFICIAL_SKILL_REPOSITORY,
        "official_commit": OFFICIAL_SKILL_COMMIT,
        "official_archive_url": OFFICIAL_SKILL_ARCHIVE_URL,
        "official_path": metadata["path"],
        "license_spdx": metadata["license_spdx"],
        "restriction": (
            "不复制本机现有副本；安装时从 Anthropic 官方仓库固定提交独立下载 "
            "Apache-2.0 版本"
        ),
    }


def catalog_root(project_root: Path) -> Path:
    return Path(project_root).resolve() / "skills" / "catalog" / CATALOG_ID


def agentrelay_bundled_catalog_root(project_root: Path) -> Path:
    return (
        Path(project_root).resolve()
        / "skills"
        / "catalog"
        / AGENTRELAY_BUNDLED_CATALOG
    )


def _tree_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise CatalogError(f"Skill tree is not a regular directory: {root}")
    files: list[Path] = []
    try:
        entries = sorted(root.rglob("*"))
    except OSError as exc:
        raise CatalogError(f"Cannot scan Skill tree {root}: {exc}") from exc
    for path in entries:
        try:
            relative = path.relative_to(root)
            if (
                "__pycache__" in relative.parts
                or path.suffix.casefold() in {".pyc", ".pyo"}
            ):
                continue
            if path.is_symlink():
                raise CatalogError(f"Skill tree contains a symbolic link: {path}")
            if path.is_file():
                files.append(path)
            elif not path.is_dir():
                raise CatalogError(f"Skill tree contains a non-regular entry: {path}")
        except OSError as exc:
            raise CatalogError(f"Cannot inspect Skill tree entry {path}: {exc}") from exc
    return files


def _integrity_bytes(path: Path, *, normalize_newlines: bool = False) -> bytes:
    data = path.read_bytes()
    if normalize_newlines:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def _tree_sha256(
    root: Path,
    *,
    normalize_newlines: bool = False,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    files = _tree_files(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            _integrity_bytes(path, normalize_newlines=normalize_newlines)
        )
        digest.update(b"\0")
    return len(files), digest.hexdigest()


def _safe_relative_path(root: Path, raw: object) -> Path:
    text = str(raw or "").strip()
    candidate = Path(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise CatalogError(f"Unsafe catalog path: {raw!r}")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise CatalogError(f"Catalog path escapes its root: {raw!r}") from exc
    return resolved


def _validate_skill_entry(root: Path, entry: object, seen: set[str]) -> dict:
    if not isinstance(entry, dict):
        raise CatalogError("Each catalog skill must be a JSON object")
    skill_id = str(entry.get("id") or "").strip()
    if not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise CatalogError(f"Invalid Skill ID: {skill_id!r}")
    if skill_id in seen:
        raise CatalogError(f"Duplicate Skill ID: {skill_id}")
    seen.add(skill_id)
    for field in ("name", "version", "description"):
        if not str(entry.get(field) or "").strip():
            raise CatalogError(f"Skill {skill_id} is missing {field}")
    skill_dir = _safe_relative_path(root, f"skills/{skill_id}")
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        raise CatalogError(f"Skill directory is missing: {skill_dir}")
    skill_file = skill_dir / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        raise CatalogError(f"Skill {skill_id} is missing SKILL.md")
    return {**entry, "id": skill_id}


def load_catalog(project_root: Path) -> dict:
    """Load and verify the bundled catalog without network access."""

    root = catalog_root(project_root)
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot read Skill catalog: {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CatalogError("Skill catalog manifest must be a JSON object")
    if manifest.get("schema_version") != 1:
        raise CatalogError("Unsupported Skill catalog schema version")
    if manifest.get("id") != CATALOG_ID:
        raise CatalogError(f"Unexpected Skill catalog ID: {manifest.get('id')!r}")
    if not str(manifest.get("name") or "").strip():
        raise CatalogError("Skill catalog is missing a name")

    source = manifest.get("source")
    if not isinstance(source, dict):
        raise CatalogError("Skill catalog is missing source metadata")
    repository = str(source.get("repository") or "").strip()
    commit = str(source.get("commit") or "").strip()
    if repository != "https://github.com/vuejs-ai/skills":
        raise CatalogError(f"Unexpected Skill catalog repository: {repository!r}")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise CatalogError(f"Invalid source commit: {commit!r}")

    license_info = manifest.get("license")
    if not isinstance(license_info, dict) or license_info.get("spdx") != "MIT":
        raise CatalogError("Skill catalog must declare the MIT license")
    license_path = _safe_relative_path(root, license_info.get("path"))
    if license_path.is_symlink() or not license_path.is_file():
        raise CatalogError(f"Skill catalog license is missing: {license_path}")
    license_digest = hashlib.sha256(
        _integrity_bytes(license_path, normalize_newlines=True)
    ).hexdigest()
    if license_digest != license_info.get("sha256"):
        raise CatalogError("Skill catalog license checksum does not match")
    if not str(license_info.get("copyright") or "").strip():
        raise CatalogError("Skill catalog license is missing copyright attribution")

    entries = manifest.get("skills")
    if not isinstance(entries, list) or not entries:
        raise CatalogError("Skill catalog must contain at least one Skill")
    seen: set[str] = set()
    manifest["skills"] = [
        _validate_skill_entry(root, entry, seen) for entry in entries
    ]

    defaults = normalize_skill_ids(manifest, manifest.get("defaults"))
    if not defaults:
        raise CatalogError("Skill catalog must select at least one default Skill")
    manifest["defaults"] = defaults

    skills_root = root / "skills"
    content_files, content_digest = _tree_sha256(
        skills_root,
        normalize_newlines=True,
    )
    if content_files != manifest.get("content_files"):
        raise CatalogError(
            f"Skill catalog file count changed: {content_files} "
            f"!= {manifest.get('content_files')}"
        )
    if content_digest != manifest.get("content_sha256"):
        raise CatalogError("Skill catalog content checksum does not match")
    return manifest


def agentrelay_bundled_skill_entry(
    project_root: Path,
    skill_id: str,
    installed_in: Iterable[str] | None = None,
) -> dict:
    """Validate and describe an MIT Skill maintained by AgentRelay."""

    metadata = AGENTRELAY_BUNDLED_SKILLS.get(skill_id)
    if metadata is None:
        raise CatalogError(f"Unknown AgentRelay bundled Skill: {skill_id}")
    root = agentrelay_bundled_catalog_root(project_root)
    source = _safe_relative_path(root, metadata["path"])
    if source.is_symlink() or not source.is_dir():
        raise CatalogError(f"AgentRelay bundled Skill is missing: {source}")
    skill_file = source / "SKILL.md"
    license_path = source / str(metadata["license"])
    if (
        skill_file.is_symlink()
        or not skill_file.is_file()
        or license_path.is_symlink()
        or not license_path.is_file()
    ):
        raise CatalogError(
            f"AgentRelay bundled Skill {skill_id} is missing SKILL.md or LICENSE"
        )
    try:
        skill_content = skill_file.read_text(encoding="utf-8")
        license_content = license_path.read_text(
            encoding="utf-8", errors="replace"
        )
        files = _tree_files(source)
    except (CatalogError, OSError, UnicodeError) as exc:
        raise CatalogError(
            f"Cannot validate AgentRelay bundled Skill {skill_id}: {exc}"
        ) from exc
    declared = _frontmatter_values(skill_content)
    if declared.get("name") != skill_id:
        raise CatalogError(
            f"AgentRelay bundled Skill {skill_id} declares an unexpected name: "
            f"{declared.get('name')!r}"
        )
    if "MIT License" not in license_content:
        raise CatalogError(
            f"AgentRelay bundled Skill {skill_id} is missing its MIT license"
        )
    file_count, digest = _tree_sha256(source, normalize_newlines=True)
    byte_count = sum(
        len(_integrity_bytes(path, normalize_newlines=True))
        for path in files
    )
    expected_files = metadata.get("files")
    expected_bytes = metadata.get("bytes")
    expected_digest = metadata.get("sha256")
    if expected_files is not None and file_count != expected_files:
        raise CatalogError(
            f"AgentRelay bundled Skill {skill_id} file count does not match"
        )
    if expected_bytes is not None and byte_count != expected_bytes:
        raise CatalogError(
            f"AgentRelay bundled Skill {skill_id} byte count does not match"
        )
    if expected_digest is not None and digest != expected_digest:
        raise CatalogError(
            f"AgentRelay bundled Skill {skill_id} checksum does not match"
        )
    return {
        "id": skill_id,
        "name": metadata["name"],
        "version": "",
        "description": metadata["description"],
        "source_type": BUNDLED_SOURCE,
        "source_label": "AgentRelay 内置",
        "installed_in": list(installed_in or []),
        "copy_policy": COPY_ALLOWED,
        "installable": True,
        "install_method": COPY_INSTALL_METHOD,
        "bundled_path": metadata["path"],
        "license_spdx": metadata["license_spdx"],
        "restriction": "",
    }


def normalize_skill_ids(catalog: dict, values: Iterable[object] | None) -> list[str]:
    available = {
        str(entry["id"]): entry for entry in catalog.get("skills", [])
        if isinstance(entry, dict) and entry.get("id")
    }
    normalized: list[str] = []
    for value in values or []:
        if not isinstance(value, str):
            raise CatalogError(f"Invalid Skill selection: {value!r}")
        for token in value.replace("，", ",").split(","):
            skill_id = token.strip()
            if not skill_id:
                continue
            if skill_id == "all":
                normalized.extend(
                    candidate_id
                    for candidate_id, entry in available.items()
                    if skill_is_installable(entry)
                )
                continue
            if skill_id not in available:
                raise CatalogError(f"Unknown Skill: {skill_id}")
            if not skill_is_installable(available[skill_id]):
                reason = str(available[skill_id].get("restriction") or "")
                detail = f"：{reason}" if reason else ""
                raise CatalogError(f"Skill cannot be installed: {skill_id}{detail}")
            if skill_id not in normalized:
                normalized.append(skill_id)
    return normalized


def default_skill_ids(catalog: dict) -> list[str]:
    defaults = [
        value
        for value in catalog.get("defaults") or []
        if isinstance(value, str)
        and skill_is_installable(catalog_skill_entry(catalog, value))
    ]
    return normalize_skill_ids(catalog, defaults)


def reconcile_skill_ids(catalog: dict, values: Iterable[object] | None) -> list[str]:
    """Keep valid selections from saved config without failing the whole page."""

    available = {
        str(entry["id"]): entry
        for entry in catalog.get("skills", [])
        if isinstance(entry, dict) and entry.get("id")
    }
    reconciled: list[str] = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        for token in value.replace("，", ",").split(","):
            skill_id = token.strip()
            if skill_id == "all":
                candidates = available
            elif skill_id in available:
                candidates = {skill_id: available[skill_id]}
            else:
                candidates = {}
            for candidate_id, entry in candidates.items():
                if skill_is_installable(entry) and candidate_id not in reconciled:
                    reconciled.append(candidate_id)
    return reconciled


def _frontmatter_values(content: str) -> dict[str, str]:
    """Read the simple scalar fields needed for Skill selection.

    Skill metadata is YAML frontmatter, but the installer must also run before
    the virtual environment and PyYAML are available. This parser intentionally
    handles only top-level scalar values and ignores nested YAML.
    """

    lines = content.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if key not in {"name", "description", "version"}:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("''", "'")
        values[key] = str(value).strip()
    return values


def _first_heading(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _shorten(text: str, limit: int = 260) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _license_copy_policy(skill_dir: Path) -> tuple[str, str]:
    """Detect explicit no-copy terms without treating every license as equal."""

    try:
        license_files = [
            path
            for path in skill_dir.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and LICENSE_FILE_PATTERN.fullmatch(path.name)
        ]
    except OSError:
        return COPY_UNKNOWN, "无法读取许可证，默认不自动安装"
    if not license_files:
        return COPY_UNKNOWN, ""

    texts: list[str] = []
    for path in sorted(license_files):
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return COPY_UNKNOWN, "许可证读取失败，默认不自动安装"
    normalized = re.sub(r"\s+", " ", "\n".join(texts)).lower()
    if any(marker in normalized for marker in RESTRICTED_COPY_MARKERS):
        return COPY_RESTRICTED, "许可证明确禁止复制或迁移，只能在原安装位置使用"
    if any(marker in normalized for marker in PERMISSIVE_LICENSE_MARKERS):
        return COPY_ALLOWED, ""
    return COPY_UNKNOWN, ""


def skill_is_installable(entry: Mapping[str, object]) -> bool:
    """Return whether the installer may install this Skill for another Agent."""

    return entry.get("installable", True) is not False


def _validate_official_archive_members(
    members: list[tarfile.TarInfo],
) -> None:
    if not members:
        raise CatalogError("Anthropic official Skill archive is empty")
    if len(members) > OFFICIAL_SKILL_ARCHIVE_MAX_MEMBERS:
        raise CatalogError("Anthropic official Skill archive has too many entries")

    total_size = 0
    seen: set[str] = set()
    for member in members:
        raw_name = member.name
        if (
            not raw_name
            or "\x00" in raw_name
            or "\\" in raw_name
            or raw_name.startswith("/")
        ):
            raise CatalogError(
                f"Anthropic official Skill archive has an unsafe path: {raw_name!r}"
            )
        path = PurePosixPath(raw_name)
        if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise CatalogError(
                f"Anthropic official Skill archive has an unsafe path: {raw_name!r}"
            )
        if not (member.isfile() or member.isdir()):
            raise CatalogError(
                "Anthropic official Skill archive contains a link or special file: "
                f"{raw_name}"
            )
        normalized = path.as_posix().casefold()
        if normalized in seen:
            raise CatalogError(
                "Anthropic official Skill archive contains a duplicate entry: "
                f"{raw_name}"
            )
        seen.add(normalized)
        total_size += member.size
        if total_size > OFFICIAL_SKILL_ARCHIVE_MAX_BYTES:
            raise CatalogError("Anthropic official Skill archive is too large")


def _official_archive_root(members: list[tarfile.TarInfo]) -> str:
    roots = {
        PurePosixPath(member.name).parts[0]
        for member in members
        if PurePosixPath(member.name).parts
    }
    if len(roots) != 1:
        raise CatalogError(
            "Anthropic official Skill archive must have exactly one root directory"
        )
    root = next(iter(roots))
    short_commit = OFFICIAL_SKILL_COMMIT[:7]
    prefixes = ("anthropics-skills-", "skills-")
    suffixes = {short_commit, OFFICIAL_SKILL_COMMIT}
    if (
        not root.startswith(prefixes)
        or root.removeprefix(prefixes[0]).removeprefix(prefixes[1])
        not in suffixes
    ):
        raise CatalogError(
            f"Anthropic official Skill archive has an unexpected root: {root!r}"
        )
    return root


def _extract_official_skill(
    archive_path: Path,
    skill_id: str,
    destination: Path,
) -> None:
    metadata = OFFICIAL_SKILLS.get(skill_id)
    if metadata is None:
        raise CatalogError(f"Unknown official Skill: {skill_id}")
    archive_path = Path(archive_path)
    if archive_path.is_symlink() or not archive_path.is_file():
        raise CatalogError(f"Official Skill archive is missing: {archive_path}")
    if archive_path.stat().st_size > OFFICIAL_SKILL_DOWNLOAD_MAX_BYTES:
        raise CatalogError("Official Skill archive exceeds the download size limit")

    destination = Path(destination)
    if destination.exists():
        raise CatalogError(f"Official Skill staging directory already exists: {destination}")
    destination.mkdir(parents=True)

    selected: list[tuple[tarfile.TarInfo, Path]] = []
    total_size = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            _validate_official_archive_members(members)
            prefix = (
                PurePosixPath(_official_archive_root(members))
                / metadata["path"]
            )
            for member in members:
                member_path = PurePosixPath(member.name)
                try:
                    relative = member_path.relative_to(prefix)
                except ValueError:
                    continue
                if not member.isfile() or not relative.parts:
                    continue
                target = destination.joinpath(*relative.parts)
                try:
                    target.resolve().relative_to(destination.resolve())
                except ValueError as exc:
                    raise CatalogError(
                        f"Official Skill path escapes its staging root: {member.name}"
                    ) from exc
                selected.append((member, target))
                total_size += member.size

            if not selected:
                raise CatalogError(
                    f"Official Skill archive is missing {metadata['path']}"
                )
            if len(selected) > OFFICIAL_SKILL_TREE_MAX_FILES:
                raise CatalogError(f"Official Skill {skill_id} has too many files")
            if total_size > OFFICIAL_SKILL_TREE_MAX_BYTES:
                raise CatalogError(f"Official Skill {skill_id} is too large")

            for member, target in selected:
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    raise CatalogError(
                        f"Official Skill file cannot be read: {member.name}"
                    )
                with handle, target.open("xb") as output:
                    remaining = member.size
                    while remaining:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise CatalogError(
                                f"Official Skill file is truncated: {member.name}"
                            )
                        output.write(chunk)
                        remaining -= len(chunk)
                    if handle.read(1):
                        raise CatalogError(
                            f"Official Skill file exceeds declared size: {member.name}"
                        )
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    except (OSError, tarfile.TarError) as exc:
        raise CatalogError(f"Cannot extract official Skill archive: {exc}") from exc
    except CatalogError:
        raise

    skill_file = destination / "SKILL.md"
    license_file_path = destination / str(metadata["license"])
    if (
        skill_file.is_symlink()
        or not skill_file.is_file()
        or license_file_path.is_symlink()
        or not license_file_path.is_file()
    ):
        raise CatalogError(f"Official Skill {skill_id} is missing SKILL.md or its license")
    try:
        skill_content = skill_file.read_text(encoding="utf-8")
        license_content = license_file_path.read_text(
            encoding="utf-8", errors="replace"
        )
    except (OSError, UnicodeError) as exc:
        raise CatalogError(f"Cannot read official Skill {skill_id}: {exc}") from exc
    skill_metadata = _frontmatter_values(skill_content)
    if skill_metadata.get("name") != skill_id:
        raise CatalogError(
            f"Official Skill {skill_id} declares an unexpected name: "
            f"{skill_metadata.get('name')!r}"
        )
    normalized_license = re.sub(r"\s+", " ", license_content).lower()
    if (
        metadata["license_spdx"] == "Apache-2.0"
        and (
            "apache license" not in normalized_license
            or "version 2.0" not in normalized_license
        )
    ):
        raise CatalogError(
            f"Official Skill {skill_id} does not contain the expected Apache-2.0 license"
        )

    files = _tree_files(destination)
    file_count, digest = _tree_sha256(destination)
    byte_count = sum(path.stat().st_size for path in files)
    if (
        file_count != metadata["files"]
        or byte_count != metadata["bytes"]
        or digest != metadata["sha256"]
    ):
        raise CatalogError(
            f"Official Skill {skill_id} content checksum does not match "
            f"commit {OFFICIAL_SKILL_COMMIT}"
        )


def download_official_archive(destination: Path) -> Path:
    """Download the pinned Anthropic Skills archive with size limits."""

    destination = Path(destination)
    if destination.is_symlink():
        raise CatalogError(f"Official Skill download target is a symlink: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    request = urllib.request.Request(
        OFFICIAL_SKILL_ARCHIVE_URL,
        headers={
            "Accept": "application/gzip",
            "User-Agent": "AgentRelay-Skill-Installer/1",
        },
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw_length = response.headers.get("Content-Length")
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError as exc:
                        raise CatalogError(
                            "Official Skill archive returned an invalid Content-Length"
                        ) from exc
                    if content_length > OFFICIAL_SKILL_DOWNLOAD_MAX_BYTES:
                        raise CatalogError(
                            "Official Skill archive exceeds the download size limit"
                        )
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > OFFICIAL_SKILL_DOWNLOAD_MAX_BYTES:
                        raise CatalogError(
                            "Official Skill archive exceeds the download size limit"
                        )
                    output.write(chunk)
                if total == 0:
                    raise CatalogError("Official Skill archive download was empty")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except (urllib.error.URLError, OSError) as exc:
        raise CatalogError(
            f"Cannot download Anthropic official Skills from "
            f"{OFFICIAL_SKILL_ARCHIVE_URL}: {exc}"
        ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


@contextmanager
def official_skill_source(
    skill_id: str,
    archive_source: Path | None = None,
):
    """Yield a verified temporary tree for one pinned official Skill."""

    if skill_id not in OFFICIAL_SKILL_IDS:
        raise CatalogError(f"Unknown official Skill: {skill_id}")
    with tempfile.TemporaryDirectory(
        prefix=f"agentrelay-official-{skill_id}-"
    ) as directory:
        root = Path(directory)
        if archive_source is None:
            archive_path = download_official_archive(root / "anthropics-skills.tar.gz")
        else:
            archive_path = Path(archive_source).expanduser()
            if archive_path.is_symlink() or not archive_path.is_file():
                raise CatalogError(
                    f"Official Skill archive is missing: {archive_path}"
                )
        source = root / "source"
        _extract_official_skill(archive_path, skill_id, source)
        yield source


def _installed_skill_entry(
    skill_dir: Path,
    origin: str,
    project_root: Path,
) -> dict | None:
    skill_id = skill_dir.name
    if skill_id in EXCLUDED_SKILL_IDS or not SKILL_ID_PATTERN.fullmatch(skill_id):
        return None
    try:
        if skill_dir.is_symlink() or not skill_dir.is_dir():
            return None
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_symlink() or not skill_file.is_file():
            return None
    except OSError:
        return None
    if skill_id in OFFICIAL_SKILL_IDS:
        try:
            _tree_files(skill_dir)
        except (CatalogError, OSError):
            return None
        return official_skill_entry(skill_id, installed_in=[origin])
    if skill_id in AGENTRELAY_BUNDLED_SKILL_IDS:
        try:
            entry = agentrelay_bundled_skill_entry(
                project_root,
                skill_id,
                installed_in=[origin],
            )
            source = _safe_relative_path(
                agentrelay_bundled_catalog_root(project_root),
                str(entry["bundled_path"]),
            )
            local_files = _tree_files(skill_dir)
            bundled_files = _tree_files(source)
            local_count, local_digest = _tree_sha256(
                skill_dir,
                normalize_newlines=True,
            )
            bundled_count, bundled_digest = _tree_sha256(
                source,
                normalize_newlines=True,
            )
            local_bytes = sum(
                len(_integrity_bytes(path, normalize_newlines=True))
                for path in local_files
            )
            bundled_bytes = sum(
                len(_integrity_bytes(path, normalize_newlines=True))
                for path in bundled_files
            )
            if (
                local_count != bundled_count
                or local_bytes != bundled_bytes
                or local_digest != bundled_digest
            ):
                return None
            return entry
        except (CatalogError, KeyError, OSError):
            return None
    try:
        _tree_files(skill_dir)
        content = skill_file.read_text(encoding="utf-8")
    except (CatalogError, OSError, UnicodeError):
        return None
    metadata = _frontmatter_values(content)
    copy_policy, restriction = _license_copy_policy(skill_dir)
    installable = copy_policy == COPY_ALLOWED
    if not installable and not restriction:
        restriction = "未找到可确认的许可证；为避免越权复制，默认不安装"
    return {
        "id": skill_id,
        "name": metadata.get("name") or _first_heading(content) or skill_id,
        "version": metadata.get("version", ""),
        "description": _shorten(
            metadata.get("description") or "本机 Agent 已安装的 Skill。"
        ),
        "source_type": INSTALLED_SOURCE,
        "installed_in": [origin],
        "local_source": str(skill_dir),
        "copy_policy": copy_policy,
        "installable": installable,
        "source_label": "本机",
        "install_method": COPY_INSTALL_METHOD,
        "restriction": restriction,
    }


def discover_installed_skills(
    skill_roots: Mapping[str, Path] | Iterable[Path],
    project_root: Path | None = None,
) -> list[dict]:
    """Discover safe, directly installed Skills from one or more Skill roots.

    Invalid directories, symlinks, AgentRelay-managed Skills, and non-Skill
    folders are ignored. Duplicate IDs keep the first source and merge the
    host labels where the Skill was found.
    """

    if isinstance(skill_roots, Mapping):
        roots = list(skill_roots.items())
    else:
        roots = [("本机", Path(root)) for root in skill_roots]
    project_root = Path(project_root or Path(__file__).resolve().parents[1])

    discovered: dict[str, dict] = {}
    for origin, raw_root in roots:
        root = Path(raw_root).expanduser()
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for skill_dir in entries:
            entry = _installed_skill_entry(skill_dir, str(origin), project_root)
            if entry is None:
                continue
            existing = discovered.get(entry["id"])
            if existing is None:
                discovered[entry["id"]] = entry
                continue
            for label in entry["installed_in"]:
                if label not in existing["installed_in"]:
                    existing["installed_in"].append(label)
    return [discovered[skill_id] for skill_id in sorted(discovered)]


def catalog_with_installed_skills(
    project_root: Path,
    skill_roots: Mapping[str, Path] | Iterable[Path],
) -> dict:
    """Merge installed Skills with the bundled fallback catalog."""

    bundled = load_catalog(project_root)
    installed = discover_installed_skills(skill_roots, project_root=project_root)
    bundled_by_id = {
        str(entry["id"]): entry for entry in bundled.get("skills", [])
    }
    installed_ids = {str(entry["id"]) for entry in installed}
    entries: list[dict] = []
    for installed_entry in installed:
        skill_id = str(installed_entry["id"])
        prefix = bundled_by_id.get(skill_id, {})
        if prefix:
            entry = {
                **prefix,
                "source_type": BUNDLED_SOURCE,
                "source_label": "Vue.js AI 内置",
                "installed_in": list(installed_entry.get("installed_in") or []),
                "copy_policy": COPY_ALLOWED,
                "installable": True,
                "install_method": COPY_INSTALL_METHOD,
                "restriction": "",
            }
        else:
            entry = {**prefix, **installed_entry}
        if not entry.get("version"):
            entry["version"] = str(prefix.get("version") or "")
        if prefix:
            entry["bundled_version"] = prefix.get("version", "")
        entries.append(entry)
    for bundled_entry in bundled.get("skills", []):
        if str(bundled_entry["id"]) in installed_ids:
            continue
        entries.append({
            **bundled_entry,
            "source_type": BUNDLED_SOURCE,
            "source_label": "Vue.js AI 内置",
            "installed_in": [],
            "installable": True,
            "install_method": COPY_INSTALL_METHOD,
        })
    catalog_ids = {str(entry["id"]) for entry in entries}
    for skill_id in sorted(OFFICIAL_SKILL_IDS):
        if skill_id not in catalog_ids:
            entries.append(official_skill_entry(skill_id))
    for skill_id in sorted(AGENTRELAY_BUNDLED_SKILL_IDS):
        if skill_id not in catalog_ids:
            entries.append(agentrelay_bundled_skill_entry(project_root, skill_id))

    defaults: list[str] = []
    for skill_id in [*PREFERRED_DEFAULT_SKILL_IDS, *bundled["defaults"]]:
        entry = next(
            (candidate for candidate in entries if candidate["id"] == skill_id),
            None,
        )
        if (
            entry is not None
            and skill_id not in defaults
            and skill_is_installable(entry)
        ):
            defaults.append(skill_id)
    return {
        **bundled,
        "name": "本机已装与内置开发 Skills",
        "description": (
            "自动发现本机 Agent 已安装的 Skill；官方文档 Skills 独立下载，"
            "不复制本机副本。"
        ),
        "skills": entries,
        "defaults": defaults,
        "installed_count": len(installed),
        "selectable_installed_count": sum(
            1 for entry in installed if skill_is_installable(entry)
        ),
        "restricted_count": sum(
            1 for entry in installed if not skill_is_installable(entry)
        ),
        "official_download_count": len(OFFICIAL_SKILL_IDS),
    }


def selection_from_cli(
    catalog: dict,
    raw: str = "",
    no_skills: bool = False,
) -> list[str]:
    if no_skills:
        if str(raw or "").strip():
            raise CatalogError("--skills and --no-skills cannot be used together")
        return []
    if not str(raw or "").strip():
        return default_skill_ids(catalog)
    return normalize_skill_ids(catalog, [raw])


def skill_source_dir(project_root: Path, catalog: dict, skill_id: str) -> Path:
    entry = catalog_skill_entry(catalog, skill_id)
    if entry.get("install_method") == OFFICIAL_DOWNLOAD_METHOD:
        raise CatalogError(
            f"Official Skill must be downloaded independently: {skill_id}"
        )
    if not skill_is_installable(entry):
        reason = str(entry.get("restriction") or "")
        detail = f"：{reason}" if reason else ""
        raise CatalogError(f"Skill cannot be installed: {skill_id}{detail}")
    bundled_path = entry.get("bundled_path")
    if bundled_path:
        root = agentrelay_bundled_catalog_root(project_root)
        source = _safe_relative_path(root, bundled_path)
        if source.is_symlink() or not source.is_dir():
            raise CatalogError(f"Bundled Skill source directory is missing: {source}")
        try:
            _tree_files(source)
        except CatalogError:
            raise
        return source
    local_source = entry.get("local_source")
    if local_source:
        source = Path(str(local_source)).expanduser()
        if source.is_symlink() or not source.is_dir():
            raise CatalogError(f"Installed Skill source is missing: {source}")
        skill_file = source / "SKILL.md"
        if skill_file.is_symlink() or not skill_file.is_file():
            raise CatalogError(f"Installed Skill is missing SKILL.md: {source}")
        try:
            _tree_files(source)
        except CatalogError:
            raise
        return source
    root = catalog_root(project_root)
    source = _safe_relative_path(root, f"skills/{skill_id}")
    if source.is_symlink() or not source.is_dir():
        raise CatalogError(f"Skill source directory is missing: {source}")
    return source


def catalog_skill_entry(catalog: dict, skill_id: str) -> dict:
    for entry in catalog.get("skills", []):
        if isinstance(entry, dict) and entry.get("id") == skill_id:
            return entry
    raise CatalogError(f"Unknown Skill: {skill_id}")


def skill_extra_files(
    project_root: Path,
    catalog: dict,
    skill_id: str,
) -> list[Path]:
    """Return files that are not already inside this Skill's source tree."""

    entry = catalog_skill_entry(catalog, skill_id)
    if entry.get("bundled_path"):
        return []
    if (
        entry.get("source_type") == INSTALLED_SOURCE
        or entry.get("install_method") == OFFICIAL_DOWNLOAD_METHOD
    ):
        return []
    return [license_file(project_root, catalog)]


def license_file(project_root: Path, catalog: dict) -> Path:
    root = catalog_root(project_root)
    path = _safe_relative_path(root, (catalog.get("license") or {}).get("path"))
    if path.is_symlink() or not path.is_file():
        raise CatalogError(f"Skill catalog license is missing: {path}")
    return path


def public_catalog(catalog: dict) -> dict:
    """Return the fields the local Web configurator needs."""

    return {
        "id": catalog["id"],
        "name": catalog["name"],
        "description": catalog.get("description", ""),
        "source": dict(catalog.get("source") or {}),
        "license": str((catalog.get("license") or {}).get("spdx") or ""),
        "defaults": list(catalog.get("defaults") or []),
        "installed_count": int(catalog.get("installed_count") or 0),
        "selectable_installed_count": int(
            catalog.get(
                "selectable_installed_count",
                catalog.get("installed_count") or 0,
            )
        ),
        "restricted_count": int(catalog.get("restricted_count") or 0),
        "official_download_count": int(
            catalog.get("official_download_count") or len(OFFICIAL_SKILL_IDS)
        ),
        "official_sources": [
            {
                "repository": OFFICIAL_SKILL_REPOSITORY,
                "commit": OFFICIAL_SKILL_COMMIT,
                "archive_url": OFFICIAL_SKILL_ARCHIVE_URL,
                "skills": sorted(OFFICIAL_SKILL_IDS),
            }
        ],
        "skills": [
            {
                "id": entry["id"],
                "name": entry["name"],
                "version": entry.get("version", ""),
                "description": entry["description"],
                "source_type": entry.get("source_type", BUNDLED_SOURCE),
                "source_label": str(entry.get("source_label") or ""),
                "installed_in": list(entry.get("installed_in") or []),
                "copy_policy": entry.get("copy_policy", COPY_ALLOWED),
                "installable": skill_is_installable(entry),
                "install_method": entry.get(
                    "install_method", COPY_INSTALL_METHOD
                ),
                "official_repository": str(
                    entry.get("official_repository") or ""
                ),
                "official_commit": str(entry.get("official_commit") or ""),
                "license_spdx": str(entry.get("license_spdx") or ""),
                "restriction": str(entry.get("restriction") or ""),
            }
            for entry in catalog.get("skills", [])
        ],
    }
