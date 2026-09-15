# AgentRelay

**AgentRelay - A local AI relay layer for Codex CLI, connecting your coding agent with external expert models.**

AgentRelay 是 Codex 的自动专家代理中继系统。它通过 Codex Hook 观察问题处理过程，
在重复失败或长时间调试时向 Agent 提供触发上下文，由 Agent 决定是否请求在线专家模型的
第二意见。

当前内置的首个 Provider 示例为 **DeepSeek**。公开 Skill、Hook、Tracker 和命令均使用
AgentRelay 通用命名；未来可以在同一中继流程下增加其他 Provider。

AgentRelay 不把 Hook 直接连接到在线模型：Hook 只产生 `additionalContext`，真正的调用仍由
Codex Agent 按 Skill 规则发起。

### Commander 与模型配置

Commander 的默认 Worker 是当前已安装并已登录的 `codex` CLI，通过多个隔离的
`codex exec` 子进程协同工作，不要求 DeepSeek、本地 9B 或 GPT API。它会按任务动态规划
1 到 5 个角色，例如前端样式、前端脚本、后端数据库、后端代码和 Git 审计；只需要一个角色
时不会强行启动三个。每个角色使用独立 workspace，结果由主 Agent 审查，未经确认卡批准不会
合并回用户工作区。

某个角色提前完成后，如果另一个角色仍在处理、超时或触发升级条件，Commander 可以让已完成
的角色再跑一轮协助任务。协助者保留自己的原角色和原结果，只提交分析、验证或补充修改；困难
角色仍是主力，不会被协助者替代。

显式运行示例：

```bash
PYTHONPATH=. python3 scripts/agentrelay_console.py commander task-1 \
  "大型跨模块重构" --workspace /path/to/project --max-agents 5
```

真实子 Agent 执行前，必须先通过 `task create` 生成确认卡，再执行
`task approve task-1`；未批准时只能使用 `--offline-plan` 查看角色规划。

DeepSeek、本地模型和 GPT API 是可选的角色 Provider；配置后可以按角色覆盖当前 Codex，
但没有这些 Provider 不会使 Commander 退化为空任务。

Provider 按接入方式分类，而不是按品牌分类：任意网页会话模型都属于 `web`，任意
OpenAI-compatible 或中转站接口都属于 `api`，本地服务属于 `local`。DeepSeek、千问、Kimi、
GPT 只是 Provider ID 的示例。新增网页模型仍需为该网站提供真实浏览器 Adapter；仅登记名称
不会伪造可用性。新增 API 模型可设置 `AGENTRELAY_API_PROVIDERS_JSON`，例如：

```json
[{"id":"kimi-api","endpoint":"https://gateway.example/v1","api_key_env":"KIMI_API_KEY","model":"model-name"}]
```

Commander 子 Agent 的 Provider 规则：同一个网页会话或本地模型进程全局一次只允许一个角色
使用；角色第一次选择的网页 Provider 会持续复用其会话。子 Agent 可以通过受控入口使用网页
模型或本地模型，但不能使用 GPT/API Provider，也不能递归启动 Commander。Cookie 失效或代理
不可用会熔断 Provider，并最多提示三次登录脚本/代理端口；重新认证后，主 Commander 按已记录
质量决定恢复原 Provider 还是继续临时替代 Provider。这里的比较不会另造无关测试题：会复用
当前真实任务的原始问题（例如“下载并配置 Redis”），分别取得两个 Provider 对这个任务的回答，
再由主 Agent 判断谁的方案更准确、更完整、更能执行。

Provider 选择会按角色记忆：前端角色之前验证过 DeepSeek，后续前端任务仍优先复用它；如果它
Cookie 失效，Commander 会在提醒用户的同时使用可用备用 Provider。恢复登录后，可以用同一道
真实问题分别重新取得两个 Provider 的回答，由主 Agent 按准确性、完整性、可执行性和废话多少进行比较，
把两份回答、分数、最终选择和备用期间的上下文摘要一起记下来；不会为了比较额外制造“测试模型”问题。
如果备用 Provider 当时不可用，会保留熔断期间对同一问题的回答并记录失败原因。多个 Agent 同时需要本地模型时会排队，
最多只留一个等待者；如果线上 Provider 有空闲，会立即使用线上 Provider，不会傻等本地队列。
只有所有候选都忙时才进入等待位，超时后才失败或切换，不会并发启动多个本地模型进程。

每个子 Agent 的 `retry_count` 和有效处理时间独立追踪，不与其他角色或父任务相加。达到自己的
三次重试或 15 分钟阈值时，Commander 只收到一次“需要决策、子 Agent 继续工作”的通知；外部
Provider 失败也不会暂停其本地工作。

Commander 完成子任务后，主审查官会读取每个隔离 workspace 的基线差异、报告和验证状态，生成
合并计划、冲突列表、修改文件清单和进度摘要。主工作区不会自动写入；批准生成的合并确认卡后，
才执行逐文件合并。命令示例：

```bash
python3 scripts/agentrelay_console.py commander-merge task-1 \
  --workspace /path/to/project --runtime-root .agentrelay/commander
```

如果主工作区在审查期间被用户修改，或两个角色对同一文件给出不同内容，合并会暂停并报告冲突，
不会覆盖用户文件。

工作流分为三档：单一低风险修改直接处理；多个普通修改点生成一张汇总执行确认卡；跨模块或
高风险任务先显示需求卡，用户确认需求后再显示执行卡。若评估建议 Commander，第二张卡会改为
“启用审查官模式”，未批准前不会启动子 Agent。CLI 默认使用文本确认；`--json` 输出可供支持
勾选项/自定义输入的宿主界面渲染，普通终端仍安全回退为默认确认卡。

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

直接在终端运行 `python3 install.py` 会进入全中文安装菜单。菜单里的“初始化当前脚本”会安装
Hook、Skill、Tracker、确认卡和离线规则；DeepSeek 登录、本地模型和 API 都可以跳过，跳过后仍可
使用离线基础模式。菜单还可以单独开启 Commander、设置子 Agent 使用的终端和并发数，或在之后
重新配置三类 Provider。

安装器支持 `CODEX_HOME`；未设置时使用 `$HOME/.codex`。它会：

- 安装 Hook 到 `$CODEX_HOME/hooks/`
- 安装 Skill 和脚本到 `$CODEX_HOME/skills/agent-relay/`
- 创建独立环境 `$CODEX_HOME/agentrelay-env/`
- 在独立环境中自动安装 requirements 和完整 Chromium
- 保留用户已有文件
- 备份并原子合并现有 `hooks.json`，不删除任何已有 Hook
- 检查 Codex CLI、Python、虚拟环境、Playwright 和 Chromium

Provider、Commander 和队列设置会保存到 `$CODEX_HOME/skills/agent-relay/config/` 下的加密文件，
密钥也只在该目录保存并限制当前用户读取。API Key 和网页登录状态不会写进项目目录、日志或 Git；
安装器也不会把它们打印出来。运行时会自动读取本地配置，显式环境变量优先。

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
$CODEX_HOME/skills/agent-relay/agent_relay_login_state.json.enc
```

登录需要人工浏览器认证，因为 DeepSeek CAPTCHA 不能也不会被 AgentRelay 绕过。请勿将
登录状态文件、Cookie 或浏览器数据提交到版本库。

菜单中的“自定义网页模型”可以打开用户提供的 HTTPS 登录地址并把会话状态加密保存；但只有已经
实现浏览器 Adapter 的网页 Provider 才能直接对话。千问、Kimi 和其他自定义网站目前会完成安全
登录状态保存并提示适配器状态，不会伪造成已经可调用。

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
- `AGENT_RELAY_PROVIDER`：在线咨询脚本的 Provider，默认值为 `deepseek`；主编排流程默认离线，不依赖该 Provider
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
