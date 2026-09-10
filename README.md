# AgentRelay

**AgentRelay - A local AI relay layer for Codex CLI, connecting your coding agent with external expert models.**

AgentRelay 是 Codex 的自动专家代理中继系统。它通过 Codex Hook 观察问题处理过程，
在重复失败或长时间调试时向 Agent 提供触发上下文，由 Agent 决定是否请求在线专家模型的
第二意见。

当前内置的首个 Provider 示例为 **DeepSeek**。公开 Skill、Hook、Tracker 和命令均使用
AgentRelay 通用命名；未来可以在同一中继流程下增加其他 Provider。

AgentRelay 不把 Hook 直接连接到在线模型：Hook 只产生 `additionalContext`，真正的调用仍由
Codex Agent 按 Skill 规则发起。

## 架构

```text
Codex
  ↓
Hook
  ↓
Tracker
  ↓
additionalContext
  ↓
AgentRelay Skill
  ↓
Provider Adapter
  ↓
DeepSeek（当前示例）
```

自动触发依据包括重复失败、有效处理时间和既有权重信息。用户也可以明确说“调用
AgentRelay”“让 AgentRelay 分析”或“调用 DeepSeek”。否定请求不会触发调用。

## Provider 兼容性

AgentRelay 的 Hook、Tracker、触发状态和 Skill 是模型无关的。DeepSeek 只是当前已经实现并
可用的首个 Provider 适配器；后续可以增加千问、OpenAI、Grok 或其他在线模型，而不改变
AgentRelay 的核心触发链路。

DeepSeek 当前把极速、图片和专家能力统一到同一会话。AgentRelay 会优先使用持久化的
`session_id/href`；首次运行或绑定失效时扫描左侧对话栏，兼容旧的“极速/图片/专家/思考”
标题。仍未找到时，发送本次真实问题创建新会话，将其重命名为 `AgentRelay-DeepSeek` 并绑定。
`-m 1`、图片请求和 `-m 2` 因此都会复用同一个 DeepSeek 会话。

通用 Provider Adapter 仍保留独立的会话作用域和能力接口。未来接入千问等网站时，只需新增
对应 Adapter、Provider 配置及侧栏链接解析规则；不需要修改主调用流程。

## Requirements

- Codex CLI（当前稳定版本）
- Python >= 3.10
- Python `venv` 支持（Ubuntu/Debian 通常由 `python3-venv` 提供）
- 可访问 PyPI 和 Playwright 浏览器下载源的网络
- 当前 Provider 对应的账号（现阶段为 DeepSeek account）

## 安装

```bash
git clone git@github.com:Geek-Dream/AgentRelay-Hub.git
cd AgentRelay-Hub
python3 install.py
```

安装器支持 `CODEX_HOME`；未设置时使用 `$HOME/.codex`。它会：

- 安装 Hook 到 `$CODEX_HOME/hooks/`
- 安装 Skill 和脚本到 `$CODEX_HOME/skills/agent-relay/`
- 创建独立环境 `$CODEX_HOME/agentrelay-env/`
- 在独立环境中自动安装 requirements 和完整 Chromium
- 保留用户已有文件
- 备份并原子合并现有 `hooks.json`，不删除任何已有 Hook
- 检查 Codex CLI、Python、虚拟环境、Playwright 和 Chromium

安装器不会向 Homebrew、系统 Python 或其他全局环境执行 `pip install`，因此不需要
`--break-system-packages`，也不会触发 PEP 668 的 externally-managed-environment 限制。

## 首次登录

安装后运行：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  "${CODEX_HOME:-$HOME/.codex}/skills/agent-relay/scripts/agent_relay_login.py"
```

Windows PowerShell：

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
& "$CodexHome\agentrelay-env\Scripts\python.exe" `
  "$CodexHome\skills\agent-relay\scripts\agent_relay_login.py"
```

脚本会打开一个可见的 Chromium 浏览器。请在浏览器中手动登录 DeepSeek，并自行完成人机
验证。登录状态会保存到：

```text
$CODEX_HOME/skills/agent-relay/agent_relay_login_state.json
```

登录需要人工浏览器认证，因为 DeepSeek CAPTCHA 不能也不会被 AgentRelay 绕过。请勿将
登录状态文件、Cookie 或浏览器数据提交到版本库。

> AgentRelay uses a user-provided authenticated browser session. It does not bypass CAPTCHA or automate credential input.

登录完成后重启 Codex，使其重新加载 `hooks.json`。

## 安装验证

安装完成、登录之前，Skill 目录应包含：

```text
SKILL.md
scripts/agent_relay.py
scripts/agent_relay_login.py
```

用户完成首次浏览器登录后，才会生成 `agent_relay_login_state.json`。可以在不调用在线模型的
情况下检查 Tracker：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" status
```

正常的新状态应显示 `retry_count: 0`、`relay_triggered: false` 和
`should_trigger: false`。测试 Skill 实际调用前需要重启 Codex。

## 目录结构

```text
AgentRelay-Hub/
├── README.md
├── LICENSE
├── install.py
├── requirements.txt
├── hooks/
│   ├── agent_relay_hook.py
│   ├── agent_relay_hook.sh
│   └── agent_relay_tracker.py
├── scripts/
│   ├── agent_relay.py
│   ├── agent_relay_login.py
│   └── agent_relay_runtime.py
├── skills/
│   └── agent-relay/
│       └── SKILL.md
├── examples/
│   └── hooks.json.example
└── .github/workflows/ci.yml
```

安装后，Codex 目录会额外包含：

```text
$CODEX_HOME/
├── agentrelay-env/          # AgentRelay 独立 Python、pip、Playwright
├── hooks/
├── skills/agent-relay/
└── hooks.json
```

## 配置

- `CODEX_HOME`：Codex 配置根目录，默认 `$HOME/.codex`
- `AGENT_RELAY_LOGIN_STATE`：可选，自定义 Playwright storage state 路径
- `AGENT_RELAY_<PROVIDER>_LOGIN_STATE`：可选，指定某个 Provider 的 storage state
- `AGENT_RELAY_PROVIDER`：默认 Provider，当前为 `deepseek`
- `AGENT_RELAY_SESSION_BINDINGS`：可选，自定义会话绑定 JSON 路径
- `AGENT_RELAY_BROWSER_PATH`：可选，自定义 Chromium 可执行文件
- `AGENT_RELAY_DEBUG=true`：可选，显示诊断输出；默认关闭

运行错误写入 Skill 根目录的 `logs/error.log`。Tracker/Hook 错误写入
`$CODEX_HOME/agent_relay_tracker/logs/error.log`。

## 故障排查

### Hook 无响应

确认 Codex 已在安装后重启，检查 `$CODEX_HOME/hooks.json` 是否包含
`agent_relay_hook.py`，并查看 `$CODEX_HOME/agent_relay_tracker/logs/error.log`。Python Hook
可在 Windows、macOS 和 Linux 使用；旧的 shell Hook 只作为 POSIX 兼容文件保留。

### 登录失效或 Session 过期

重新运行 `agent_relay_login.py`，在浏览器中手动完成登录，再重启 Codex。不要手动编辑 storage
state 文件。

### Provider 页面变化

页面结构变化可能导致回复提取失败。查看 Skill 的 `logs/error.log`，提交 issue 时只附去敏后的
错误信息，不要附 Cookie、登录状态或私人对话。

### Chromium 不存在

重新运行 `python3 install.py`。安装器会在独立环境中执行 Playwright 的 Chromium 安装，并验证
`chromium.launch(headless=False)` 所需的完整浏览器；也可通过 `AGENT_RELAY_BROWSER_PATH`
指定兼容的 Chromium。

Linux 如果已经存在 Chromium，但启动时报缺少系统库，可按 Playwright 提示安装系统依赖；
安装器不会自动执行需要管理员权限的 `install-deps` 或 `sudo`。

## 安全边界

AgentRelay 不绕过 CAPTCHA，不保存账号密码，不在 Hook 中直接访问任何在线 Provider，也不会
把 Provider 建议自动视为最终答案。Agent 必须自行判断、修改并验证。

## License

MIT，详见 [LICENSE](LICENSE)。
