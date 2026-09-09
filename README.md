# AgentRelay

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

当前脚本中的“极速/生图”和“专家/思考”是 DeepSeek Provider 使用的固定会话列表名称，
不是 AgentRelay 的通用模型能力定义。未来接入其他 Provider 时，应将这些名称放入对应
Provider 的会话配置中；本次发布保留现有判断，避免破坏已验证的请求和回复流程。

## Requirements

- Codex CLI（当前稳定版本）
- Python >= 3.10
- pip
- Playwright
- Playwright Chromium
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
- 保留用户已有文件
- 备份并原子合并现有 `hooks.json`，不删除任何已有 Hook
- 检查 Codex CLI、Python、pip、Playwright 和 Chromium

若依赖尚未安装，请执行：

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

## 首次登录

安装后运行：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-relay/scripts/agent_relay_login.py"
```

脚本会打开一个可见的 Chromium 浏览器。请在浏览器中手动登录 DeepSeek，并自行完成人机
验证。登录状态会保存到：

```text
$CODEX_HOME/skills/agent-relay/agent_relay_login_state.json
```

登录需要人工浏览器认证，因为 DeepSeek CAPTCHA 不能也不会被 AgentRelay 绕过。请勿将
登录状态文件、Cookie 或浏览器数据提交到版本库。

登录完成后重启 Codex，使其重新加载 `hooks.json`。

## 目录结构

```text
AgentRelay-Hub/
├── README.md
├── LICENSE
├── install.py
├── requirements.txt
├── hooks/
│   ├── agent_relay_hook.sh
│   └── agent_relay_tracker.py
├── scripts/
│   ├── agent_relay.py
│   └── agent_relay_login.py
├── skills/
│   └── agent-relay/
│       └── SKILL.md
├── examples/
│   └── hooks.json.example
└── .github/workflows/ci.yml
```

## 配置

- `CODEX_HOME`：Codex 配置根目录，默认 `$HOME/.codex`
- `AGENT_RELAY_LOGIN_STATE`：可选，自定义 Playwright storage state 路径
- `AGENT_RELAY_BROWSER_PATH`：可选，自定义 Chromium 可执行文件
- `AGENT_RELAY_DEBUG=true`：可选，显示诊断输出；默认关闭

运行错误写入 Skill 根目录的 `logs/error.log`。Tracker/Hook 错误写入
`$CODEX_HOME/agent_relay_tracker/logs/error.log`。

## 故障排查

### Hook 无响应

确认 Codex 已在安装后重启，检查 `$CODEX_HOME/hooks.json` 是否包含
`agent_relay_hook.sh`，并查看 `$CODEX_HOME/agent_relay_tracker/logs/error.log`。

### 登录失效或 Session 过期

重新运行 `agent_relay_login.py`，在浏览器中手动完成登录，再重启 Codex。不要手动编辑 storage
state 文件。

### Provider 页面变化

页面结构变化可能导致回复提取失败。查看 Skill 的 `logs/error.log`，提交 issue 时只附去敏后的
错误信息，不要附 Cookie、登录状态或私人对话。

### Chromium 不存在

运行 `python3 -m playwright install chromium`，或通过 `AGENT_RELAY_BROWSER_PATH` 指定兼容的
Chromium。

## 安全边界

AgentRelay 不绕过 CAPTCHA，不保存账号密码，不在 Hook 中直接访问任何在线 Provider，也不会
把 Provider 建议自动视为最终答案。Agent 必须自行判断、修改并验证。

## License

MIT，详见 [LICENSE](LICENSE)。
