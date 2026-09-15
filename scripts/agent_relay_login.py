import argparse
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

try:
    from .agent_relay_runtime import (
        normalize_provider_name,
        provider_spec,
        provider_state_file,
        resolve_codex_home,
    )
except ImportError:
    from agent_relay_runtime import (
        normalize_provider_name,
        provider_spec,
        provider_state_file,
        resolve_codex_home,
    )


CODEX_HOME = resolve_codex_home()

def login_provider(provider_name="deepseek"):
    """打开当前 Provider，并在用户手动认证后保存登录状态。"""

    provider_name = normalize_provider_name(provider_name)
    spec = provider_spec(provider_name)
    state_file = provider_state_file(provider_name, CODEX_HOME)
    encrypted_path = state_file if state_file.suffix == ".enc" else state_file.with_suffix(state_file.suffix + ".enc")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"正在打开 {spec.name} 登录页面...")
        page.goto(spec.base_url)

        print("请在浏览器中手动登录并完成人机验证/CAPTCHA。")
        print("AgentRelay 不会填写凭据，也不会尝试绕过验证码。")
        input("完成登录并进入聊天页面后，回到终端按 Enter 保存会话...")

        state_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as temporary:
            temporary_state = Path(temporary.name)
        try:
            context.storage_state(path=str(temporary_state))
            try:
                from .config_manager import encrypt_secret_bytes
            except ImportError:
                from config_manager import encrypt_secret_bytes
            encrypted = encrypt_secret_bytes(temporary_state.read_bytes(), CODEX_HOME)
            encrypted_path.write_bytes(encrypted)
            encrypted_path.chmod(0o600)
            print(f"\n登录状态已加密保存到 {encrypted_path}")
        finally:
            temporary_state.unlink(missing_ok=True)
        browser.close()


def login_custom_provider(provider_name: str, url: str, state_file: str | None = None):
    """登录未内置适配器的网页 Provider，只保存 Playwright 会话状态。"""
    if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc:
        raise ValueError("登录网址必须是完整的 HTTP(S) 地址")
    target = Path(state_file or (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        / "skills" / "agent-relay" / f"agent_relay_{provider_name}-web_login_state.json"
    )).expanduser()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        print(f"正在打开 {provider_name} 登录页面：{url}")
        page.goto(url)
        print("请在浏览器中手动登录并完成人机验证。")
        input("完成登录后回到终端按 Enter 保存会话...")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as temporary:
            temporary_state = Path(temporary.name)
        try:
            context.storage_state(path=str(temporary_state))
            try:
                from .config_manager import encrypt_secret_bytes
            except ImportError:
                from config_manager import encrypt_secret_bytes
            encrypted_path = target if target.suffix == ".enc" else target.with_suffix(target.suffix + ".enc")
            encrypted_path.write_bytes(encrypt_secret_bytes(temporary_state.read_bytes()))
            encrypted_path.chmod(0o600)
            print(f"登录状态已加密隔离保存到：{encrypted_path}")
        finally:
            temporary_state.unlink(missing_ok=True)
        browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="登录 AgentRelay Provider")
    parser.add_argument(
        "--provider",
        default=os.environ.get("AGENT_RELAY_PROVIDER", "deepseek"),
        help="Provider 名称；内置 deepseek，其他 Provider 需要配合 --url",
    )
    parser.add_argument("--url", default="", help="自定义网页 Provider 登录网址")
    parser.add_argument("--state-file", default="", help="自定义登录状态文件路径")
    args = parser.parse_args()
    if args.url:
        login_custom_provider(args.provider, args.url, args.state_file or None)
    else:
        login_provider(args.provider)
