---
name: agent-fusion
description: "Delegate bulky or isolated execution work to AgentRelay Fusion's persistent sidekick, including test suites, wide searches, batch reading, mechanical scans, independent reproduction, and well-scoped implementation. Use it before doing qualifying work locally when the local status returns delegable=true; queue only within the configured timeout, expose a busy timeout instead of silently switching to web, and skip when Fusion is disabled, has no configured Provider, or Commander is active. Default readonly; coding requires an explicit switch."
---

# AgentFusion

AgentFusion 是 AgentRelay 的持久协助 Agent：默认开启，但只有用户显式配置 Provider 后才可
调用。它拥有自己的会话、上下文和工具循环，可并行承担耗时的调查、批量阅读和用户明确委派
的实现工作；主 Agent 仍负责需求判断、规划、最终审查和验收。

Fusion 永不启动另一个 Fusion，也永不启动 Commander。Commander 拥有最高优先级，启动时
会接管并关闭正在运行的 Fusion 任务。

## 委派决策

## 默认辅助、忘记调用补救与失败降级

- 对独立调查、批量阅读、代码审查、回归复核等适合委派的任务，先查 `delegable` 并实际
  `delegate`；只检查状态或收到提醒不等于 Fusion 已工作，不能声称“已复核”。
- Hook 不提醒、不阻塞 Stop，也不会因为关键词自动调用 Fusion。是否委派由主 Agent
  根据任务边界判断；用户显式要求 Fusion 时直接执行下面的 `delegate`。
- `scripts/fusion_assist.py delegate --session <id> --home <CODEX_HOME> --prompt <任务> --workspace <路径>`
  通过现有 `agent_fusion.py delegate` 调用一次，并把结果写入会话级 `fusion-assist` 证据。
- 未配置 Provider、Commander/Fusion 子会话或不适合的任务直接本地处理，不打扰。
  API 过期、网络失败或调用异常后主 Agent 继续完成任务；回复说明“Fusion 调用失败，
  请检查 Provider；本次任务由主 Agent 继续完成”。超时、调用失败、结果缺失或 Provider
  繁忙会在当前会话打开熔断，后续 `delegate` 不再次调用 Provider。
- 熔断后只有 `scripts/fusion_assist.py retry` 或 `delegate(..., retry=True)` 可以重新尝试；
  成功结果或显式重试会更新会话状态。
- Fusion 成功也需主 Agent 审查与独立验收；没有真实成功结果不能说 Fusion 已复核。
- `AGENTRELAY_FUSION_ASSIST=off` 仍可让手动 `remind` 静默，不改变 Fusion 开关或 Provider。


像 Pi 的双 Agent harness 一样，主 Agent 应保留计划、歧义解释和最终验收，把高输出量或可
隔离的执行工作默认交给 Fusion。判断依据是任务会产生多少输出，而不是步骤数。

满足以下任一条件，且状态为 `delegable=true` 时，先委派再继续本地工作：

- 测试套件、构建或静态检查会产生数百行日志。
- 需要跨多个文件搜索、批量阅读、机械扫描或汇总大量结果。
- 需要独立复现问题、收集证据或做一轮独立验证。
- 任务边界已经明确，可以写成自包含的实现、重构或测试修复工作。

以下任务由主 Agent 直接完成：

- 一两行就能回答的查询，或很小的定点修改。
- 需求仍然模糊，必须先和用户澄清。
- 判断本身就是交付物，或最终审查必须由主 Agent 独立完成。
- 涉及密钥、Cookie、登录状态，或与当前工作产生不可协调的写冲突。
- Fusion 已关闭、未配置 Provider、正在忙、被 Commander 抢占或返回不可用。

委派时不要只给一句“看一下”。`task` 要说明做什么和在哪里做；`context` 要包含主 Agent
已经确定的计划、约束、相关文件和决定；`expect` 要说明需要返回的报告、改动和验证证据。
状态 `delegable=true` 只表示可以调用，只有真正调用 delegate 或 `agent_fusion_delegate`
后 Fusion 才在干活。

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
- `state.degraded=true` 或 `state.fallback_reason=PROVIDER_QUEUE_ACTIVE`：Provider 已满载，
  只能在配置的排队时间内等待。超时返回 `FUSION_PROVIDER_BUSY`，必须明确报告，不能静默
  改成本地执行，也不能把需要读写文件的 coding 任务降级给网页模型。

未配置 Provider 时状态检查在本地立即结束，不能让主任务等待外部模型。

Pi 中优先调用原生 `agent_fusion_delegate` 工具。其他目标没有等价的原生委派工具时，
才使用下面的 CLI。

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

### 共享并发与租约

- 每个 Provider 的并发上限由全部 Codex 会话、Fusion 和 Commander 共享。
- 默认上限是网页 `1`、本地 `1`、API `10`；每个 Provider 可以单独配置。
- 满载请求按持久化 ticket 顺序排队，默认最多等待 30 秒。
- 单个请求持有租约，默认 TTL 为 90 秒；进程退出或心跳过期后名额会自动回收。
- 排队超时返回 `FUSION_PROVIDER_BUSY`。不要把 busy 当成“可以偷偷换网页模型”。

配置 Provider 时不要填写正在驱动当前 Agent 的主模型，也不要填写与主模型共用的
endpoint、API 或同一个本地服务。那会造成资源争用、串行、超时和错误的性能判断。

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

### 网页 Provider 的 advisory 边界

网页 Provider 适合咨询、简单代码建议、网络搜索、数据整理，以及在显式配置支持时识图。
它们没有 Fusion 的工具协议，因此不能获得本机文件读写或 shell 执行能力。

需要实际执行本机任务时，Fusion 的合法路由顺序是 `API → 本地`。即使网页 Provider 可用、
并发更高或排队更短，也不能用它顶替 API 或本地模型来执行 coding 任务。

### 网页会话与脱敏

- 每个网页 Provider 使用 `AgentRelay-<Provider>-1`、`-2`、`-3` 等全局编号会话槽位。
- 同一 Provider 的槽位被所有 Agent 共享；不同业务流程使用不同的上下文绑定范围。
- 已绑定的旧网页会话只允许被兼容流程接管一次。之后的新业务流程从干净会话开始。
- 所有槽位都忙时进入队列，达到上限后不会额外创建超量会话。
- 网页请求发出前会替换已识别的 API Key、Bearer 令牌、密码、私钥、邮箱和手机号。
- 可逆映射只保留在当前本机进程内存，不落盘；返回文本到达本机后再恢复原值。
- 遇到不完整私钥等无法安全脱敏的内容时停止发送，不能把原文降级发给网页模型。

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

## 超时与失败诊断

Fusion 调用会写入有界 JSONL 诊断日志：

```bash
tail -n 100 "${CODEX_HOME:-$HOME/.codex}/agent_relay_tracker/logs/fusion-trace.jsonl"
```

日志只记录 trace ID、会话 ID、阶段、耗时、Provider 类型、模型、错误类型和少量状态，
不记录完整 Prompt、API Key 或 Provider 回答。超时或失败结果会带回 `trace_path`、
`last_stage` 和最近 12 条 `trace_tail`，优先看 `last_stage`：

- `provider_queue_wait`：仍在本机 Provider 租约队列等待。
- `provider_request`：请求已经发出，正在等上游 Provider。
- `provider_error`：上游 HTTP/API 已返回错误，查看同一条 trace 的 `error` 和
  `error_type`。
- `model_request`：Fusion Runner 正在执行某一轮模型调用。
- `tool_read_file`、`tool_grep`、`tool_run_command`：具体本地工具仍在执行。
- `runner_complete`、`subprocess_complete`：业务已经返回，问题可能在收尾或读取结果。

不要因为超时直接放大外层 120 秒限制；先根据最后阶段确认是排队、上游请求、模型多步循环、
本地工具还是收尾问题，再调整对应配置。

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
- 需要本机执行时只选择 `api` 或 `local`；网页 Provider 始终按 advisory 处理。
- Provider 满载时按租约排队，超时要明确报告，不能静默换 Provider 或改变任务能力。
- 不让 Fusion 自己启动 Commander；脚本通过 `AGENTRELAY_FUSION_CHILD=1` 拒绝递归。
- 不在 Commander 子 Agent 中调用 Fusion；脚本通过 `AGENTRELAY_COMMANDER_CHILD=1` 拒绝。
- `agentrelay_daemon.py` 是可选的监控和状态汇总进程，不是 Fusion 租约或调度的必要条件。
