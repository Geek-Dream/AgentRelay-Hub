# AgentRelay

AgentRelay 是一个运行在本机的 AI 编程 Agent 协作层。

它不替代 Codex、Claude Code、Kimi Code、Pi 或 Hermes，而是在这些 Agent 工作时补上四类能力：

- 任务重复失败或耗时过长时，提醒主 Agent 考虑找外援。
- 修改范围不清楚时，先生成有边界的确认卡，批准后才允许写文件。
- 把大批量调查、日志分析或局部实现交给 AgentFusion。
- 把跨模块任务拆给 Commander 的多个隔离子 Agent，再统一审查和合并。

一句话理解：

```text
主 Agent = 项目负责人，负责理解需求、做决定和最终验收
AgentRelay = 项目负责人的安全员、调度台和外部协作通道
```

AgentRelay 的目标不是把每个小任务都做得很重。简单问答和只读检查不会建卡；清晰、局部、
低风险的修改可以走轻量流程；只有复杂、跨文件、共享、高风险或用户明确要求完整审查的任务，
才进入更严格的确认和审计链路。

> 当前状态：macOS 主流程、单元测试和真实浏览器页面检查是主要验证路径。Windows、Hermes
> 等宿主已经有代码路径和自动化测试，但仍需要在对应真实环境中补做端到端验收，不能仅凭
> macOS 测试宣称全平台完成。

## 目录

- [30 秒看懂](#30-秒看懂)
- [新手安装](#新手安装)
- [第一次使用](#第一次使用)
- [什么时候会出现确认卡](#什么时候会出现确认卡)
- [权限模式与工作档](#权限模式与工作档)
- [AgentFusion 和 Commander](#agentfusion-和-commander)
- [支持的 Agent](#支持的-agent)
- [常见问题](#常见问题)
- [安全边界](#安全边界)
- [工作原理](#工作原理)
- [开发与测试](#开发与测试)

## 30 秒看懂

| 名词 | 大白话解释 |
|---|---|
| `Hook` | 主 Agent 每次提交任务、调用工具或结束时触发的本地检查器 |
| `Skill` | 交给 Agent 阅读的协作规则，告诉它何时确认、找外援或审查 |
| `Provider` | 外部帮手，可以是网页模型、OpenAI 兼容 API 或本地模型 |
| `AgentFusion` | 一个持久的外援会话，适合处理大日志、大范围阅读和边界明确的局部任务 |
| `Commander` | 临时项目组，最多启动 5 个隔离子 Agent 分工完成复杂任务 |
| `AgentImage` | 当前主模型不能识图时，把图片交给支持视觉的模型描述 |
| `确认卡` | 真正修改前给用户看的“准备做什么、改哪里、不能碰什么、怎么验收” |
| `工作档` | Agent 为当前写任务选择的流程深度，从轻量到完整审查 |

AgentRelay 负责提供边界、证据和协作能力，最终判断仍由主 Agent 负责。网页模型给出的建议
不能直接当答案，必须由主 Agent 结合代码和测试验证。

## 新手安装

### 1. 准备环境

需要：

- Python 3.10 或更高版本。
- Git。
- 至少一个支持的编程 Agent，例如 Codex CLI。
- 首次安装时可访问 PyPI 和 Playwright 下载源。
- 如果使用网页 Provider，需要一个可正常登录的模型网页账号。

不需要单独安装数据库，也不需要让 AgentRelay 常驻后台。

### 2. 下载并安装

macOS 或 Linux：

```bash
git clone https://github.com/Geek-Dream/AgentRelay-Hub.git
cd AgentRelay-Hub
python3 install.py
```

Windows PowerShell：

```powershell
git clone https://github.com/Geek-Dream/AgentRelay-Hub.git
cd AgentRelay-Hub
py install.py
```

如果系统没有 `py`，把 `py` 换成 `python`。

安装器会做这些事：

1. 创建独立的 `agentrelay-env` Python 环境。
2. 安装 Python 依赖和 Playwright Chromium。
3. 安装 `agent-relay`、`agent-fusion`、`agent-image` 三个主 Skill。
4. 按选择安装内置 Vue.js AI、AgentRelay Browser Automation、Anthropic 官方
   Skills 和本机发现的 Skills。
5. 把 Hook、运行脚本和参考文档安装到对应 Agent 目录。
6. 合并已有 Hook 配置，不覆盖用户自己配置的其他 Hook。
7. 运行基础检查和合成 Hook 事件检查。

如果以前安装过旧版本，重新运行安装器即可。安装器会备份被更新的旧文件。

### 3. 选择 Agent 和 Skills

交互安装开始时会显示类似下面的清单：

```text
[x] 1. Codex CLI
[ ] 2. Claude Code
[x] 3. Pi
```

输入编号切换勾选，直接回车确认。安装器会自动识别本机已经安装的 Agent，并默认勾选它们。

也可以只接入指定宿主：

```bash
python3 install.py --targets codex,claude,pi,hermes
```

Codex 是共享运行时和默认安装路径。即使命令行里没有写 `codex`，安装器也会保留 Codex 基础组件。

选完 Agent 后，安装器会扫描这些 Agent 现有的 Skills 目录，把已经安装的
Skill（例如 `mysql`）加入清单。预设默认清单是 `browser-automatic`、`docx`、
`pdf`、`pptx`、`xlsx`、`vue-best-practices`、`web-artifacts-builder`；这些项目
默认勾选，用户可以在确认前手动取消。其他许可证明确且允许复制的本机 Skill 会显示，
但默认不勾选。AgentRelay 不读取模型的隐式调用记录，也不会统计你的使用行为。

清单里同时保留内置 Vue.js AI Skills。即使本机没有装过 Vue Skill，也会默认
选择 `vue-best-practices` 作为离线兜底。你可以输入编号取消或增加项目，然后
按回车确认；输入 `0` 或 `none` 表示这次不安装额外 Skill。

`browser-automatic` 是 AgentRelay 自己维护的 MIT Playwright Skill，不读取、也不复制
本机已有的同名目录。安装器会从仓库内的可信包为每个选中的 Agent 分别创建独立副本，
因此 Codex、Claude Code 和 Pi 可以同时安装，彼此不共享文件。

其他许可证明确且允许复制的本机 Skill 安装到另一个 Agent 时，会完整复制目录、子目录
和原有许可证文件。
`agent-relay`、`agent-fusion`、`agent-image` 属于 AgentRelay 自己管理的组件，
不会混进可选清单；符号链接和格式不安全的目录也会跳过。

`docx`、`pdf`、`pptx`、`xlsx`、`web-artifacts-builder` 不复制任何本机副本。
它们来自 Anthropic 官方
[`anthropics/skills`](https://github.com/anthropics/skills)，固定到提交
`2c5958ecac7cee2b583967a2c4981d0f4a75c579`，安装时由 AgentRelay 分别下载到每个
被选中的 Agent。归档只允许 HTTPS 官方地址，限制下载和解压体积，拒绝绝对路径、
`..`、符号链接、硬链接、设备文件和重复条目，并逐个校验文件数、内容 SHA-256、
`SKILL.md` 与 Apache-2.0 `LICENSE.txt`。校验通过前不会改动目标 Agent；下载或
校验失败会明确报告，不会把失败说成安装成功。

许可证明确禁止复制或迁移、或者无法确认许可证的本机 Skill 仍会显示来源，但会标成
“不可复制”并禁止勾选。AgentRelay 不会为了绕过许可证限制而复制它们，也不会把
“没有许可证文件”当作允许复制。

非交互安装可以显式指定 Skill：

```bash
python3 install.py --targets codex,claude,pi \
  --skills browser-automatic,docx,pdf,vue-best-practices
```

不写 `--skills` 时，默认安装清单中可安全安装的项目，包括从 Anthropic 官方仓库
独立下载的五个官方 Skills、AgentRelay 内置 Browser Skill、可复制的本机 Skills
（若存在）和 `vue-best-practices` 兜底。`--skills all` 只选择可安装项，不会绕过
非官方 Skill 的许可证限制。
只需要 AgentRelay 基础能力时使用：

```bash
python3 install.py --targets codex --no-skills
```

### 4. 配置一个外援

安装器通常会自动打开本地网页配置中心。如果没有打开，运行：

```bash
python3 install.py --menu
```

然后选择“打开本地网页配置”。

在网页里完成：

1. 添加网页、API 或本地模型 Provider。
2. 网页模型点击“打开网页登录”，由用户手动完成登录和人机验证。
3. 点击“测试”确认 Provider 可用。
4. 按需设置并发上限、排队超时和租约时间。
5. 保存并关闭。

没有 Provider 也能使用 Hook 门禁、确认卡、本地归档和只读检查，只是不能调用外部模型。

不要把正在驱动当前 Agent 的主模型重复填成协作 Provider。尤其不要填写同一个本地服务或
相同 endpoint，否则两个进程会抢同一份推理资源，出现排队和超时。

### 5. 重启 Agent

安装 Hook 或更新 Skill 后，重启正在使用的 Codex、Claude Code、Pi 或 Hermes。

Codex 用户可以在终端检查 Hook 是否加载：

```text
/hooks
```

如果看不到 AgentRelay Hook，先重启，再重新运行安装器。

## 第一次使用

安装完成后，不需要改变平时的提问方式。

### 普通问答

可以直接问：

```text
请解释这个函数，不要修改文件。
```

这类问题只读，不创建需求卡、不启动交付审计，也不会为了普通问答无意义调用 Fusion。

### 普通小改

可以直接说：

```text
把这个按钮文字从“保存”改成“保存修改”，只改这个文件。
```

清晰、局部、低风险的小改通常走 `light` 工作档，不显示用户确认卡。Hook 会自动记录
`light` 并允许首次写入，不需要主 Agent 或用户额外运行选档命令。

### 复杂修改

如果任务是跨文件、共享组件、高风险或目标本身比较模糊，主 Agent 应该先展示五栏确认卡，
而不是直接改：

```text
请把支付模块从旧接口迁移到新接口，保持页面行为不变，并说明影响范围、验证方式和回滚方式。
```

五栏分别是：

| 栏目 | 要回答的问题 |
|---|---|
| `现象` | 现在实际看到的问题是什么 |
| `期望` | 改完后具体要达到什么状态 |
| `定位` | 已经查到哪些真实文件、函数或页面元素 |
| `边界` | 哪些文件、模块和行为不能改 |
| `疑点` | 哪里还不确定，推荐采用什么方案 |

卡片还会补充影响范围、风险、验证命令和回滚方式。批准只对当前卡片展示的同一范围生效。
如果修改范围扩大，必须重新确认。

### 让 Fusion 帮忙

可以明确提出：

```text
请先检查 AgentFusion 状态。如果 delegable=true，就把完整测试日志分析交给 Fusion；
如果不可用，不要等待，继续由你本地处理。
```

Fusion 适合测试日志、批量阅读、跨文件搜索、独立复现和边界明确的局部实现。它默认只读，
写文件和执行命令需要明确切换到 `coding`。

Hook 只负责本地门禁、确认卡和修改摘要，不会主动提醒或调用 Fusion，也不会因为 Fusion
超时阻塞本轮回复。Fusion 由主 Agent 在明确适合的任务中主动调用；一次超时后，当前会话
会打开熔断，后续自动调用直接跳过，只有显式 `retry` 才重新尝试。

### 让 Commander 分工

可以明确提出：

```text
这个任务涉及前端、后端和测试。请用 Commander 拆分隔离子 Agent，
完成后先汇总审查，再逐文件合并，不要直接覆盖主工作区。
```

Commander 适合真正需要多人并行的工作。一个普通单文件任务启用 Commander 只会增加成本和
等待时间，所以是否启动应由任务复杂度和用户要求决定。

### 让别的模型看图

只有当前主模型确实不能识图，而且你明确要求借用其他模型时，才使用 AgentImage：

```text
$agent-image /path/to/screenshot.png
```

也可以直接说“让在线模型看一下这张图”。AgentImage 不会自动拦截所有图片。当前主模型本身
能读图时，应直接使用原生视觉能力，避免多一次转述丢失细节。

## 什么时候会出现确认卡

不是每个任务都会建卡。

| 场景 | 是否建卡 | 说明 |
|---|---|---|
| 普通问答、解释、搜索、只读检查 | 不建卡 | 不写文件，也不建立交付审计 |
| 清晰、局部、低风险的小改 | 默认不出用户卡 | Hook 自动记录 `light` 工作档和审计基线 |
| 普通实现需要明确范围和验收 | 可能出卡 | Hook 给出最低工作档，主 Agent 选择具体档位 |
| 跨文件、共享组件、高风险、参考图驱动 | 出卡 | 通常至少为 `strict` |
| 用户要求完整审查、全量回归、逐项核对 | 出卡 | 使用 `user_required`，不能自行降档 |
| 未批准时尝试写文件 | 写入确认卡 | Hook 硬阻断，只允许继续只读检查和安全测试 |
| 删除、破坏性 Git、部署、数据库迁移 | 危险操作确认卡 | 展示实际命令，只允许完全相同的输入批准一次 |

常见卡片类型：

- **需求确认卡**：先确认目标、范围、风险、验证和回滚。
- **五栏确认卡**：把截图、一句抱怨或模糊目标翻译成可执行、可验收的修改合同。
- **写入确认卡**：任务未批准却要写文件时出现。
- **危险操作确认卡**：高风险命令前出现。
- **权限设置菜单**：`/agent-approval` 或 `/设置`，只用于切模式，不批准当前任务。

`/agent-audit` 不是审批卡。它只显示当前交付审计还缺哪些证据，不代表审计已经通过。

简单卡片的字段大致如下：

```text
「确认卡」
--------------------------------
- 任务：修复登录页提交按钮无法点击
- 现象：点击后没有请求发出
- 期望：点击后发起登录请求并显示加载状态
- 定位：src/pages/Login.tsx，onSubmit 被条件提前返回
- 边界：只允许修改 Login.tsx，不改接口和其他页面
- 疑点：无
- 验证：运行登录页测试，并在 Chromium 中真实点击
- 回滚：失败时恢复修改前文件
--------------------------------
```

用户可以用自然语言批准，例如“可以”“按这个范围改”“批准修改这个范围”。主 Agent 会结合
当前卡片判断语义，Hook 不依赖固定口令。问句、否定、假设、补充条件和新范围都不会被当成
批准。

## 权限模式与工作档

这两个概念名字相似，作用不同。

### 权限模式

权限模式控制“什么时候需要问你”。可在 Agent 中输入 `/agent-approval` 或 `/设置`，也可以
直接运行：

```bash
scripts/approval_policy.py show
scripts/approval_policy.py set standard
```

| 模式 | 适合谁 | 行为 |
|---|---|---|
| `strict` | 生产项目、高风险仓库 | 所有新增、修改和删除先确认 |
| `standard` | 大多数用户，默认 | 清晰小改可直接做；跨文件、高风险和模糊任务先确认 |
| `relaxed` | 个人试验、低风险项目 | 小任务更少打断；危险命令、共享影响和越界仍会拦截 |

模式放宽不会放开危险 Git、部署、越界修改或未批准的新范围。

### 工作档

`light` 写任务由 Hook 自动记录，普通小改不需要额外命令。需要 `standard`、`strict` 或
`user_required` 时，由主 Agent 在首次写入前选择；Hook 会给出最低档，Agent 可以选更高档，
不能自行降档。用户不需要手动选择工作档或运行内部命令。

| 工作档 | 用于什么任务 | 用户是否看到卡 |
|---|---|---|
| `light` | 单文件、清晰、低风险、有直接验证方式 | 通常不显示 |
| `standard` | 普通实现，需要需求、diff、行为和审查证据 | 显示 |
| `strict` | 跨文件、共享、高风险、模糊或参考图任务 | 显示 |
| `user_required` | 用户明确要求完整审查和逐项验证 | 显示 |

纯问答和只读检查不选择工作档，也不创建审计。

更详细的规则见：

- [work-profiles.md](skills/agent-relay/references/work-profiles.md)
- [confirmation-card.md](skills/agent-relay/references/confirmation-card.md)
- [red-green-review.md](skills/agent-relay/references/red-green-review.md)

## AgentFusion 和 Commander

| 能力 | 主要用途 | 启动方式 | 默认安全边界 |
|---|---|---|---|
| AgentFusion | 一个持久副手，处理高输出量或可隔离的工作 | 主 Agent 检查 `delegable=true` 后委派 | 默认 `readonly`，`coding` 需要明确开启 |
| Commander | 多角色并行处理跨模块任务 | 用户明确要求或任务复杂度足够时启动 | 子 Agent 使用隔离 workspace，合并前需确认 |

Fusion 的典型任务：

- 跑完整测试并汇总大量日志。
- 批量阅读多个目录。
- 独立复现一个 bug。
- 按主 Agent 已确定的方案修改局部代码。

Commander 的典型任务：

- 前端、后端、数据库、测试分别处理。
- 多个角色需要并行工作。
- 主工作区必须先保持不动，最后统一审查和逐文件合并。

两者不会递归启动。Commander 运行时会抢占 Fusion；子 Agent 不能继续启动 Commander 或 Fusion。

检查 Fusion 状态：

```bash
PYTHON="${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python"
FUSION="${CODEX_HOME:-$HOME/.codex}/skills/agent-fusion/scripts/agent_fusion.py"
"$PYTHON" "$FUSION" status --json
```

Fusion 默认开启，但不会自己猜 Provider。必须明确选择，例如：

```bash
# 如果新开了一个终端，先重新执行上一段命令，设置 PYTHON 和 FUSION。
"$PYTHON" "$FUSION" provider api:kimi-api
"$PYTHON" "$FUSION" mode readonly
```

允许 Fusion 修改文件或执行命令前，必须确认任务边界和 Provider 类型：

```bash
"$PYTHON" "$FUSION" mode coding --confirm-coding
```

网页 Provider 只提供咨询，不能获得本机文件写入和 shell 能力。需要实际执行时只能选择 API
或本地 Provider。

完整说明见 [agent-fusion/SKILL.md](skills/agent-fusion/SKILL.md)。

## 支持的 Agent

| Agent | 当前支持 | 说明 |
|---|---|---|
| Codex CLI | 完整 | 主路径，Hook、Skill、确认卡、Fusion 和 Commander |
| Claude Code | 完整 | 安装器写入 `~/.claude/settings.json` 的 hooks |
| Pi | 完整 | 安装原生扩展，提供 Fusion 菜单、工具阻断和 Stop 修改摘要 |
| Kimi Code | 完整 | 写入 `$KIMI_CODE_HOME/config.toml`，失败和 Stop Hook 可用 |
| Hermes | 代码已接入 | `pre_llm_call`、`post_tool_call`、`subagent_stop`；真实宿主可见性仍需实测 |
| OpenCode | 仅 Skill | 没有 Prompt 注入 Hook，自动提醒不可用 |
| DeepSeek CLI | 不支持自动接入 | 没有 Hook、插件或 Skill 机制；DeepSeek 网页可作为 Provider |

多 Agent 共用一套 Provider 并发额度。网页和本地默认各 1 个并发，API 默认 10 个并发，可按
账号能力调整。满载时先排队，排队超时会明确返回忙，不会偷偷换另一个模型冒充原 Provider。

### Windows 支持进度

代码已经使用 Windows 的 `Scripts/python.exe` 路径、`subprocess.list2cmdline` 和 Python
参数列表，避免依赖 macOS 固定路径或 `/bin/sh`。

但当前发布验证仍以 macOS 为主。Windows 用户可以做真实安装测试，但在完成独立 Windows
端到端验收前，README 不把“代码兼容”写成“Windows 已验证完成”。

## 常见问题

### Hook 没有反应

1. 重启对应 Agent。
2. Codex 中运行 `/hooks`，确认 AgentRelay Hook 已被识别或信任。
3. 重新运行 `python3 install.py`。
4. 检查安装输出里对应宿主的验证结果。

### 看不到确认卡

- 普通问答和小改本来就可能没有用户卡。
- 复杂任务要求主 Agent 先生成确认卡。
- 某些宿主不保证显示同一种卡片界面。Hermes 仍需真实宿主验证。
- Kimi 可能通过 Stop 原因或普通文本提示，不承诺与 Codex 完全相同的卡片 UI。

### Fusion 不工作

先检查：

```bash
"$PYTHON" "$FUSION" status --json
```

关注：

- `delegable=false`：当前不能委派。
- `FUSION_PROVIDER_NOT_CONFIGURED`：还没有选择 Provider。
- `FUSION_DISABLED`：Fusion 已关闭。
- `COMMANDER_ACTIVE`：Commander 正在运行，Fusion 按设计暂停。
- `FUSION_PROVIDER_BUSY`：Provider 满载，排队超时。
- `FUSION_PROVIDER_TIMEOUT`：Provider 在限定时间内没有返回。当前会话暂停自动调用，
  显式重试时才恢复。

### Provider 一直排队

检查 Provider 的并发设置。不要为了“解决排队”把主模型和协作模型指向同一个本地服务。

### 网页登录失效

重新运行安装器并打开本地网页配置，在浏览器中手动重新登录。AgentRelay 不绕过 CAPTCHA，
也不自动填写账号密码。

### 安装后页面检查失败

修改 HTML、CSS、JavaScript 或页面模板后，可以使用真实 Chromium 检查：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
  scripts/verify_page.py --output runtime/page-verification
```

失败返回非零退出码。截图、控制台错误和 `result.json` 会保存在输出目录，便于定位。

## 安全边界

- 不绕过 CAPTCHA，不保存账号密码。
- Cookie、登录状态和 API Key 不写入项目、日志或 Git。
- 网页咨询发送前会替换识别到的密钥、令牌、邮箱和手机号。
- 危险命令必须单独确认，只允许完全相同的输入重试一次。
- Fusion 只能访问指定 workspace。
- Commander 子 Agent 使用隔离 workspace，默认不覆盖主工作区。
- 外部模型建议必须由主 Agent 验证，不能直接当作最终答案。
- 删除策略与权限模式绑定。普通项目文件默认先确认，永久删除在默认模式下不允许。
- “测试通过”必须有当前文件版本的证据；旧截图、旧测试结果或空测试数量不能冒充通过。

## 工作原理

### 一次普通任务

```text
用户提交任务
    |
    v
Hook 做本地预检
    |
    +-- 纯问答/只读 -> 正常回答
    |
    +-- 小改 -> light 工作档 -> 写入 -> 对应验证
    |
    +-- 复杂任务 -> 确认卡 -> 用户批准 -> 写入 -> 审计和验证
```

Hook 只做确定性本地检查，不会为了判断任务大小偷偷调用第二个模型。最终语义判断仍由主
Agent 完成。

### 五个 Hook 事件

| 事件 | 作用 |
|---|---|
| `SessionStart` | 初始化会话状态 |
| `UserPromptSubmit` | 判断是否需要任务门禁和确认卡 |
| `PreToolUse` | 拦截未批准写入和危险命令 |
| `PostToolUse` | 记录工具结果、失败、耗时和本轮真实写入 |
| `Stop` | 检查未完成证据，并输出最近两次修改摘要 |

### 修改摘要历史

只有本轮真正成功写过文件，并且 `Stop` 的确认、交付审计和验证门禁全部放行后，Hook 才会
在控制台打印一次：

```text

  📝 本次修改：
    修复了登录超时的问题。现在会话过期后会自动回到登录页，并补上了对应的回归测试。

  🕘 上次修改：
    补充了安装步骤和常见问题，第一次使用的人可以照着说明完成配置。
```

开头保留一个空行。`📝 本次修改` 和 `🕘 上次修改` 是两个清楚的标题，每个标题下面只有
一段大白话，两段之间空一行。正文超过约 68 列显示宽度时会自动折行，中文按双宽计算，
续行缩进四格。输出不显示文件清单、测试数量、内部脚本名或技术日志；这些信息仍会保存在
历史 JSON 中，方便排查，但不会挤在 Hook 提示里。没有上一条记录时显示“暂无记录”，之后
最多保留和显示两条，不显示“上上次修改”。纯问答、只读搜索、危险命令被拦截或交付审计
尚未通过时都不会记账，也不会反复刷屏。

真实写文件后，Agent 应在最终回复前主动登记摘要和文件用途分组：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" change-summary \
  --summary "修复了登录超时问题。现在会话过期会自动回到登录页，并补上了对应测试。" \
  --file-group "登录超时修复=src/login.py" \
  --file-group "回归测试=tests/test_login.py"
```

`--summary` 只写用户能直接看懂的结果：哪里变了、现在有什么不同、是否补了测试。不要复述
用户请求，也不要写文件路径、测试数量、`JSONL`、`trace`、HTTP 状态码或内部函数名，
除非这些内容本身就是用户要的结果。推荐用一小段话说明，不要堆技术要点。

`--file-group` 只用于历史追溯，可以重复，格式是“修改目的=文件1,文件2”。Hook 仍会校验
文件是否属于本轮真实写入，但不会把它打印到控制台。如果 Agent 没有登记真实摘要，输出会
明确显示“未记录具体修改说明”，不会用用户原话冒充结果。当前会话可自动识别；需要指定时
再加 `--session <session-id>`。

历史按“工作区 + Agent”隔离，保存在：

```text
~/.codex/agent_relay_tracker/history/change-summaries/
```

它是帮助回忆“刚才改了什么”的摘要历史，不是 Git，也不是一键回滚 checkpoint。需要真正
撤回代码时仍使用 Git、修改前备份或专门的回滚工具。

### Provider 调度

- 每个 Provider 的并发名额由所有 Agent 会话共享。
- 满载请求进入持久化 FIFO 队列。
- 请求持有带心跳和过期时间的租约，异常退出后可回收。
- 网页 Provider 使用编号会话，避免不同任务混用上下文。
- 需要本机执行时按 `API -> 本地` 路由，网页 Provider 只做 advisory。

### 交付审计

普通问答不会启动审计。实际修改任务会绑定当前工作区指纹，按工作档收集证据：

- 原始需求是否逐项完成。
- 真实 diff 是否只改了允许范围。
- 测试或行为探针是否真实执行。
- 页面是否有当前版本的浏览器和视觉证据。
- 主 Agent 是否完成自审，必要时是否有独立审查。
- 逻辑改动是否在修改前得到真实 RED，修改后得到真实 GREEN。

这些机制参考了 gstack 的 review/QA 思路，但没有复制它依赖宿主 Bun 和大量 Hook 的整套重流程。
AgentRelay 把重流程放在按需读取的 reference 中，由任务复杂度决定加载深度。

来源和许可见
[delivery-audit-gstack.md](skills/agent-relay/references/delivery-audit-gstack.md)。

### 主要目录

| 目录 | 内容 |
|---|---|
| `hooks/` | Hook 入口、任务门禁、计时和状态 |
| `scripts/` | Provider、Fusion、Commander、审计、页面验证和配置中心 |
| `skills/agent-relay/` | 主协作规则和按需流程 reference |
| `skills/agent-fusion/` | Fusion 使用规则 |
| `skills/agent-image/` | 图片识别中继规则 |
| `skills/catalog/vuejs-ai/` | 固定 commit、带 MIT 许可和校验指纹的 Vue.js AI Skill 快照 |
| `skills/catalog/agentrelay/` | AgentRelay 自有的 MIT Browser Automation Skill 包 |
| `scripts/skill_catalog.py` | 本机 Skill 发现、官方 Skill 下载与内容校验 |
| `tests/` | 正式自动化测试，不是临时演示垃圾 |
| `runtime/` | 本地运行数据，默认被 Git 忽略 |

## 开发与测试

开发测试需要额外的 `pytest` 依赖。先创建独立环境并安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

运行完整测试：

```bash
.venv/bin/python -m pytest -q
```

Windows 下把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。

检查 Python 语法：

```bash
.venv/bin/python -m compileall -q hooks scripts tests install.py
```

检查 diff 格式：

```bash
git diff --check
```

真实浏览器检查：

```bash
python scripts/verify_page.py --output runtime/page-verification
```

CI 会执行：

1. Python 编译检查。
2. Shell 语法和示例 JSON 检查。
3. 完整单元测试。
4. 真实 Chromium 页面验证。
5. 私密运行文件、个人路径和内嵌凭据扫描。

`runtime/`、缓存、日志、Cookie、登录状态和本地备份不应提交到 Git。正式 `tests/` 应保留，
它们是发布质量门，不是测试残留。

## 贡献

提交改动前至少完成：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q hooks scripts tests install.py
git diff --check
```

涉及页面时再运行真实 Chromium 检查。涉及 Hook、确认卡或多 Agent 协议时，应同时验证至少
一个真实宿主，不能只依赖 mock。

## License

MIT License，详见 [LICENSE](LICENSE)。
