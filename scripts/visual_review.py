"""Visual acceptance for UI scope boundaries and honest non-visual fallback."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any, Iterable

try:
    from . import verification_state as _verification_state
except ImportError:
    import verification_state as _verification_state

VALID_MODES = {"native", "agent-image", "unavailable"}
UNKNOWN_OBSERVATION = re.compile(r"无法判断|不确定|没有检查|未检查")
GENERIC_OBSERVATION = re.compile(
    r"^(?:符合|正常|无变化|没问题|无异常|未见异常|通过|已通过|检查通过|验证通过|"
    r"符合预期|看起来正常|已完成|已检查|ok|pass|passed)[。.!！\s]*$",
    re.IGNORECASE,
)
VISUAL_DETAIL = re.compile(
    r"\d|px|像素|宽|高|位置|尺寸|间距|对齐|空白|留白|溢出|遮挡|裁切|"
    r"颜色|边框|阴影|圆角|标题|按钮|表格|行|列|文字|文本|内容|图标|"
    r"布局|可见|隐藏|截断|重叠|rect|computed",
    re.IGNORECASE,
)
CONCRETE_OBSERVATION = re.compile(
    r"\d|px|像素|rgb(?:a)?\s*\(|#[0-9a-f]{3,8}\b|"
    r"rect|bounding\s*box|computed|before\s*/?\s*after|修改前|修改后|"
    r"['\"“”‘’][^'\"“”‘’]{2,}['\"“”‘’]",
    re.IGNORECASE,
)
ATTESTATION_DETAIL = re.compile(
    r"内容|文本|布局|间距|对齐|空白|留白|溢出|遮挡|裁切|位置|尺寸|颜色|边框"
)
VISUAL_CLAIM_PATTERNS = (
    r"(?:我|本人)?(?:亲自|实际)(?:查看|检查|核对|评审)(?:了|过)?"
    r"[^\n。！？；;]{0,40}(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式)?",
    r"(?:我|本人)?(?:已经|已|逐项|逐一)(?:查看|检查|核对|评审)(?:了|过)?"
    r"[^\n。！？；;]{0,40}(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式)",
    r"(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式)"
    r"[^\n。！？；;]{0,50}(?:人工|亲自|实际|已经|已|逐项|逐一)"
    r"(?:查看|检查|核对|评审)(?:了|过)?",
    r"(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式)"
    r"[^\n。！？；;]{0,50}(?:已检查|已查看|已核对|已评审|检查通过|验证通过)",
    r"(?:(?:全部|所有|每个|每一|逐项|逐一|四象限|1\s*[-–—~至到]\s*4)"
    r"[^\n。！？；;]{0,50}(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式)"
    r"[^\n。！？；;]{0,50}(?:检查|验证|评审|操作)"
    r"|(?:全部|所有|每个|每一|逐项|逐一|四象限|1\s*[-–—~至到]\s*4)"
    r"[^\n。！？；;]{0,50}(?:检查|验证|评审|操作)"
    r"[^\n。！？；;]{0,50}(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式))"
    r"[^\n。！？；;]{0,20}(?:通过|完成|无问题|还原|放大)?",
    r"(?:查看|检查|验证|评审|操作)(?:了)?[^\n。！？；;]{0,40}"
    r"(?:全部|所有|每个|每一|逐项|逐一|四象限|1\s*[-–—~至到]\s*4)"
    r"[^\n。！？；;]{0,40}(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式)",
)
HONEST_LIMITATION_PATTERNS = (
    r"(?:只|仅)[^\n。！？]{0,30}(?:截图|图片|象限|表格|弹窗|功能)",
    r"(?:其余|另外|其他)[^\n。！？]{0,30}(?:没有|未|仅)[^\n。！？]{0,20}(?:截图|人工查看|视觉检查)",
    r"(?:没有|未)[^\n。！？]{0,20}(?:逐张|逐项|逐页|人工查看|视觉检查)",
)
NEGATED_VISUAL_CLAIM = re.compile(
    r"(?:没有|没|未|尚未|无法|不能|并未|不是|并非|不代表|不等同于|不属于|"
    r"不再声明|不标记|不能标记|不视为|不能视为)"
    r"[^。！？\n；;]{0,80}"
    r"(?:(?:人工|亲自|实际|已经|已|逐项|逐一)?(?:查看|检查|核对|评审|操作)|"
    r"(?:视觉|页面|截图|图片|UI)[^。！？\n；;]{0,30}(?:验收|验证|检查|通过|完成)|"
    r"(?:验收|验证|检查)[^。！？\n；;]{0,30}(?:通过|完成|记录|结果|认证|证书)|"
    r"(?:通过|完成))",
    re.IGNORECASE,
)
VISUAL_CLAIM_SEGMENT_SPLIT = re.compile(r"[，,]+|但是|然而|不过|可是|(?<!不)但")
FUTURE_VISUAL_CLAIM = re.compile(
    r"(?:将|将要|将会|预计|计划|准备|拟|待|后续|下一步|确认后|实施后|修改后|完成后)"
    r"[^。！？\n；;]{0,80}"
    r"(?:截图|图片|页面|界面|UI|象限|表格|弹窗|功能|按钮|布局|样式|"
    r"查看|检查|核对|评审|验证)",
    re.IGNORECASE,
)
COMPLETED_VISUAL_CLAIM = re.compile(
    r"(?:已经|已|亲自|实际|逐项|逐一)(?:查看|检查|核对|评审)(?:了|过)?|"
    r"(?:查看|检查|核对|评审)(?:了|过)|"
    r"(?:已检查|已查看|已核对|已评审|检查通过|验证通过)",
    re.IGNORECASE,
)
FIRST_PERSON_VISUAL_CLAIM = re.compile(r"(?:我|本人|我们)[^。！？\n；;]{0,20}(?:查看|检查|核对|评审)", re.IGNORECASE)
META_VISUAL_DISCUSSION = re.compile(
    r"是否|为什么|为何|什么|怎么|如何|用于|用来|作用|规则|门禁|Hook|"
    r"触发|误判|识别|判断|判定|语境|示例|例如|检测到|"
    r"肯定完成声明|否定声明|短语|表述|措辞|"
    r"(?:吗|呢|么)[。.!！?？\s]*$",
    re.IGNORECASE,
)
QUOTED_SPAN = re.compile(r"“[^”\n]{0,240}”|‘[^’\n]{0,240}’|\"[^\"\n]{0,240}\"|'[^'\n]{0,240}'")
NUMBER_WORDS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
SCREENSHOT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
SAFE_TEMP_SCREENSHOT_NAME = re.compile(
    r"(?:agent[-_]?relay|screen[-_]?shot|capture|snapshot|review|"
    r"verif(?:y|ication)|evidence|check)",
    re.IGNORECASE,
)
SAFE_TEMP_SCREENSHOT_TARGET = re.compile(
    r"(?:^|[-_])(?:dcm|dc-query)(?:[-_]|$)",
    re.IGNORECASE,
)


def state_path(home: Path, session_id: str) -> Path:
    return _verification_state.state_path(home, session_id)


def read_state(home: Path, session_id: str) -> dict[str, Any]:
    return _verification_state.read_state(home, session_id)


def write_state(home: Path, session_id: str, state: dict[str, Any]) -> None:
    _verification_state.write_state(home, session_id, state)


def _valid_png(data: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 45:
        return False
    offset = 8
    saw_header = saw_data = saw_end = False
    try:
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            checksum = data[offset + 8 + length:offset + 12 + length]
            if len(payload) != length or len(checksum) != 4:
                return False
            if zlib.crc32(kind + payload) & 0xFFFFFFFF != int.from_bytes(checksum, "big"):
                return False
            if kind == b"IHDR":
                saw_header = length == 13 and int.from_bytes(payload[:4], "big") > 0 and int.from_bytes(payload[4:8], "big") > 0
            elif kind == b"IDAT" and payload:
                saw_data = True
            elif kind == b"IEND":
                saw_end = True
                break
            offset += length + 12
    except (ValueError, IndexError):
        return False
    return saw_header and saw_data and saw_end


def _validate_screenshots(paths: Iterable[str]) -> list[str]:
    result = []
    for value in paths:
        raw = Path(value).expanduser()
        if raw.is_symlink():
            raise ValueError(f"截图路径不能是符号链接：{value}")
        path = raw.resolve()
        if not path.is_file() or path.stat().st_size == 0 or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError(f"真实截图不存在或无效：{value}")
        data = path.read_bytes()
        valid_image = _valid_png(data) if path.suffix.lower() == ".png" else (
            data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9") if path.suffix.lower() in {".jpg", ".jpeg"}
            else data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) > 20
        )
        if not valid_image:
            raise ValueError(f"真实截图不是可解码的图片内容：{value}")
        result.append(str(path))
    if not result:
        raise ValueError("至少需要一张覆盖相关页面/容器的真实截图")
    return result


def _normalize_target_items(target: str, target_items: Iterable[str] | None) -> list[str]:
    values = list(target_items) if target_items is not None else [target]
    result = list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    if not result:
        raise ValueError("视觉认证必须至少声明一个具体目标项")
    return result


def _normalize_item_screenshots(
    items: list[str],
    images: list[str],
    item_screenshots: dict[str, str] | Iterable[tuple[str, str]] | None,
) -> dict[str, str]:
    if item_screenshots is None:
        if len(items) != 1:
            raise ValueError("多个视觉目标必须逐项绑定截图，不能共用一张截图代表全部目标")
        return {items[0]: images[0]}
    mapping = dict(item_screenshots)
    normalized = {}
    for item, value in mapping.items():
        name = str(item).strip()
        if not name or not str(value).strip():
            continue
        raw = Path(value).expanduser()
        if raw.is_symlink():
            raise ValueError(f"目标项截图不能是符号链接：{value}")
        normalized[name] = str(raw.resolve())
    if set(normalized) != set(items):
        missing = [item for item in items if item not in normalized]
        extra = [item for item in normalized if item not in items]
        detail = []
        if missing:
            detail.append("缺少目标截图：" + "、".join(missing))
        if extra:
            detail.append("存在未声明目标的截图：" + "、".join(extra))
        raise ValueError("逐项目标截图与目标列表不一致；" + "；".join(detail))
    recorded = set(images)
    for item, path in normalized.items():
        if path not in recorded:
            raise ValueError(f"目标项截图不在当前浏览器验证结果中：{item}")
    return normalized


def _screenshot_digest(path: str) -> str:
    """Hash decoded pixels so metadata-only copies still count as duplicates."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a required dependency
        raise ValueError("缺少 Pillow，无法计算截图像素哈希") from exc

    with Image.open(path) as source:
        image = source.convert("RGBA")
        digest = hashlib.sha256()
        digest.update(f"{image.width}x{image.height}\0".encode("ascii"))
        digest.update(image.tobytes())
        return digest.hexdigest()


def _screenshot_layout_findings(path: str) -> list[dict[str, Any]]:
    """Detect large near-white bands that usually indicate missing UI content."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a required dependency
        raise ValueError("缺少 Pillow，无法分析截图的布局空白") from exc

    with Image.open(path) as source:
        image = source.convert("RGB")
    original_width, original_height = image.size
    if original_width <= 0 or original_height <= 0:
        raise ValueError(f"截图尺寸无效：{path}")
    if original_width > 720:
        resized_height = max(1, round(original_height * 720 / original_width))
        image = image.resize((720, resized_height))
    width, height = image.size
    pixels = image.load()
    light_ratios = []
    run_ratios = []
    for y in range(height):
        light = 0
        current_run = 0
        longest_run = 0
        for x in range(width):
            red, green, blue = pixels[x, y]
            if min(red, green, blue) >= 238 and max(red, green, blue) - min(red, green, blue) <= 10:
                light += 1
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 0
        light_ratios.append(light / width)
        run_ratios.append(longest_run / width)

    minimum_band_height = max(80, round(height * 0.16))
    findings = []
    start = None
    for index in range(height + 1):
        ratio = light_ratios[index] if index < height else 0.0
        run_ratio = run_ratios[index] if index < height else 0.0
        has_blank_band = ratio >= 0.72 or run_ratio >= 0.55
        if has_blank_band and start is None:
            start = index
        elif not has_blank_band and start is not None:
            end = index
            band_height = end - start
            average_width = sum(light_ratios[start:end]) / max(1, band_height)
            average_run = sum(run_ratios[start:end]) / max(1, band_height)
            if band_height >= minimum_band_height and (
                average_width >= 0.75 or average_run >= 0.60
            ):
                top = round(start * original_height / height)
                bottom = round(end * original_height / height)
                findings.append({
                    "kind": "large_blank_band",
                    "blocking": True,
                    "path": str(Path(path).resolve()),
                    "sha256": _screenshot_digest(path),
                    "top": top,
                    "bottom": bottom,
                    "height_ratio": round(band_height / height, 4),
                    "width_ratio": round(average_width, 4),
                    "contiguous_width_ratio": round(average_run, 4),
                    "actual": (
                        f"{Path(path).name} 的 y={top}-{bottom}px 是连续近白横带，"
                        f"占截图高度 {band_height / height:.0%}、整行宽度 {average_width:.0%}、"
                        f"连续白色区域宽度 {average_run:.0%}；"
                        "疑似内容区大块空白或内容缺失"
                    ),
                })
            start = None
    return findings


def _layout_review(paths: Iterable[str]) -> dict[str, Any]:
    screenshots = []
    findings = []
    for path in dict.fromkeys(paths):
        image_findings = _screenshot_layout_findings(path)
        findings.extend(image_findings)
        screenshots.append({
            "path": str(Path(path).resolve()),
            "sha256": _screenshot_digest(path),
            "findings": image_findings,
        })
    return {
        "version": 1,
        "status": "blocked" if findings else "clear",
        "screenshots": screenshots,
        "findings": findings,
    }


def _observation_quality_reason(observations: dict[str, str], required: set[str]) -> str | None:
    for item in sorted(required):
        value = str(observations.get(item) or "").strip()
        if not value:
            return f"缺少逐项观察：{item}"
        if len(value) < 6 or GENERIC_OBSERVATION.fullmatch(value) or not VISUAL_DETAIL.search(value):
            return (
                f"观察过于笼统，不能证明实际看过截图：{item}。"
                "必须写出可见内容、位置尺寸、间距对齐、空白溢出、颜色或遮挡等具体结果"
            )
        if not CONCRETE_OBSERVATION.search(value):
            return (
                f"观察缺少可核对的具体证据：{item}。"
                "至少写出数值/尺寸、颜色值、rect/computed、修改前后状态或引号内的实际文本，"
                "不能只写“符合”“正常”或元素名称"
            )
    return None


def _attestation_reason(attestation: str) -> str | None:
    value = attestation.strip()
    if len(value) < 16:
        return "自评审签名过短，必须说明实际核对的可见内容和布局范围"
    if not re.search(r"逐项|逐一|每个|全部", value) or not re.search(r"截图|页面|界面|视觉", value):
        return "自评审签名必须明确说明已逐项读取截图并对照页面或界面"
    if len(set(ATTESTATION_DETAIL.findall(value))) < 2:
        return "自评审签名必须至少覆盖两类视觉检查，例如内容、布局、间距、对齐、空白或溢出"
    return None


def _number_value(value: str) -> int:
    if value.isdigit():
        return int(value)
    return NUMBER_WORDS.get(value, 0)


def _claimed_scope_count(message: str) -> int:
    """Extract explicit multi-target counts from a completion claim."""
    text = str(message or "")
    counts: list[int] = []
    for match in re.finditer(
        r"(\d+)\s*[-–—~至到]\s*(\d+)\s*(?:个|张|项|处|块|组)?\s*"
        r"(?:象限|表格|表|弹窗|页面|模块|功能|目标|元素)",
        text,
    ):
        counts.append(max(int(match.group(1)), int(match.group(2))))
    for match in re.finditer(
        r"([一二两三四五六七八九十]|\d+)\s*(?:个|张|项|处|块|组)?\s*"
        r"(?:象限|表格|表|弹窗|页面|模块|功能|目标|元素)",
        text,
    ):
        counts.append(_number_value(match.group(1)))
    return max(counts, default=0)


def visual_claim(message: str) -> bool:
    """Return whether any non-negated clause asserts visual or UI interaction coverage."""
    text = str(message or "")
    clauses = [part.strip() for part in re.split(r"[。！？\n；;]+", text) if part.strip()]
    for clause in clauses:
        asserted = False
        for part in VISUAL_CLAIM_SEGMENT_SPLIT.split(clause):
            part = part.strip()
            if not part:
                continue
            if NEGATED_VISUAL_CLAIM.search(part):
                # A later retraction supersedes an earlier concession, while a
                # later positive observation supersedes an earlier limitation.
                asserted = False
                continue
            if (
                FUTURE_VISUAL_CLAIM.search(part)
                and not COMPLETED_VISUAL_CLAIM.search(part)
            ):
                continue
            if (
                META_VISUAL_DISCUSSION.search(part)
                and not FIRST_PERSON_VISUAL_CLAIM.search(QUOTED_SPAN.sub("", part))
            ):
                continue
            if any(re.search(pattern, part, re.I) for pattern in VISUAL_CLAIM_PATTERNS):
                asserted = True
        if asserted:
            return True
    return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_safe_explicit_screenshot(path: Path, evidence_roots: Iterable[Path]) -> bool:
    if path.suffix.lower() not in SCREENSHOT_SUFFIXES or not path.is_file():
        return False
    if any(_is_within(path, root) for root in evidence_roots):
        return True
    if not (
        SAFE_TEMP_SCREENSHOT_NAME.search(path.name)
        or SAFE_TEMP_SCREENSHOT_TARGET.search(path.name)
        or SAFE_TEMP_SCREENSHOT_TARGET.search(path.parent.name)
    ):
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
    return any(_is_within(path, root) for root in temp_roots)


def cleanup_screenshots(home: Path, session_id: str,
                        explicit_screenshots: Iterable[str] = ()) -> list[str]:
    """Delete registered or explicitly listed disposable screenshots only."""
    explicit = []
    for value in explicit_screenshots:
        raw = Path(value).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise ValueError(f"截图清理路径必须是绝对路径且不能是符号链接：{value}")
        explicit.append(raw.resolve())

    def update(state: dict[str, Any]) -> list[str]:
        verification = state.get("verification") or {}
        review = state.get("visual_review") or {}
        evidence = state.get("evidence_root") or str(state_path(home, session_id).with_suffix("") / "evidence")
        evidence_roots = [state_path(home, session_id).parent, Path(evidence).expanduser().resolve()]
        registered = set()
        for value in set(verification.get("screenshots") or []) | set(review.get("screenshots") or []):
            raw = Path(value).expanduser()
            if raw.is_symlink():
                raise ValueError(f"登记截图不能是符号链接：{value}")
            registered.add(raw.resolve())
        targets = registered | set(explicit)
        for path in targets:
            if path not in registered and not _is_safe_explicit_screenshot(path, evidence_roots):
                raise ValueError(
                    "只能清理当前验证登记的截图，或系统临时目录/AgentRelay 证据目录内"
                    f"名称明确的普通图片：{path}"
                )
            if path.exists() and not path.is_file():
                raise ValueError(f"截图清理目标不是普通文件：{path}")

        certification = review.get("certification") if isinstance(review, dict) else {}
        layout = certification.get("layout_analysis") if isinstance(certification, dict) else {}
        analyzed = {
            str(Path(item.get("path") or "").expanduser().resolve()): str(item.get("sha256") or "")
            for item in (layout.get("screenshots") or []) if isinstance(item, dict) and item.get("path")
        }
        if registered and analyzed:
            for path in registered:
                if not path.exists():
                    continue
                expected_digest = analyzed.get(str(path))
                if not expected_digest:
                    raise ValueError(f"截图未纳入当前视觉认证，不能当作已验收截图清理：{path}")
                if _screenshot_digest(str(path)) != expected_digest:
                    raise ValueError(f"截图在视觉审查后发生变化，原认证已失效：{path}")

        removed = []
        for path in targets:
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            except OSError:
                continue
        remaining = [str(path) for path in targets if path.exists()]
        if remaining:
            raise ValueError("截图清理未完成，文件仍存在：" + "、".join(sorted(remaining)))
        verification["screenshots"] = []
        verification["screenshots_deleted"] = True
        state["verification"] = verification
        if review:
            review["screenshots"] = []
            review["screenshots_deleted"] = True
            state["visual_review"] = review
        state["cleanup_completed"] = True
        return removed

    return _verification_state.mutate_state(home, session_id, update)


def record_review(home: Path, session_id: str, *, mode: str, screenshots: Iterable[str],
                  target: str, unchanged_checks: Iterable[str], passed: bool,
                  summary: str, model: str = "", observations: dict[str, str] | None = None,
                  target_items: Iterable[str] | None = None,
                  item_screenshots: dict[str, str] | Iterable[tuple[str, str]] | None = None,
                  self_reviewed: bool = False, attestation: str = "") -> dict[str, Any]:
    """Record itemized visual review with a verifiable self-review certificate."""
    if mode not in VALID_MODES:
        raise ValueError("视觉审查方式必须是 native、agent-image 或 unavailable")
    checks = [str(item).strip() for item in unchanged_checks if str(item).strip()]
    items = _normalize_target_items(target, target_items)
    scope = target.strip() or "、".join(items)
    if not scope or not checks:
        raise ValueError("必须同时记录目标和非目标副作用检查，不能只检查被修改元素")
    images = _validate_screenshots(screenshots)
    if item_screenshots is None and len(items) > 1 and not passed:
        item_paths: dict[str, str] = {}
    else:
        item_paths = _normalize_item_screenshots(items, images, item_screenshots)
    if mode == "unavailable" and passed:
        raise ValueError("无视觉能力不能声称视觉审查通过，必须 user_review_required")
    observations = {str(key).strip(): str(value).strip() for key, value in (observations or {}).items()}
    if len(items) == 1 and target_items is None and "target" in observations and items[0] not in observations:
        observations[items[0]] = observations.pop("target")
    required = {*items, *checks}
    if passed and (set(observations) != required or any(not value for value in observations.values())):
        raise ValueError("通过视觉审查必须提供每个目标项和每个目标外检查项的逐项观察结果")
    if passed and mode in {"native", "agent-image"} and any(
        UNKNOWN_OBSERVATION.search(value) for value in observations.values()
    ):
        raise ValueError("无法判断的观察不能登记为视觉通过")
    if passed and mode in {"native", "agent-image"}:
        observation_reason = _observation_quality_reason(observations, required)
        if observation_reason:
            raise ValueError(observation_reason)
    attestation = attestation.strip()
    if passed and mode == "native":
        if not self_reviewed:
            raise ValueError("原生视觉通过必须由主 Agent 明确确认已完成逐项自评审")
        attestation_reason = _attestation_reason(attestation)
        if attestation_reason:
            raise ValueError(attestation_reason)
    if passed and len(items) > 1:
        digests = [_screenshot_digest(path) for path in item_paths.values()]
        if len(set(digests)) != len(digests):
            raise ValueError("多个目标项不能复用同一张截图，必须逐项提供独立截图")
    item_digests = {item: _screenshot_digest(path) for item, path in item_paths.items()}
    layout_analysis = _layout_review(images)
    if passed and layout_analysis["status"] != "clear":
        findings = "；".join(
            str(item.get("actual") or "疑似大块空白") for item in layout_analysis["findings"][:3]
        )
        raise ValueError(
            "截图布局分析发现疑似大块空白，不能登记视觉通过；"
            f"请修复后重新截图，或如实改为用户复查。{findings}"
        )
    reviewer = "agent-self-review" if mode == "native" else mode
    certification = {
        "version": 1,
        "status": "passed" if passed else "incomplete",
        "reviewer": reviewer,
        "self_reviewed": bool(self_reviewed),
        "attestation": attestation,
        "reviewed_items": items,
        "item_screenshots": item_paths,
        "item_screenshot_sha256": item_digests,
        "layout_analysis": layout_analysis,
        "coverage": {
            "targets": len(items),
            "screenshots": len(images),
            "unchanged_checks": len(checks),
        },
        "signed_at": time.time(),
    }
    review = {
        "mode": mode, "status": "passed" if passed else "user_review_required",
        "target": scope, "target_items": items, "unchanged_checks": checks,
        "screenshots": images, "summary": summary.strip(), "model": model,
        "observations": observations, "item_screenshots": item_paths,
        "certification": certification, "checked_at": time.time(),
    }
    def update(state: dict[str, Any]) -> dict[str, Any]:
        verification = state.get("verification") or {}
        recorded = {
            str(Path(value).expanduser().resolve())
            for value in verification.get("screenshots") or []
        }
        if set(images) - recorded:
            raise ValueError(
                "视觉审查截图必须来自当前浏览器验证结果；只能使用 verify_page.py "
                "写入 verification.screenshots 的原始路径"
            )
        if verification.get("status") != "passed":
            raise ValueError("真实浏览器检查未通过；请先重新运行 verify_page.py 并确认检查成功")
        analyzed = {
            str(item.get("path") or ""): str(item.get("sha256") or "")
            for item in (layout_analysis.get("screenshots") or []) if isinstance(item, dict)
        }
        for path in images:
            if analyzed.get(path) != _screenshot_digest(path):
                raise ValueError(f"截图在视觉审查写入前发生变化，必须重新读取后审查：{path}")
        state["visual_review"] = review
        return review

    return _verification_state.mutate_state(home, session_id, update)


def record_nonvisual_review(home: Path, session_id: str, *, target: str,
                            unchanged_checks: Iterable[str], summary: str) -> dict[str, Any]:
    """Non-visual evidence comes from a real browser, never a free-text claim."""
    checks = [str(item).strip() for item in unchanged_checks if str(item).strip()]
    if not target.strip() or not summary.strip():
        raise ValueError("无视觉也必须明确目标并说明需要用户复查")
    def update(state: dict[str, Any]) -> dict[str, Any]:
        verification = state.get("verification") or {}
        evidence = verification.get("nonvisual_checks") or []
        if verification.get("status") != "passed" or verification.get("browser") != "chromium" or not evidence:
            raise ValueError("无视觉必须提供真实 Chromium 的结构化检查，不能只写已检查")
        required = {"dom", "console", "diff", "interaction", *checks}
        by_name = {str(item.get("check")): item for item in evidence if isinstance(item, dict)}
        if any(name not in by_name for name in required) or any(
            not by_name[name].get("passed") or not str(by_name[name].get("actual") or "").strip()
            for name in required
        ):
            raise ValueError("无视觉结构化检查必须覆盖 DOM/布局、控制台、真实 diff、交互及全部目标外项目")
        review = {"mode": "unavailable", "status": "user_review_required",
                  "target": target.strip(), "unchanged_checks": checks,
                  "summary": summary.strip(), "screenshots": [], "nonvisual_checks": evidence,
                  "screenshots_deleted": True, "checked_at": time.time()}
        state["visual_review"] = review
        state["cleanup_completed"] = True
        return review

    return _verification_state.mutate_state(home, session_id, update)


def review_with_agent_image(
    home: Path,
    session_id: str,
    *,
    screenshots: list[str],
    target: str,
    unchanged_checks: list[str],
    target_items: Iterable[str] | None = None,
    item_screenshots: dict[str, str] | Iterable[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Ask the configured vision provider, never simulate a visual response."""
    checks = [str(item).strip() for item in unchanged_checks if str(item).strip()]
    items = _normalize_target_items(target, target_items)
    script = home / "skills" / "agent-image" / "scripts" / "agent_image.py"
    if not script.is_file():
        return record_review(home, session_id, mode="unavailable", screenshots=screenshots,
                             target=target, unchanged_checks=checks, passed=False,
                             summary="agent-image 未安装；当前模型无视觉，需用户复查",
                             target_items=items, item_screenshots=item_screenshots)
    python = Path(os.environ.get("AGENTRELAY_PYTHON", ""))
    if not python.is_file():
        python = home / "agentrelay-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    question = ("请检查这次 UI 修改的视觉边界。逐项输出“项目名称: 通过/不通过: 实际观察”。\n"
                + "\n".join(items) + "\n" + "\n".join(checks)
                + "\n每项都必须明确回答；不知道就写无法判断，不能编造或只说整体没问题。")
    try:
        completed = subprocess.run([str(python), str(script), *screenshots, "--question", question],
                                   capture_output=True, text=True, timeout=120, check=False)
        output = completed.stdout.strip()
        failed = completed.returncode != 0 or "【AgentImage 注】" in output or "没有任何支持图片识别的模型可用" in output
        observations = {}
        for segment in re.split(r"[\n;；]+", output):
            match = re.match(r"\s*(?:[-*]\s*)?([^:：]+)[:：]\s*(通过|不通过|无法判断)\s*[:：]?\s*(.*)", segment.strip())
            if match:
                observations[match.group(1).strip()] = match.group(3).strip() or match.group(2)
        expected = {*items, *checks}
        if target_items is None and len(items) == 1 and "target" in observations and items[0] not in observations:
            observations[items[0]] = observations.pop("target")
        if set(observations) != expected or any(not value for value in observations.values()) or re.search(r"不通过|无法判断", output):
            failed = True
        return record_review(home, session_id, mode="unavailable" if failed else "agent-image",
                             screenshots=screenshots, target=target,
                             unchanged_checks=checks, passed=not failed,
                             summary=output or completed.stderr.strip() or "视觉 Provider 无返回",
                             model="agent-image" if not failed else "", observations=observations,
                             target_items=items, item_screenshots=item_screenshots,
                             attestation="agent-image 已逐项读取截图并返回目标观察")
    except (OSError, subprocess.SubprocessError) as exc:
        return record_review(home, session_id, mode="unavailable", screenshots=screenshots,
                             target=target, unchanged_checks=checks, passed=False,
                             summary=f"视觉 Provider 调用失败：{exc}；需用户复查",
                             target_items=items, item_screenshots=item_screenshots)


def capture_invariants(page, selectors: Iterable[str]) -> list[dict[str, Any]]:
    """Capture positions, dimensions and styles of elements that must not change."""
    results = []
    for selector in selectors:
        snapshot = capture_target(page, selector, ["display", "color", "position", "width", "height"])
        results.append({"selector": selector, "before": snapshot})
    return results


def verify_invariants(before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    after_by_selector = {item["selector"]: item for item in after}
    results = []
    for item in before:
        selector = item["selector"]
        current = after_by_selector.get(selector)
        if not current:
            results.append({"check": selector, "passed": False, "actual": "目标外元素不存在或不可见"})
            continue
        expected_rect = item["before"]["rect"]
        actual_rect = current["before"]["rect"]
        expected_styles = item["before"].get("styles") or {}
        actual_styles = current["before"].get("styles") or {}
        passed = expected_rect == actual_rect and expected_styles == actual_styles
        results.append({"check": selector, "passed": passed,
                        "actual": f"before_rect={expected_rect}, after_rect={actual_rect}, before_styles={expected_styles}, after_styles={actual_styles}"})
    return results


def record_invariant_evidence(home: Path, session_id: str, results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(results)
    if not values or any(not item.get("passed") for item in values):
        raise ValueError("目标外检查存在失败项，不能记录为无副作用")
    def update(state: dict[str, Any]) -> None:
        state["unchanged_results"] = values

    _verification_state.mutate_state(home, session_id, update)
    return {"unchanged_results": values, "unchanged_verified": True}


def capture_target(page, selector: str, properties: Iterable[str]) -> dict[str, Any]:
    """Capture the actual target element, not just the whole page."""
    if not selector.strip():
        raise ValueError("target_selector 不能为空，不能用整页截图代替目标验收")
    locator = page.locator(selector).first
    locator.wait_for(state="visible")
    styles = locator.evaluate("""(element, properties) => Object.fromEntries(properties.map(
        property => [property, getComputedStyle(element)[property]]))""", list(properties))
    box = locator.bounding_box() or {}
    return {"selector": selector, "styles": styles,
            "rect": {key: box.get(key, 0) for key in ("x", "y", "width", "height")}}


def target_changes(before: dict[str, Any], after: dict[str, Any],
                   expected: dict[str, Any]) -> dict[str, bool]:
    """Each expected property must really change as requested."""
    if before.get("selector") != after.get("selector"):
        raise ValueError("修改前后目标选择器不一致，无法证明目标元素修改成功")
    if not expected:
        raise ValueError("expected_changes 不能为空，必须明确目标属性与预期值")
    result = {}
    for prop, expected_value in expected.items():
        actual = after.get("styles", {}).get(prop)
        if isinstance(expected_value, str) and expected_value.startswith("!="):
            result[prop] = actual != before.get("styles", {}).get(prop) and actual != expected_value[2:]
        else:
            result[prop] = str(actual) == str(expected_value)
    return result


def target_observed_change(before: dict[str, Any], after: dict[str, Any],
                           expected: dict[str, Any]) -> bool:
    """Require at least one declared target property to differ from the baseline."""
    before_styles = before.get("styles") if isinstance(before.get("styles"), dict) else {}
    after_styles = after.get("styles") if isinstance(after.get("styles"), dict) else {}
    return any(after_styles.get(prop) != before_styles.get(prop) for prop in expected)


def build_target_verification(before: dict[str, Any], after: dict[str, Any],
                              expected: dict[str, Any],
                              unchanged_checks: Iterable[str]) -> dict[str, Any]:
    checks = [str(item).strip() for item in unchanged_checks if str(item).strip()]
    if not checks:
        raise ValueError("目标验收也必须包含目标外副作用检查")
    results = target_changes(before, after, expected)
    observed_change = target_observed_change(before, after, expected)
    passed = all(results.values()) and observed_change
    return {
        "selector": before["selector"], "expected_changes": expected,
        "before": before, "after": after, "checks": results,
        "observed_change": observed_change,
        "unchanged_checks": checks, "target_verified": passed,
        "status": "passed" if passed else "target_not_satisfied",
        "checked_at": time.time(),
    }


def record_target_verification(home: Path, session_id: str, *, before: dict[str, Any],
                               after: dict[str, Any], expected: dict[str, Any],
                               unchanged_checks: Iterable[str]) -> dict[str, Any]:
    target_review = build_target_verification(
        before, after, expected, unchanged_checks,
    )
    def update(state: dict[str, Any]) -> None:
        state["target_review"] = target_review

    _verification_state.mutate_state(home, session_id, update)
    return target_review


def _review_certificate_reason(review: dict[str, Any], message: str = "") -> str | None:
    certification = review.get("certification")
    if not isinstance(certification, dict) or certification.get("status") != "passed":
        return "缺少逐项目标视觉认证，不能把截图、交互或尺寸测量等同于完整视觉检查。"
    items = [
        str(item).strip() for item in review.get("target_items") or [] if str(item).strip()
    ]
    checks = [
        str(item).strip() for item in review.get("unchanged_checks") or [] if str(item).strip()
    ]
    observations = review.get("observations") if isinstance(review.get("observations"), dict) else {}
    required = {*items, *checks}
    if not items or not checks or set(observations) != required or any(
        not str(observations.get(item) or "").strip() for item in required
    ):
        return "视觉认证没有覆盖全部目标项和目标外检查的逐项观察。"
    observation_reason = _observation_quality_reason(observations, required)
    if observation_reason:
        return observation_reason
    reviewed = [
        str(item).strip() for item in certification.get("reviewed_items") or []
        if str(item).strip()
    ]
    if reviewed != items:
        return "视觉认证登记的目标项与实际审查目标不一致。"
    item_screenshots = certification.get("item_screenshots")
    digests = certification.get("item_screenshot_sha256")
    review_item_screenshots = review.get("item_screenshots")
    if not isinstance(item_screenshots, dict) or set(item_screenshots) != set(items):
        return "视觉认证缺少每个目标项对应的独立截图。"
    if not isinstance(review_item_screenshots, dict) or review_item_screenshots != item_screenshots:
        return "视觉认证的逐项目标截图与审查记录不一致。"
    if not isinstance(digests, dict) or set(digests) != set(items) or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(value or "")) for value in digests.values()
    ):
        return "视觉认证缺少每个目标截图的真实内容哈希。"
    if len(items) > 1 and len(set(str(value) for value in digests.values())) != len(items):
        return "多个目标项复用了同一张截图，不能证明逐项评审。"
    layout_analysis = certification.get("layout_analysis")
    if (
        not isinstance(layout_analysis, dict)
        or layout_analysis.get("status") != "clear"
        or layout_analysis.get("findings")
    ):
        return "视觉认证缺少通过的大块空白布局分析，不能证明截图内容完整。"
    analyzed_screenshots = layout_analysis.get("screenshots")
    if not isinstance(analyzed_screenshots, list) or not analyzed_screenshots:
        return "视觉认证缺少截图像素布局分析记录。"
    analyzed = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in analyzed_screenshots if isinstance(item, dict)
    }
    for item, path in item_screenshots.items():
        expected_path = str(Path(path).expanduser().resolve())
        if analyzed.get(expected_path) != digests.get(item):
            return f"视觉认证的截图布局分析与目标截图哈希不一致：{item}。"
    if review.get("mode") == "native":
        if not certification.get("self_reviewed"):
            return "主 Agent 没有签署逐项自评审确认，不能声称视觉验收通过。"
        attestation = str(certification.get("attestation") or "").strip()
        attestation_reason = _attestation_reason(attestation)
        if attestation_reason:
            return attestation_reason
    claimed_count = _claimed_scope_count(message)
    if claimed_count > len(items):
        return (
            f"完成声明覆盖 {claimed_count} 个目标，但视觉认证只登记了 {len(items)} 个逐项截图；"
            "不能把其余目标的功能操作或尺寸测量说成已做视觉检查。"
        )
    if not claimed_count and re.search(r"全部|所有|每个|每一|逐项|逐一", str(message or "")) \
            and re.search(r"象限|表格|表|弹窗|页面|模块|功能|目标|元素", str(message or "")) \
            and len(items) < 2:
        return "完成声明使用了全量覆盖措辞，但视觉认证只有一个目标项，不能声称逐项完成。"
    return None


def review_certificate_reason(review: dict[str, Any], message: str = "") -> str | None:
    """Public gate used by both visual and page verification entry points."""
    return _review_certificate_reason(review, message)


def completion_block(home: Path, session_id: str, message: str = "") -> str | None:
    state = read_state(home, session_id)
    target = state.get("target_review")
    if isinstance(target, dict) and not target.get("target_verified"):
        return "[AgentRelay 目标验收] 指定目标元素没有变成预期状态，不能声称修改成功。"
    review = state.get("visual_review")
    if not isinstance(review, dict):
        verification = state.get("verification") or {}
        status = str(verification.get("status") or "").strip()
        if status == "failed":
            errors = [
                str(item).strip() for item in verification.get("errors") or []
                if str(item).strip()
            ]
            detail = (errors[0] if errors else "未记录具体错误").splitlines()[0][:500]
            return (
                "[AgentRelay 视觉验收] 真实浏览器检查失败，尚未生成可用截图："
                f"{detail}。请修复该检查错误并重新运行 scripts/verify_page.py；"
                "在 verification.status=passed 且完成视觉审查前，不能声称页面完成或视觉通过。"
            )
        screenshots = [
            str(item) for item in verification.get("screenshots") or []
            if str(item).strip()
        ]
        if screenshots:
            return (
                "[AgentRelay 视觉验收] 真实浏览器检查已通过并生成截图，"
                "但尚未登记逐项视觉审查。请先读取截图，再运行 scripts/visual_review.py record；"
                "登记前不能声称视觉通过。"
            )
        if status == "passed":
            return (
                "[AgentRelay 视觉验收] 真实浏览器检查已通过，但没有登记截图。"
                "如需视觉验收，请重新运行 scripts/verify_page.py 生成截图并执行 record；"
                "如只做非视觉检查，请执行 visual_review.py nonvisual 并如实标记用户复查。"
            )
        return (
            "[AgentRelay 视觉验收] 本轮没有成功的真实浏览器检查记录。"
            "请先运行 scripts/verify_page.py，再登记逐项视觉审查；"
            "检查完成前不能声称页面完成或视觉通过。"
        )
    if review.get("status") != "passed":
        return ("[AgentRelay 视觉验收] 当前模型无视觉或视觉 Provider 不可用，"
                "已完成非视觉检查后仍需用户复查，不能声称视觉验收通过。")
    reason = _review_certificate_reason(review, message)
    if reason:
        return "[AgentRelay 视觉认证] " + reason
    return None


def _item_screenshot_mapping(values: Iterable[str]) -> dict[str, str] | None:
    if not values:
        return None
    result = {}
    for value in values:
        key, separator, path = value.partition("=")
        if not separator or not key.strip() or not path.strip():
            raise ValueError("--item-screenshot 必须是 目标=截图绝对路径")
        result[key.strip()] = path.strip()
    return result


def _cli_error_hint(message: str) -> str:
    if "截图必须来自当前浏览器验证结果" in message:
        return (
            "先运行 scripts/verify_page.py 完成真实 Chromium 检查，"
            "再把该次 verification.screenshots 中的原始路径传给 --screenshot。"
        )
    if "至少需要一张" in message or "真实截图不存在" in message:
        return "先运行 scripts/verify_page.py 生成并登记真实 Chromium 截图。"
    if "真实浏览器检查未通过" in message:
        return "修复浏览器检查失败项，重新运行 verify_page.py，确认 verification.status=passed。"
    if "逐项观察" in message or "观察过于笼统" in message or "具体证据" in message:
        return "逐项填写截图里可核对的文本、尺寸、颜色、rect、间距或修改前后状态。"
    if "自评审" in message:
        return "提供 --self-reviewed，并用 --attestation 说明逐项读取截图及实际核对范围。"
    if "cleanup" in message or "截图" in message and "删除" in message:
        return "完成视觉审查后运行 cleanup；未登记截图时用 --screenshot 逐张列出明确路径。"
    return "根据 error 修正参数或前置浏览器验证后重试。"


def _print_cli_error(exc: Exception) -> int:
    payload = {
        "status": "error",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "hint": _cli_error_hint(str(exc)),
    }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "nonvisual", "agent-image", "status", "cleanup"):
        item = sub.add_parser(name)
        item.add_argument("--session", required=True)
        item.add_argument("--home", type=Path, required=True)
        item.add_argument("--target", default="")
        item.add_argument("--unchanged", action="append", default=[])
        item.add_argument("--screenshot", action="append", default=[])
        item.add_argument("--summary", default="")
        item.add_argument("--model", default="")
        item.add_argument("--observation", action="append", default=[], help="项目=实际视觉观察，target 为必填项")
        item.add_argument("--target-item", action="append", default=[], help="逐项视觉目标；多目标必须分别绑定截图")
        item.add_argument("--item-screenshot", action="append", default=[], help="目标=截图绝对路径")
        item.add_argument("--self-reviewed", action="store_true", help="主 Agent 确认已逐项读取截图")
        item.add_argument("--attestation", default="", help="逐项读取截图并对照页面的自评审说明")
        item.add_argument("--passed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "record":
            observations = {}
            for item in args.observation:
                key, separator, value = item.partition("=")
                if not separator or not key.strip() or not value.strip():
                    parser.error("--observation 必须是 项目=实际观察")
                observations[key.strip()] = value.strip()
            result = record_review(args.home, args.session, mode="native", screenshots=args.screenshot,
                                   target=args.target, unchanged_checks=args.unchanged,
                                   passed=args.passed, summary=args.summary, model=args.model,
                                   observations=observations, target_items=args.target_item or None,
                                   item_screenshots=_item_screenshot_mapping(args.item_screenshot),
                                   self_reviewed=args.self_reviewed, attestation=args.attestation)
        elif args.command == "nonvisual":
            result = record_nonvisual_review(args.home, args.session, target=args.target,
                                             unchanged_checks=args.unchanged, summary=args.summary)
        elif args.command == "agent-image":
            result = review_with_agent_image(args.home, args.session, screenshots=args.screenshot,
                                             target=args.target, unchanged_checks=args.unchanged,
                                             target_items=args.target_item or None,
                                             item_screenshots=_item_screenshot_mapping(args.item_screenshot))
        elif args.command == "cleanup":
            result = {
                "deleted": cleanup_screenshots(args.home, args.session, args.screenshot),
                "cleanup_completed": True,
            }
        else:
            result = read_state(args.home, args.session).get("visual_review") or {}
    except (ImportError, OSError, ValueError, TypeError) as exc:
        return _print_cli_error(exc)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
