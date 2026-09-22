#!/usr/bin/env python3
"""AgentRelay 图片识别中继（agent-image）。

用户主动触发（agent-image）：把本地图片发给当前已配置、支持识图的 Provider，
由它详细描述图片内容，描述文本输出到 stdout，供调用方 Agent 转述。
这样即使本地 Agent 模型不支持图片识别，也能借助线上模型理解图片。

Provider 选择顺序：
1. 显式 --provider 指定、且支持识图的网页 Provider；
2. 默认网页 Provider（支持识图时）；
3. 任意已启用且支持识图的网页 Provider；
4. 安装时标记 vision=true 的本地模型（OpenAI 兼容接口直接调用）；
5. 都不支持时打印明确说明并以 exit 0 正常退出，由调用方 Agent 自行处理。

不做 Hook 集成；只在用户明确要求时运行。不绕过任何人机验证。
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import sys
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

DEFAULT_PROMPT = (
    "请详细描述这张图片的内容，包括主要物体、场景、图中文字、布局、颜色等关键信息。"
    "要求：1) 直接输出描述内容，不要客套话，不要“好的”“这是一张……”之类的开场白；"
    "2) 不要评价图片质量，不要反问，不要给建议；"
    "3) 如果有多张图片，按图片顺序分别描述，每张冠以“图1”“图2”等编号。"
)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def load_config(home: Path | None = None) -> dict[str, Any]:
    return config_manager.load_config(home)


def web_provider_supports_images(provider_id: str, entry: Mapping[str, Any]) -> bool:
    conversation = config_manager.normalize_conversation(
        provider_id, entry.get("conversation") if isinstance(entry, Mapping) else None
    )
    return any(conversation["images"].values())


def pick_web_provider(
    config: Mapping[str, Any], requested: str = ""
) -> tuple[dict[str, Any] | None, str]:
    """在网页 Provider 中按选择顺序挑出第一个支持识图的，返回 (entry, 说明)。"""
    providers = [
        item for item in config.get("web_providers", []) or []
        if isinstance(item, dict) and item.get("enabled", True)
    ]
    if not providers:
        return None, "没有已启用的网页 Provider。"

    wanted = (requested or "").strip().lower()

    def find(predicate, note):
        for item in providers:
            pid = str(item.get("id", "")).strip().lower()
            if predicate(pid, item) and web_provider_supports_images(pid, item):
                return item, note
        return None, ""

    if wanted:
        aliases = {wanted, f"{wanted}-web", wanted.removesuffix("-web")}
        entry, note = find(lambda pid, _it: pid in aliases, "")
        if entry is not None:
            return entry, note
        entry = config_manager.find_web_provider(config, wanted)
        if entry is not None and not web_provider_supports_images(wanted, entry):
            return None, f"指定的 Provider（{wanted}）不支持图片（没有已启用模式支持识图）。"
        return None, f"找不到指定的 Provider（{wanted}）。"

    default_id = str(config.get("default_provider", "")).strip().lower()
    if default_id:
        entry, note = find(lambda pid, _it: pid == default_id, "")
        if entry is not None:
            return entry, note
    entry, note = find(lambda _pid, _it: True, "默认网页 Provider 不支持图片，已改用其他支持识图的网页 Provider。")
    if entry is None:
        note = "已启用的网页 Provider 均不支持图片。"
    return entry, note


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


def run_web_provider(
    provider_id: str,
    prompt: str,
    images: list[Path],
    home: Path | None = None,
) -> int:
    """复用 agent_relay.py 的网页会话链路发图识图，stdout 直通。"""
    if RELAY_SCRIPT is None:
        print("❌ 未找到 agent_relay.py，无法调用网页 Provider。请重新运行安装脚本。")
        return 0
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
    try:
        result = subprocess.run(command, env=env, check=False)
    except OSError as exc:
        print(f"❌ 调用网页 Provider 失败：{exc}")
        return 0
    return result.returncode


def _encode_image(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def run_local_provider(entry: Mapping[str, Any], prompt: str, images: list[Path]) -> int:
    """OpenAI 兼容接口直接调用本地多模态模型。"""
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
        print(f"❌ 读取图片失败：{exc}")
        return 0
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
            print("⚠️ 本地模型没有返回描述内容。")
            return 0
        print(str(answer).strip())
        return 0
    except Exception as exc:  # noqa: BLE001 - 任何失败都降级为提示，保证 exit 0
        print(f"⚠️ 本地模型识图调用失败（{type(exc).__name__}），请检查本地服务是否支持图片输入。")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AgentRelay 图片识别中继：把图片发给支持识图的在线模型并输出描述。"
    )
    parser.add_argument(
        "images",
        nargs="+",
        help="图片文件路径，最多 5 张。",
    )
    parser.add_argument(
        "--provider",
        default="",
        help="指定网页 Provider（如 deepseek）；不指定时按默认顺序自动选择。",
    )
    parser.add_argument(
        "--question",
        default="",
        help="附加描述要求，例如“重点识别图中的文字”。",
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

    web_entry, note = pick_web_provider(config, args.provider)
    if web_entry is not None:
        if note:
            print(f"[Provider] {note}", flush=True)
        provider_id = str(web_entry.get("id", "deepseek-web"))
        print(f"[Provider] 使用网页模型 {web_entry.get('name', provider_id)} 识别图片。", flush=True)
        return run_web_provider(provider_id, prompt, images)

    local_entry = pick_local_provider(config, args.provider)
    if local_entry is not None:
        if note:
            print(f"[Provider] {note}", flush=True)
        print(f"[Provider] 使用本地模型 {local_entry.get('name', local_entry.get('id'))} 识别图片。", flush=True)
        return run_local_provider(local_entry, prompt, images)

    if note:
        print(f"[Provider] {note}")
    print(
        "⚠️ 当前没有任何支持图片识别的模型可用：\n"
        "  - 网页模型需要在配置中心启用支持图片的对话模式（DeepSeek 混合模式默认支持；\n"
        "    GPT 网页识图限时限量，默认不启用）；\n"
        "  - 本地模型需要在配置时标记支持图片识别（vision）。\n"
        "  请先在配置中心完成配置后再试；本次图片未被发送。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
