<div align="center">

# 🔀 AgentRelay

### 给你的 coding Agent 配一个会提醒、会找外援、还能分工协作的调度台

**Codex · Claude Code · Kimi Code · Pi · Hermes 完整支持**

[🚀 快速开始](#-快速开始) · [✨ 能做什么](#-能做什么) · [🧭 怎么使用](#-怎么使用) · [🛠️ 实现原理](#️-它是怎么实现的)

</div>

---

## 🎯 这是什么东西？

AgentRelay 是一个运行在本机的 **Agent 外援与协作系统**。

它不替代你的主 Agent，而是在主 Agent 卡住、任务太耗时，或者你明确要求外援时，提供：

- 🔔 重复失败和耗时过长提醒
- 🌐 网页、API、本地模型三类外援
- 🗂️ 本地经验归档
- 🖼️ 图片识别中继
- ⚡ 可并行工作的 AgentFusion
- 👑 最多 1 到 5 个隔离子 Agent 的 Commander

可以把它理解成：

```text
主 Agent = 项目负责人
Hook     = 本地预检员，发现大任务、危险命令和卡住迹象
Skill    = 协作规则，决定是否需要确认或找外援
Fusion   = 独立副手，适合调查、批量阅读和局部实现
Commander = 临时项目组，按任务拆分多个隔离 Agent
```

> 🧠 **一句话记住：**<br>
> 主 Agent 负责判断和验收，AgentRelay 负责在合适的时候找到额外帮手。

---

## ✨ 能做什么？

### 🎁 三个核心能力

| 能力 | 一句话说明 | 你什么时候会用到 |
|---|---|---|
| 🔔 **AgentRelay** | 观察 Agent 会话，在失败、超时或你明确要求时给出外援提醒 | 同一个问题反复失败、改了半小时还没进展 |
| 🖼️ **AgentImage** | 让支持识图的在线或本地模型先描述图片 | 当前模型不能看图 |
| ⚡ **AgentFusion** | 启动独立持久会话，执行调查、批量阅读、独立复现或局部实现 | 日志很多、搜索范围很大、需要一个独立副手 |

### 🚦 Hook 门禁

- 🧾 你提交消息后，Hook 先用本地规则判断是不是中大型任务；不会额外请求模型。
- 🛑 需要确认时，Agent 必须先展示需求卡/确认卡，批准前不能写文件。
- ⚠️ 遇到删除、破坏性 Git、依赖安装、数据库迁移、部署等高风险命令时直接拦下。
- ✅ 批准只对当前方案生效；危险命令只允许完全相同的输入重试一次。

### 👑 AgentRelay 里还有 Commander

Commander 适合跨模块或高风险任务：

- 👥 按任务实际需要启动 1 到 5 个隔离子 Agent
- 🧩 前端、后端、数据库、测试、Git 审计可以分别处理
- 🔒 每个 Agent 使用隔离 workspace
- 🙅 未经确认不会直接合并回你的工作区
- 🔁 某个角色提前完成时，可以继续协助其他角色

### 🌐 三种外援 Provider

| 类型 | 适合什么 | 例子 |
|---|---|---|
| 🌐 `web` | 已有网页账号，希望直接使用网页模型 | DeepSeek、千问、Kimi |
| 🔌 `api` | 有 OpenAI 兼容 API 或中转接口 | Kimi API、其他兼容网关 |
| 💻 `local` | 希望数据留在本机 | Ollama、LM Studio、vLLM、llama.cpp |

> ⚠️ **配置提醒：**<br>
> 配置中心里的 `local` 和 `api` 是**协作模型**，不是正在驱动 Codex 的主模型。<br>
> 不要把主模型本身，或者与主模型共用同一个服务、endpoint、API 的模型填进去。<br>
> 否则两边会争用同一个资源，出现排队、串行、超时，也会失去独立复核的价值。

### 🧮 并发、排队和路由边界

每个 Provider 都像一个小型服务台，所有 Codex 会话、Fusion 和 Commander 共用同一套名额：

| 类型 | 默认并发 | 适合怎么用 |
|---|---:|---|
| 🌐 网页模型 | 1 | 编号网页会话；每个会话同一时间只处理一个请求 |
| 💻 本地模型 | 1 | 默认假设本地服务只有一个推理槽位 |
| 🔌 API 模型 | 10 | 按账号限额调整；提高前先确认接口真的支持并发 |

- ⏳ 满载时先进入全局 FIFO 队列，默认最多等待 30 秒。
- 🛑 排队超时会返回明确的 `FUSION_PROVIDER_BUSY`，不会偷偷改成另一个模型继续执行。
- 🧹 每个请求持有短租约；调用方异常退出后，租约默认在 90 秒内自动回收。
- 🧠 需要读写文件或执行命令的任务只走 `api → local`。网页模型只能提供咨询、代码建议、
  网页搜索、数据整理或在配置支持时识图，不能获得本机读写和 shell 执行能力。
- 🔢 网页会话使用 `AgentRelay-DeepSeek-1`、`-2`、`-3` 这样的全局编号槽位；不同业务流程
  不复用彼此历史。没有空闲槽位时排队，不会通过混用旧上下文假装扩容。
- 🕵️ 发给网页模型前会替换识别到的密钥、令牌、邮箱和手机号；恢复映射只留在当前本机进程
  内存中，不写盘。遇到不完整私钥等无法安全处理的内容时会停止发送。

---

## 🚦 什么时候会触发？

采用和 Pi 类似的两层方式：Hook 做轻量预检，主 Agent 做最终语义判断。

| 触发场景 | 系统会做什么 |
|---|---|
| 📝 你提交一个中大型任务 | `UserPromptSubmit` 注入任务门禁，要求先展示确认卡 |
| 🧱 任务尚未批准但要写文件 | `PreToolUse` 硬阻断写入，只读检查和安全测试仍可继续 |
| ⚠️ 即将执行高风险命令 | 硬阻断并给出安全门禁，要求先解释影响、等待确认 |
| 🔁 同一问题重复失败 | 生成“可以找外援”的候选提醒 |
| ⏱️ 单问题有效处理时间过长 | 达到门槛后生成候选提醒 |
| 🗣️ 你明确说“现在调用 DeepSeek”“让 Fusion 看一下” | 进入显式协作流程 |
| 🗂️ 本地归档命中相似问题 | 先推荐历史结论，不直接调用在线模型 |

Hook 不会因为看到“AgentRelay”“模型”几个字就替 Agent 认定你在下命令。是否调用外援、
调用哪个 Provider，仍由主 Agent 结合完整语境判断。

### 🧾 确认卡到底会显示什么？

当 Hook 判断任务需要确认，或者 Agent 在没有获得批准时尝试写入文件、执行高风险命令，
会先出现一张确认卡。它不是为了增加步骤，而是让你在真正修改前看清楚：

- 🎯 准备做什么，和你原始需求是否一致
- 📂 可能影响哪些模块、文件和命令
- ⚠️ 有哪些风险，失败后怎么回滚
- 🧪 完成后准备运行哪些验证
- ✅ 本次批准到底允许哪些操作

AgentRelay 会根据场景使用三种卡片：

| 卡片 | 什么时候出现 | 主要用途 |
|---|---|---|
| 🧾 **需求确认卡** | 你提交了中大型任务，或者任务包含明确的重构、迁移、配置、依赖、部署等操作 | 先确认目标、范围、风险、验证和回滚方式 |
| 📝 **写入确认卡** | 任务仍在等待批准，但 Agent 尝试修改工作区文件 | 阻断写入，提醒 Agent 先取得你的明确批准 |
| 🛑 **危险操作确认卡** | 即将执行删除、破坏性 Git、数据库迁移、部署等高风险命令 | 展示实际命令和影响，只允许你针对同一输入单独确认 |

在 Codex CLI 中，卡片通过 Hook 的 `systemMessage` 显示在终端界面里。Codex 当前会把
`systemMessage` 当普通提示展示，不能保证 ANSI 颜色或 Markdown 样式生效，所以 AgentRelay
使用 emoji 给字段分区，并保持纯文本结构：

| 内容 | 显示方式 |
|---|---|
| 卡片类型 | 🧭 需求、📝 写入或 🛑 危险操作 |
| 任务和状态 | 🎯 任务、⏳ 当前状态 |
| 修改内容 | 📝 需求、📦 影响范围、📂 操作内容 |
| 风险和批准边界 | ⚠️ 主要风险、🔐 批准边界 |
| 验证和回滚 | 🧪 验证、↩️ 回滚 |
| 操作选项 | ✅ 批准、🛠️ 调整、⛔ 取消 |

这种格式不依赖终端颜色，在 Codex CLI 和其他支持 Hook `systemMessage` 的环境中都能稳定
阅读。`additionalContext` 只提供给主 Agent，不会变成用户可见的卡片。

下面是一个简化后的需求确认卡示例：

```text
🧭 AgentRelay / 需求确认卡
────────────────────────────────────────
🎯 任务
task-a1b2c3d4e5
📝 用户需求
重构整个项目的 Hook、Skill、安装流程和测试
🔎 触发原因
包含重构、配置、依赖或部署操作
🧭 处理方式
先核对修改目标和影响范围；你批准后再动文件，完成后运行验证。
📦 影响范围
模块：Hook、Skill、安装
文件：待读取相关代码后确认
⏳ 当前状态
waiting_for_approval
✅ 批准后
执行当前卡片展示的修改方案，并运行专项检查和完整验证。
⚠️ 主要风险
将修改工作区文件，方案或修改范围变化时需要重新确认。
🧪 验证
先运行与改动直接相关的测试或构建命令。
确认通过后，再运行项目已有的完整测试。
↩️ 回滚
修改前保留原文件内容；验证失败时按文件恢复，并记录失败原因。
────────────────────────────────────────
✅ [1] 批准执行
🛠️ [2] 调整方案
⛔ [3] 取消任务
```

卡片底部固定提供三个选择。它们是需要你回复的文字选项，不是 Codex 原生按钮：

- `[1]` 批准当前卡片展示的方案。不同卡片可能显示为“批准执行”“确认需求”或“启用审查”。
- `[2]` 要求 Agent 调整范围、风险处理或验证方式，调整后重新确认。
- `[3]` 取消当前任务。

批准只对当前卡片生效，不会自动扩展到其他文件、其他命令或之后的新需求。危险命令即使批准，
也只允许完全相同的输入重试一次。

### ⚡ AgentFusion 的触发方式

Fusion 能力默认开启，但 **不等于每轮都会自动干活，也不会自动选择 Provider**：

- ✅ 已配置可用 Provider，且状态为 `delegable=true`：主 Agent 可以委派
- ⏸️ 未配置 Provider、Fusion 已关闭或 Provider 不可用：立即跳过，不等待、不探网
- ⏳ Provider 满载：按配置进入 FIFO 队列；超时后明确报告忙，不会静默降级成网页执行
- 👑 Commander 运行时：Fusion 强制暂停，避免两个调度器互相抢占
- 🎯 Provider 必须显式指定，例如 `web:deepseek-web`、`local:local-llm` 或
  `api:kimi-api`；没有 `provider_id` 时不会偷偷借用 Codex 主模型
- 🌐 指定网页 Provider 时只能做咨询；需要 Fusion 实际改文件或跑命令时必须选择 API 或本地模型

Fusion 适合“输出很多、边界清楚、可以独立完成”的工作，例如：

- 跑完整测试并汇总数百行日志
- 跨多个文件搜索、批量阅读和整理
- 独立复现一个 bug
- 按主 Agent 已确定的方案修改局部代码

需求仍然模糊、涉及密钥、必须由主 Agent 做最终判断的工作，不会交给 Fusion。

---

## 🚀 快速开始

### 1️⃣ 准备环境

需要：

- 🐍 Python 3.10 或更高版本
- 📦 Python `venv`
- 🤖 至少一个支持的 Agent，例如 Codex CLI
- 🌐 首次安装时可访问 PyPI 和 Playwright 下载源
- 🔐 对应网页、API 或本地模型的访问权限

### 2️⃣ 安装

```bash
git clone git@github.com:Geek-Dream/AgentRelay-Hub.git
cd AgentRelay-Hub
python3 install.py
```

Windows 可以使用：

```powershell
py install.py
```

安装器会：

- 📦 安装 `agent-relay`、`agent-image`、`agent-fusion`
- 🐍 创建独立环境 `$CODEX_HOME/agentrelay-env/`
- 🌐 安装 Playwright Chromium
- 🔐 加密保存 Provider 配置
- 🔗 注册 Agent Hook
- ✅ 自动检查 Skill、Hook、Tracker 和合成事件

如果你以前装过旧版，重新运行 `python3 install.py` 即可。安装器会识别旧的
`agent_relay_hook.sh` 命令，升级为新版 `.py` 入口，清理重复的旧 AgentRelay Hook，
同时保留你自己已有的 Hook。

安装完成后，重启正在使用的 Agent，让 Hook 重新加载。

### 3️⃣ 打开网页配置中心

```bash
python3 install.py --menu
```

选择“打开本地网页配置”，或者安装时直接输入：

```text
0
```

也可以输入：

```text
web
```

在配置中心依次完成：

1. 🌐 添加网页、本地或 API 模型
2. 🔑 网页模型点击“打开网页登录”，手动完成登录
3. 🧪 点击“测试”确认模型可用
4. 🧮 为每个协作模型设置最大并发、排队超时和租约回收时间
5. 👑 按需启用 Commander
6. ✅ 点击“完成并关闭”

> 🔒 登录状态和 API Key 只保存在本机加密目录。<br>
> 🚫 AgentRelay 不会绕过 CAPTCHA，也不会自动填写账号密码。

---

## 🧭 怎么使用？

### 💬 日常使用：不用记命令

安装并配置 Provider 后，继续正常使用 Codex、Claude Code、Kimi Code、Pi 或 Hermes 即可。

当问题重复失败、耗时过长，或者你明确说出：

```text
现在调用 DeepSeek
请让 Fusion 独立检查这个问题
用 AgentRelay 查一下本地归档
```

Agent 会结合 Hook 提醒和 Skill 规则决定是否调用外援。

### ⚡ 使用 AgentFusion

Pi 中直接输入：

```text
/agent-fusion
```

会打开中文选择菜单，可以开关 Fusion、选择 Provider、切换 `readonly` / `coding` 模式和重置会话。

其他 Agent 通常只需自然语言提出委派需求，例如：

```text
请先检查 AgentFusion 状态。
如果 delegable=true，就把测试日志调查交给 Fusion；
如果不可用，不要等待，继续本地处理。
```

如果要直接使用 CLI：

```bash
PYTHON="${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python"
FUSION="${CODEX_HOME:-$HOME/.codex}/skills/agent-fusion/scripts/agent_fusion.py"

"$PYTHON" "$FUSION" status --json
"$PYTHON" "$FUSION" provider web:deepseek-web
"$PYTHON" "$FUSION" mode readonly
```

需要允许 Fusion 写文件和执行命令时：

```bash
"$PYTHON" "$FUSION" mode coding --confirm-coding
```

> 🛡️ `coding` 模式会真实修改工作区并执行命令。只在任务边界清楚、你明确同意时使用。

### 🖼️ 使用 AgentImage

当前模型不能识图时，可以显式调用：

```bash
python3 scripts/agent_image.py /path/to/image.png
```

也可以使用 Agent 提供的 `$agent-image` 或 `@agent-image` 入口。

AgentImage 不会自动拦截所有图片，只有你明确要求识图时才执行。

### 👑 使用 Commander

遇到跨模块、高风险或适合并行的任务，可以明确告诉主 Agent：

```text
这个任务跨前端和后端，请启用 Commander，
拆分隔离 Agent，完成后先审查，再逐文件合并。
```

Commander 会：

1. 🧾 生成需求和执行计划
2. 👥 创建 1 到 5 个隔离 Agent
3. 🔍 汇总结果并做审查
4. ✅ 经你确认后才合并回主工作区

主工作区在审查期间被修改，或者两个 Agent 对同一文件产生冲突时，合并会暂停，不会覆盖你的文件。

---

## 🛠️ 支持的 Agent

| Agent | 支持情况 | 说明 |
|---|---|---|
| 🟢 Codex CLI | 完整支持 | 始终安装 |
| 🟢 Claude Code | 完整支持 | 自动合并 `hooks` 配置 |
| 🟢 Kimi Code | 完整支持 | 支持失败与 Stop Hook |
| 🟢 Pi | 完整支持 | 提供原生 Fusion 工具与中文菜单 |
| 🟢 Hermes | 完整支持 | 首次触发需要在终端确认 |
| 🟡 OpenCode | 仅 Skill | 没有 Prompt 注入 Hook |
| 🔴 deepseek-cli | 不支持 | 没有 Hook、插件或 Skill 机制 |

安装器只新增或更新 AgentRelay 自己的配置，不会删除已有 Hook。

---

## ✅ 安装后检查

查看 Tracker 状态：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" status
```

查看最近问题：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" problems
```

正常的新状态应包含：

```text
retry_count: 0
relay_triggered: false
relay_call_count: 0
should_trigger: false
```

如果 Hook 没有反应，先重启 Agent，再检查：

```text
$CODEX_HOME/hooks.json
$CODEX_HOME/agent_relay_tracker/logs/error.log
```

---

## 🩺 常见问题

| 现象 | 先检查什么 |
|---|---|
| 🔔 Hook 没反应 | 是否重启 Agent；`hooks.json` 是否包含 `agent_relay_hook.py` |
| 🧾 Codex 看不到确认卡 | 是否重启过 Codex；在 Codex 中运行 `/hooks` 检查 Hook 是否已信任；配置和 Skill 是否为最新版 |
| 🔑 网页登录失效 | 重新打开配置中心并登录；不要手动编辑登录状态文件 |
| 🌐 网页保存了但不能自动对话 | 网站是否有适配器；DeepSeek 已内置，其他网站能力取决于当前 Adapter |
| ⚡ Fusion 不工作 | 是否配置 Provider；`status --json` 是否返回 `delegable=true` |
| ⏳ Fusion 一直排队或返回忙 | 查看该 Provider 的 `max_concurrency`；确认没有把主模型或同一本地服务重复填入 |
| 👑 Commander 合并暂停 | 主工作区是否被改动；不同 Agent 是否修改了同一个文件 |
| 🖥️ Chromium 找不到 | 重新运行 `python3 install.py`，或设置 `AGENT_RELAY_BROWSER_PATH` |

---

## 🛡️ 安全边界

- 🚫 不绕过 CAPTCHA，不保存账号密码
- 🔒 任务门禁只做本地规则预检，不额外调用第二个模型
- 🛑 Codex 当前不支持 Hook 原生 `ask` 审批按钮；AgentRelay 用
  `deny + systemMessage 纯文本 emoji 确认卡 + 原输入重试` 实现安全确认
- 🧠 外部模型建议必须由主 Agent 验证，不能直接当作最终答案
- 🧰 Fusion 只能访问指定 workspace
- 👑 Commander 使用隔离 workspace，默认不会覆盖主工作区
- 🕵️ 网页咨询发送前做可逆脱敏；敏感映射只保存在当前进程内存，不写入项目或运行文件
- 🔐 Cookie、登录状态和 API Key 不写入项目、日志或 Git

---

## ⚙️ 它是怎么实现的？

### 🪝 五个 Hook 各做什么

| Hook 事件 | AgentRelay 用来做什么 |
|---|---|
| `SessionStart` | 初始化会话状态，恢复安全边界 |
| `UserPromptSubmit` | 本地预检任务大小，注入任务门禁和确认要求 |
| `PreToolUse` | 检查危险命令和未批准写入，必要时直接 `deny` |
| `PostToolUse` | 记录工具结果、失败次数和处理耗时 |
| `Stop` | 判断本轮是否结束，必要时注入后续提醒 |

Codex 的 Hook schema 虽然列出了 `permissionDecision: ask`，但当前官方解析器明确拒绝它。
因此 AgentRelay 不依赖这个无效选项，而是使用可验证的：

```text
PreToolUse deny
   ↓
Agent 展示确认卡
   ↓
用户明确批准
   ↓
只重试完全相同的工具输入一次
```

### 🔁 一次外援请求的完整链路

```text
Agent 会话
   │
   ▼
Hook 观察失败次数、有效耗时和显式调用语句
   │
   ▼
Tracker 按 Session 和问题隔离状态
   │
   ▼
向 Agent 注入候选提醒 additionalContext
   │
   ▼
Agent 结合上下文判断：现在是否需要外援
   │
   ▼
Skill 选择 web / api / local Provider
   │
   ▼
主 Agent 按能力判断：API / 本地可执行，网页只提供咨询
   │
   ▼
Provider 租约或编号网页会话槽位（全局 FIFO 排队）
   │
   ▼
网页请求发送前脱敏，返回后在本机恢复原值
   │
   ▼
Provider Adapter 调用外部模型
   │
   ▼
主 Agent 验证结果、修改代码并重新测试
```

### 🧩 主要模块

| 模块 | 作用 |
|---|---|
| `hooks/` | 监听 Agent 事件、计时、记录重试和生成提醒 |
| `skills/agent-relay/` | 决定什么时候查询 Provider、归档和发起协作 |
| `skills/agent-image/` | 把图片交给支持识图的 Provider |
| `skills/agent-fusion/` | 管理 Fusion 开关、Provider、模式、会话和工具循环 |
| `scripts/agent_provider.py` | 统一处理网页、API 和本地模型 |
| `scripts/provider_leases.py` | 跨进程 Provider 并发租约、FIFO 排队和异常回收 |
| `scripts/web_session_pool.py` | 全局编号网页会话、业务流程隔离和槽位复用 |
| `scripts/sensitive_redaction.py` | 网页咨询前的可逆脱敏与本机恢复 |
| `scripts/model_router.py` | 根据能力选择可执行或只读咨询 Provider |
| `scripts/fusion_runner.py` | Fusion 的持久会话、工具调用、步数限制和交接 |
| `scripts/research_commander.py` | Commander 的角色规划、隔离执行和结果汇总 |
| `scripts/agentrelay_console.py` | 确认卡、Commander 和逐文件合并入口 |
| `scripts/config_web.py` | 本机网页配置中心 |

### ⚖️ 触发权重

AgentRelay 使用简单的时间与失败阶梯，不靠无限轮询：

| 权重 | 条件 | 行为 |
|---|---|---|
| 1️⃣ | 刚开始处理，未命中归档 | 正常继续 |
| 2️⃣ | 5 到 15 分钟，或命中归档 | 先查本地历史结论 |
| 3️⃣ | 15 分钟以上，或重复失败明显 | 进入“是否叫外援”的判断 |

系统只给候选建议，不会越过主 Agent 直接做决定。

### 🧮 Provider 调度怎么工作？

- 每个 Provider 的并发上限由所有 Agent 进程共享，不按单个会话重新计算。
- 调用开始前先获取租约；轮到谁由持久化队列的 ticket 顺序决定。
- 租约记录 PID、心跳和过期时间。进程崩溃后，不再存活或过期的名额会被下一个调用回收。
- 网页 Provider 使用同一套租约，但每个名额对应一个编号会话和业务流程绑定范围。
- 需要执行本机操作时，路由器按 `API → 本地` 选择；网页 Provider 会被标记为 advisory，
  即使它满载、排队或不可用，也不能被当成隐式执行器。
- 队列超时会返回结构化错误，主 Agent 必须报告“当前 Provider 忙”，不能伪造成功或静默
  换用另一种能力。

### ⚡ Fusion 为什么不会递归？

- Fusion 子进程会设置 `AGENTRELAY_FUSION_CHILD=1`
- Commander 子进程会设置 `AGENTRELAY_COMMANDER_CHILD=1`
- 子 Agent 不允许再次启动 Fusion 或 Commander
- Commander 运行时持有最高优先级锁，Fusion 会在下一次模型或工具调用前暂停
- 被中断的 Fusion 会生成交接快照，不会静默丢失工作

### 👑 Commander 如何避免写坏项目？

- 每个子 Agent 使用独立 workspace
- 主 Agent 负责最终审查
- 合并前检查主工作区是否发生变化
- 同一个文件出现多个不同版本时停止自动合并
- 只有明确确认后才逐文件写回

### 🧪 本地验证

运行主要测试：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  -m unittest discover -s tests -v
```

检查差异格式：

```bash
git diff --check
```

真实页面检查（Chromium 必须已安装；失败返回 1，不会把跳过当通过）：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  scripts/verify_page.py --output runtime/page-verification
```

检查会实际打开配置中心，点击全部导航、切换明暗主题，检查桌面与手机布局，并保存截图、
控制台错误和 `result.json`。修改 HTML/CSS/JS/页面模板后，Hook 会按会话记录文件哈希；
声称“页面完成/验证通过”时，必须有覆盖当前文件版本的真实浏览器证据，否则 Stop 会阻断。
再次修改文件会使旧证据失效；非页面任务不触发。CI 同样运行页面检查并上传截图。

Hook 和 Provider 租约都按需运行，不需要常驻服务。网页配置服务在关闭页面后停止；可选的
`agentrelay_daemon.py` 只负责周期监控和汇总 Provider 租约状态，不是 Fusion 调度的前提。

---

<div align="center">

### ⭐ 如果 AgentRelay 对你有帮助，欢迎 Star

**MIT License** · 详见 [LICENSE](LICENSE)

</div>
