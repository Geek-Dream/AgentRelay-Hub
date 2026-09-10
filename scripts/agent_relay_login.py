import argparse
import os

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
        context.storage_state(path=str(state_file))
        print(f"\n登录状态已保存到 {state_file}")
        browser.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="登录 AgentRelay Provider")
    parser.add_argument(
        "--provider",
        default=os.environ.get("AGENT_RELAY_PROVIDER", "deepseek"),
        help="Provider 名称；当前支持 deepseek",
    )
    args = parser.parse_args()
    login_provider(args.provider)
