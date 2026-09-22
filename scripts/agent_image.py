#!/usr/bin/env python3
"""AgentRelay 图片识别中继（agent-image）。

用户主动触发（agent-image）：把本地图片发给当前已配置、支持识图的 Provider，
由它详细描述图片内容，描述文本输出到 stdout，供调用方 Agent 转述或自行消化。
这样即使本地 Agent 模型不支持图片识别，也能借助线上模型理解图片。

Provider 选择顺序：
1. 显式 --provider 指定、且支持识图的网页 Provider；
2. 默认网页 Provider（支持识图时）；
3. 其他已启用且支持识图的网页 Provider；
4. 安装时探测/标记 vision=true 的本地模型（OpenAI 兼容接口直接调用）。

一个 Provider 失败时自动按顺序切换下一个；全部失败时把每个 Provider 的
失败原因注入 stdout（exit 0），由调用方 Agent 转达用户并引导重新登录/配置。
没有任何可用 Provider 时同样正常退出并说明。

可靠性：
- 超过 2MB 或边长超过 2000px 的图片会先压缩为 JPEG 再发送（需 PIL，缺失时原样发送）；
- 同一张图片（按内容 hash）的识别结果缓存 7 天，命中直接返回；缓存每次运行时顺手清理。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
# 安装布局下本脚本位于 skills/agent-image/scripts，agent-relay 的完整脚本集
# （agent_relay.py、config_manager.py 等）在 skills/agent-relay/scripts；
# 源码仓库里两者平级，都在 scripts/。
_CANDIDATE_SCRIPT_DIRS = [
    SCRIPT_DIR,
    SCRIPT_DIR.parent.parent / "agent-relay" / "scripts",
]
for _candidate in reversed([p for p in _CANDIDATE_SCRIPT_DIRS if p.is_dir()]):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
RELAY_SCRIPT = next(
    (directory / "agent_relay.py" for directory in _CANDIDATE_SCRIPT_DIRS
     if (directory / "agent_relay.py").is_file()),
    None,
)

import config_manager  # noqa: E402

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_DIMENSION = 2000
WEB_CALL_TIMEOUT = max(60, int(os.environ.get("AGENT_IMAGE_WEB_TIMEOUT", "360")))
CACHE_TTL_SECONDS = 7 * 24 * 3600
CACHE_MAX_ENTRIES = 50

DEFAULT_PROMPT = (
    "请详细描述这张图片的内容，包括主要物体、场景、图中文字、布局、颜色等关键信息。"
    "要求：1) 直接输出描述内容，不要客套话，不要“好的”“这是一张……”之类的开场白；"
    "2) 不要评价图片质量，不要反问，不要给建议；"
    "3) 如果有多张图片，按图片顺序分别描述，每张冠以“图1”“图2”等编号。"
)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


# ---------------------------------------------------------------------------
# 图片预处理：大图先压缩，避免网页端拒绝或本地模型超时
# ---------------------------------------------------------------------------

def _pil_image_module():
    try:
        from PIL import Image  # type: ignore
        return Image
    except ImportError:
        return None


def prepare_image(path: Path) -> tuple[Path, bool]:
    """把超过 2MB 或边长超过 2000px 的图片压缩为 JPEG。

    返回 (实际发送路径, 是否经过压缩)。PIL 不可用或处理失败时原样返回。
    压缩产物是临时文件，调用方无需清理（系统 tmp 自动回收）。
    """
    image_module = _pil_image_module()
    if image_module is None:
        return path, False
    try:
        if path.stat().st_size <= MAX_IMAGE_BYTES:
            with image_module.open(path) as opened:
                if max(opened.size) <= MAX_IMAGE_DIMENSION:
                    return path, False
        with image_module.open(path) as opened:
            converted = opened.convert("RGB")
            if max(converted.size) > MAX_IMAGE_DIMENSION:
                ratio = MAX_IMAGE_DIMENSION / max(converted.size)
                converted = converted.resize(
                    (max(1, int(converted.width * ratio)),
                     max(1, int(converted.height * ratio)))
                )
            handle = tempfile.NamedTemporaryFile(
                suffix=".jpg", prefix="agentimage_", delete=False)
            converted.save(handle.name, "JPEG", quality=85)
            handle.close()
            return Path(handle.name), True
    except Exception:  # noqa: BLE001 - 预处理失败不阻断发送
        return path, False


# ---------------------------------------------------------------------------
# 结果缓存：同一张图不重复调用线上模型；TTL 7 天，每次运行顺手清理
# ---------------------------------------------------------------------------

def _extract_answer(output: str) -> str:
    """从 agent_relay.py 的输出中提取 AI 回复正文。"""
    marker = "💬 AI回复:"
    if marker not in output:
        return ""
    tail = output.split(marker, 1)[1]
    stop = len(tail)
    for separator in ("=" * 60, "AGENT_RELAY_RESULT="):
        index = tail.find(separator)
        if index != -1:
            stop = min(stop, index)
    return tail[:stop].strip()


def cache_directory(home: Path | None = None) -> Path:
    directory = (home or config_manager.codex_home()) / "skills" / "agent-image" / "cache"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory


def image_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cache_load(digest: str, home: Path | None = None,
               now: float | None = None) -> dict[str, Any] | None:
    entry_path = cache_directory(home) / f"{digest}.json"
    try:
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        created = float(entry.get("time", 0))
        if (now if now is not None else time.time()) - created > CACHE_TTL_SECONDS:
            entry_path.unlink(missing_ok=True)
            return None
        if not str(entry.get("description", "")).strip():
            return None
        return entry
    except (OSError, ValueError, TypeError):
        return None


def cache_store(digest: str, provider: str, description: str,
                home: Path | None = None, now: float | None = None) -> None:
    if not description.strip():
        return
    directory = cache_directory(home)
    entry_path = directory / f"{digest}.json"
    payload = json.dumps({
        "time": now if now is not None else time.time(),
        "provider": provider,
        "description": description,
    }, ensure_ascii=False)
    try:
        entry_path.write_text(payload, encoding="utf-8")
    except OSError:
        return
    cache_cleanup(home, now=now)


def cache_cleanup(home: Path | None = None, now: float | None = None) -> None:
    """删除过期缓存；超过上限时按时间从旧到新删。"""
    directory = cache_directory(home)
    current = now if now is not None else time.time()
    entries: list[tuple[float, Path]] = []
    for entry_path in directory.glob("*.json"):
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
            created = float(entry.get("time", 0))
        except (OSError, ValueError, TypeError):
            entry_path.unlink(missing_ok=True)
            continue
        if current - created > CACHE_TTL_SECONDS:
            entry_path.unlink(missing_ok=True)
            continue
        entries.append((created, entry_path))
    entries.sort()
    while len(entries) > CACHE_MAX_ENTRIES:
        _oldest, path = entries.pop(0)
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Provider 选择与调用
# ---------------------------------------------------------------------------

def load_config(home: Path | None = None) -> dict[str, Any]:
    return config_manager.load_config(home)


def web_provider_supports_images(provider_id: str, entry: Mapping[str, Any]) -> bool:
    conversation = config_manager.normalize_conversation(
        provider_id, entry.get("conversation") if isinstance(entry, Mapping) else None
    )
    return any(conversation["images"].values())


def image_capable_web_providers(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    providers = [
        item for item in config.get("web_providers", []) or []
        if isinstance(item, dict) and item.get("enabled", True)
    ]
    capable = [item for item in providers
               if web_provider_supports_images(str(item.get("id", "")), item)]
    default_id = str(config.get("default_provider", "")).strip().lower()
    capable.sort(key=lambda item: str(item.get("id", "")).strip().lower() != default_id)
    return capable


def pick_local_provider(
    config: Mapping[str, Any], requested: str = ""
) -> dict[str, Any] | None:
    """挑出第一个标记了 vision=true 且可用的本地模型。"""
    wanted = (requested or "").strip().lower()
    for item in config.get("local_providers", []) or []:
        if not (isinstance(item, dict) and item.get("enabled", True)):
            continue
        if not item.get("vision"):
            continue
        if wanted and str(item.get("id", "")).strip().lower() != wanted:
            continue
        if item.get("endpoint") and item.get("model"):
            return item
    return None


def local_vision_providers(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in config.get("local_providers", []) or []
        if isinstance(item, dict) and item.get("enabled", True)
        and item.get("vision") and item.get("endpoint") and item.get("model")
    ]


def validate_images(paths: list[str]) -> tuple[list[Path], list[str]]:
    images: list[Path] = []
    problems: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            problems.append(f"图片不存在或不是文件：{raw}")
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            problems.append(f"不支持的图片格式（{path.suffix or '无扩展名'}）：{raw}")
            continue
        images.append(path)
    return images, problems


def _classify_relay_failure(output: str, timed_out: bool) -> str:
    if timed_out:
        return "等待回复超时"
    if "未检测到" in output and "登录状态" in output:
        return "登录状态缺失（Cookie 可能失效）"
    if "出错:" in output:
        tail = output.split("出错:", 1)[1].strip().splitlines()[0]
        return f"调用出错（{tail[:80]}）"
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return f"未返回有效结果（{lines[-1][:60]}）" if lines else "未返回任何输出"


def run_web_provider_attempt(
    provider_id: str,
    prompt: str,
    images: list[Path],
    home: Path | None = None,
    timeout: int = WEB_CALL_TIMEOUT,
) -> tuple[bool, str, str]:
    """调用一次网页 Provider。返回 (是否成功, 完整输出, 失败原因)。"""
    if RELAY_SCRIPT is None:
        return False, "", "未找到 agent_relay.py"
    command = [
        sys.executable, str(RELAY_SCRIPT),
        prompt,
        "--provider", provider_id.removesuffix("-web"),
    ]
    for image in images:
        command.extend(["--image", str(image)])
    env = dict(os.environ)
    config_manager.apply_config_to_environment(home)
    for key, value in os.environ.items():
        env.setdefault(key, value)
    timed_out = False
    try:
        result = subprocess.run(
            command, env=env, check=False,
            capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = ((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        output += ((exc.stderr or "") if isinstance(exc.stderr, str) else "")
    except OSError as exc:
        return False, "", f"脚本无法启动（{exc}）"

    for line in output.splitlines():
        if line.startswith("AGENT_RELAY_RESULT="):
            try:
                completion = json.loads(line[len("AGENT_RELAY_RESULT="):])
            except json.JSONDecodeError:
                continue
            if completion.get("status") == "success" and int(completion.get("answer_chars", 0)) > 0:
                return True, output, ""
    return False, output, _classify_relay_failure(output, timed_out)


def _encode_image(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def run_local_provider_attempt(
    entry: Mapping[str, Any], prompt: str, images: list[Path]
) -> tuple[bool, str, str]:
    """OpenAI 兼容接口调用本地多模态模型。返回 (是否成功, 描述文本, 失败原因)。"""
    endpoint = str(entry.get("endpoint", "")).rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    timeout = max(5, int(entry.get("queue_timeout", 30) or 30)) * max(1, len(images)) + 30
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    try:
        for image in images:
            mime, data = _encode_image(image)
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    except OSError as exc:
        return False, "", f"读取图片失败（{exc}）"
    payload = {
        "model": str(entry.get("model", "")),
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        answer = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
        if isinstance(answer, list):
            answer = "".join(
                str(part.get("text", "")) for part in answer if isinstance(part, dict)
            )
        if not str(answer).strip():
            return False, "", "模型未返回描述内容"
        return True, str(answer).strip(), ""
    except Exception as exc:  # noqa: BLE001 - 失败原因注入给调用方
        return False, "", f"{type(exc).__name__}"


def print_failure_report(failures: list[tuple[str, str]]) -> None:
    """全部失败后注入说明，由调用方 Agent 转达用户。"""
    print("\n【AgentImage 注】")
    print("图片识别失败：以下模型均已尝试并自动切换，均不可用。")
    for name, reason in failures:
        print(f"  - {name}：{reason}")
    print("本次没有图片被成功识别。")
    print("建议：重新运行 python3 install.py 写入 DeepSeek 的 Cookie，"
          "或在配置中心检查 Provider 状态后重试。")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AgentRelay 图片识别中继：把图片发给支持识图的在线模型并输出描述。"
    )
    parser.add_argument("images", nargs="+", help="图片文件路径，最多 5 张。")
    parser.add_argument(
        "--provider", default="",
        help="优先使用指定网页 Provider（如 deepseek）；失败后仍会自动切换其他候选。",
    )
    parser.add_argument(
        "--question", default="",
        help="附加描述要求，例如“重点识别图中的文字”。",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="跳过结果缓存，强制调用模型重新识别。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    images, problems = validate_images(list(args.images))
    for problem in problems:
        print(f"⚠️ {problem}")
    if not images:
        print("❌ 没有可用的图片文件。")
        return 2
    if len(images) > MAX_IMAGES:
        print(f"❌ 一次最多 {MAX_IMAGES} 张图片，当前 {len(images)} 张。请分批处理。")
        return 2

    prompt = DEFAULT_PROMPT + (f" 额外要求：{args.question.strip()}" if args.question.strip() else "")

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 读取本地配置失败（{type(exc).__name__}）。")
        config = config_manager.default_config()

    digest = hashlib.sha256(
        b"".join(image_digest(path).encode("ascii") for path in images)
    ).hexdigest()
    if not args.no_cache:
        cached = cache_load(digest)
        if cached is not None:
            print(f"[缓存] 命中 {time.strftime('%m-%d %H:%M', time.localtime(cached['time']))} "
                  f"由 {cached.get('provider', '未知模型')} 生成的识别结果，未重复调用模型。")
            print(cached["description"])
            return 0

    prepared: list[Path] = []
    for image in images:
        send_path, compressed = prepare_image(image)
        if compressed:
            print(f"🗜️ {image.name} 已压缩后再发送（超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 或边长超过 {MAX_IMAGE_DIMENSION}px）。")
        prepared.append(send_path)

    web_candidates = image_capable_web_providers(config)
    requested = (args.provider or "").strip().lower()
    requested_note = ""
    if requested:
        aliases = {requested, f"{requested}-web", requested.removesuffix("-web")}
        match = next((item for item in web_candidates
                      if str(item.get("id", "")).strip().lower() in aliases), None)
        entry = config_manager.find_web_provider(config, requested)
        if match is not None:
            web_candidates = [match] + [item for item in web_candidates if item is not match]
        elif entry is not None:
            requested_note = f"指定的 Provider（{requested}）不支持图片，已改用其他支持识图的 Provider。"
        else:
            requested_note = f"找不到指定的 Provider（{requested}），已改用其他支持识图的 Provider。"

    local_candidates = local_vision_providers(config)
    if requested and local_candidates:
        match = next((item for item in local_candidates
                      if str(item.get("id", "")).strip().lower() == requested), None)
        if match is not None:
            local_candidates = [match] + [item for item in local_candidates if item is not match]

    if requested_note:
        print(f"[Provider] {requested_note}", flush=True)

    if not web_candidates and not local_candidates:
        print(
            "⚠️ 当前没有任何支持图片识别的模型可用：\n"
            "  - 网页模型需要在配置中心启用支持图片的对话模式（DeepSeek 混合模式默认支持；\n"
            "    GPT 网页识图限时限量，默认不启用）；\n"
            "  - 本地模型需要在配置时确认支持图片识别（vision）。\n"
            "  请先在配置中心完成配置后再试；本次图片未被发送。"
        )
        return 0

    failures: list[tuple[str, str]] = []
    for entry in web_candidates:
        provider_id = str(entry.get("id", "deepseek-web"))
        label = str(entry.get("name", provider_id))
        print(f"[Provider] 使用网页模型 {label} 识别图片。", flush=True)
        ok, output, reason = run_web_provider_attempt(provider_id, prompt, prepared)
        if ok:
            print(output)
            answer = _extract_answer(output)
            if not args.no_cache:
                cache_store(digest, label, answer)
            return 0
        print(f"[Provider] {label} 失败（{reason}），切换下一个候选。", flush=True)
        failures.append((label, reason))

    for entry in local_candidates:
        label = f"本地模型 {entry.get('name', entry.get('id'))}"
        print(f"[Provider] 使用 {label} 识别图片。", flush=True)
        ok, answer, reason = run_local_provider_attempt(entry, prompt, prepared)
        if ok:
            print(answer)
            if not args.no_cache:
                cache_store(digest, label, answer)
            return 0
        print(f"[Provider] {label} 失败（{reason}），切换下一个候选。", flush=True)
        failures.append((label, reason))

    print_failure_report(failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
