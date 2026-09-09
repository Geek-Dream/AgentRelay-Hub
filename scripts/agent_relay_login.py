import os
from pathlib import Path

from playwright.sync_api import sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
STATE_FILE = Path(
    os.path.expanduser(
        os.environ.get(
            "AGENT_RELAY_LOGIN_STATE",
            str(
                Path(
                    os.path.expanduser(
                        os.environ.get(
                            "CODEX_HOME",
                            str(Path.home() / ".codex"),
                        )
                    )
                )
                / "skills"
                / "agent-relay"
                / "agent_relay_login_state.json"
            ),
        )
    )
)

def login_provider():
    """打开当前 Provider，并在用户手动认证后保存登录状态。"""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print("正在打开 DeepSeek 登录页面...")
        page.goto("https://chat.deepseek.com/")

        print("请在浏览器中手动登录并完成人机验证/CAPTCHA。")
        print("AgentRelay 不会填写凭据，也不会尝试绕过验证码。")
        input("完成登录并进入聊天页面后，回到终端按 Enter 保存会话...")

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STATE_FILE))
        print(f"\n登录状态已保存到 {STATE_FILE}")
        browser.close()

if __name__ == "__main__":
    login_provider()
