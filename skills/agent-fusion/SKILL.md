---
name: agent-fusion
description: "AgentRelay Fusion 默认开启的持久 sidekick。仅在 `/agent-fusion status` 返回 delegable=true，或用户显式调用 agent-fusion 时使用；通过当前已配置的单个 web/local/api Provider 建立按 workspace 和 session 隔离的持久会话，默认 readonly，可按用户明确选择切换 coding。未配置 Provider、Commander 正在运行、当前进程是 Commander/Fusion 子会话，或 Fusion 已关闭时立即跳过，不探测登录、不打开浏览器、不回退默认 Provider。"
---

# AgentFusion

AgentFusion 是 AgentRelay 的持久协助 Agent：默认开启，但只有用户显式配置 Provider 后才可
调用。它拥有自己的会话、上下文和工具循环，可并行承担耗时的调查、批量阅读和用户明确委派
的实现工作；主 Agent 仍负责需求判断、规划、最终审查和验收。

Fusion 永不启动另一个 Fusion，也永不启动 Commander。Commander 拥有最高优先级，启动时
会接管并关闭正在运行的 Fusion 任务。

## 先检查本地状态

必须使用 AgentRelay 独立 Python：

- macOS/Linux：`${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python`
- Windows：`%CODEX_HOME%\agentrelay-env\Scripts\python.exe`

下面的命令以 macOS/Linux shell 为例；Windows PowerShell 使用对应的
`Scripts\python.exe` 路径，并把命令开头的 `"$PYTHON"` 改为 `& $Python`。

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  "${CODEX_HOME:-$HOME/.codex}/skills/agent-fusion/scripts/agent_fusion.py" \
  status --json
```

输出以 `AGENT_FUSION_RESULT=` 开头。读取 `state`：

- `state.delegable` 必须为 `true` 才能委派。
- `state.tool_mode` 是当前模式：默认 `readonly`，用户明确确认后可设为 `coding`。
- `state.max_steps` 是单次委派最多允许的模型步数。
- `FUSION_PROVIDER_NOT_CONFIGURED`：静默跳过。除非用户正在操作 Fusion，否则不要提醒，
  不要等待、探网、检查登录或尝试默认 Provider。
- `COMMANDER_ACTIVE`、`COMMANDER_CHILD` 或 `FUSION_CHILD`：强制互斥，立即跳过。
- `FUSION_DISABLED`：跳过。只有用户明确要求开启时才执行开关命令。

未配置 Provider 时状态检查在本地立即结束，不能让主任务等待外部模型。

## 开关、Provider 与模式

Pi 中直接输入 `/agent-fusion` 会显示中文选择菜单，每个选项都有用途说明。显式命令用于
脚本化和精确操作：

```bash
PYTHON="${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python"
FUSION="${CODEX_HOME:-$HOME/.codex}/skills/agent-fusion/scripts/agent_fusion.py"

"$PYTHON" "$FUSION" on
"$PYTHON" "$FUSION" off
"$PYTHON" "$FUSION" status --json
"$PYTHON" "$FUSION" mode readonly
"$PYTHON" "$FUSION" mode coding --confirm-coding
"$PYTHON" "$FUSION" reset --workspace /path/to/project --session-id main-session
"$PYTHON" "$FUSION" provider web:deepseek-web
"$PYTHON" "$FUSION" provider local:local-llm
"$PYTHON" "$FUSION" provider api:kimi-api
"$PYTHON" "$FUSION" provider none
```

`on` 只开启能力，不会自动选择 Provider。Fusion 永不猜测默认 Provider；必须使用
`web:<id>`、`local:<id>` 或 `api:<id>` 精确指定。

### readonly

默认模式。允许：

- `read_file`：读取工作区文本文件。
- `list_files`：按 glob 列出文件。
- `grep`：搜索工作区文本。

禁止写文件、执行 shell 命令或访问工作区外路径。

### coding

仅 `local` 和 `api` Provider 可用。除 readonly 工具外，还允许：

- `write_file`：创建或替换工作区内文件。
- `run_command`：在工作区执行 shell 命令。

coding 模式可以直接修改文件并执行命令，必须由用户明确确认后才能启用。网页 Provider 没有
工具协议，不能切换到 coding。

## 发起委派

状态允许后，使用明确的 workspace 和 session ID 保持会话隔离：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  "${CODEX_HOME:-$HOME/.codex}/skills/agent-fusion/scripts/agent_fusion.py" \
  delegate "具体可执行任务" \
  --workspace "/path/to/project" \
  --session-id "main-session" \
  --context "环境、约束、相关文件和期望输出"
```

同一 workspace 与 session ID 会复用持久上下文。Provider、workspace 或工具模式变化时，
脚本自动创建新会话，避免历史污染。`reset` 可显式清除当前会话。

回复在 `AGENT_FUSION_RESULT.answer` 中，同时包含 `tools_used`、`steps`、`handoff_path`
等执行证据。主 Agent 必须：

1. 审查 Fusion 的报告、改动文件和验证结果是否符合用户约束。
2. 对 coding 模式的写文件和命令结果重新验证。
3. 在 Fusion 未配置、失败或不可用时继续本地处理，不阻塞主任务。

## Commander 抢占与会话隔离

- Commander 启动时写入最高优先级运行锁，并把当前活动的 Fusion run 标记为中断。
- Fusion 在每一步模型调用和每一个工具调用前重新检查锁，不只在任务开始时检查。
- 被抢占时 Fusion 生成 handoff 快照，记录任务、活动、改动文件和命令，并关闭旧任务。
- Commander 子 Agent 只读取结构化 handoff，不复用 Fusion 旧会话；它先接手 Fusion 遗留
  工作，再执行 Commander 主任务。
- Commander 结束时会按 Fusion 会话发送 scoped 回执，明确每个遗留项是 completed 还是
  failed，并携带报告、改动文件和可用验证证据。没有证据的遗留项不能标记为完成。
- 旧 Fusion 任务不会自动恢复；Fusion 回到空闲，等待主 Agent 的下一次明确委派。
- 主 Agent、Fusion、Commander 及其子 Agent 各自使用独立会话和上下文，不共享工具历史。
- Commander 运行期间，主流程和全部 Commander 子 Agent 都不能调用或启用 Fusion。

## 边界

- 不把密钥、Cookie、登录状态或无关的大段源码放进任务上下文。
- Fusion 不能访问 workspace 之外的路径。
- coding 模式能执行真实命令，必须遵守当前用户的授权范围和项目验证流程。
- 不让 Fusion 自己启动 Commander；脚本通过 `AGENTRELAY_FUSION_CHILD=1` 拒绝递归。
- 不在 Commander 子 Agent 中调用 Fusion；脚本通过 `AGENTRELAY_COMMANDER_CHILD=1` 拒绝。
