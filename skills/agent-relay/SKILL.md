---
name: agent-relay
description: "AgentRelay 外援协作、工作流与本地归档。当 Agent 长期无法解决技术问题、重复尝试、有效处理时间过长，或用户明确要求调用 AgentRelay/在线模型时，使用已配置的在线模型 Provider 获取外部技术建议；回复默认输出到终端 stdout，主 Agent 必须自行判断、修改并验证。用户指派较大改动时由主 Agent 判断工作量并生成需求卡/确认卡，需要时协调 1-5 个隔离子 Agent；任务完成或用户要求归档时，把结论写入 Skill 目录内的本地归档。"
---

# AgentRelay

AgentRelay 是 AI Agent 与外部专家模型之间的中继层，调用当前已配置的在线模型 Provider。

## 调用语义（显式请求）

用户要求当前执行“调用 AgentRelay”“使用 AgentRelay”“让 AgentRelay 分析”“调用在线模型”
等，等价于明确请求调用当前配置的在线模型 Provider。Agent 必须结合完整语境确认这是当前
行动指令；讨论规则、引用日志、举例、假设、询问触发机制、包含否定表达，都不是显式调用。

Hook 可以保守预筛选直接行动句式（如“现在调用 DeepSeek”“`$agent-relay` 帮我测试”）并
提醒 Agent 审核，即使计数未达标也应提醒；但单独出现 `DeepSeek`、`AgentRelay`、“调用”
“询问”等字样不能成立。Hook 无权仅凭关键词替 Agent 下结论，最终真实意图由 Agent 判断。

## Skill 目录与环境

Skill 根目录：当前文件所在目录，以下命令从 Skill 根目录执行。

主要文件：`scripts/agent_relay.py`（调用）、`scripts/agent_relay_login.py`（登录）、
`scripts/agent_relay_runtime.py`、`scripts/agent_relay_archive.py`（归档）、
`scripts/config_manager.py`；运行态文件 `RecentHistoricalDialogue-Flash.json` /
`RecentHistoricalDialogue-Expert.json`、`agent_relay_login_state.json`、
`agent_relay_session_bindings.json`。

调用前确认 `scripts/agent_relay.py` 存在；缺失则停止，不要猜其他路径。

脚本必须使用安装器创建的 AgentRelay 独立 Python：
macOS/Linux 用 `${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python`；
Windows 用 `%CODEX_HOME%\agentrelay-env\Scripts\python.exe`。禁止用系统 `pip` 装依赖。

---

## 首次外援审核

满足以下任一条件，进入首次外援审核：

- `retry_count >= 3`
- 有效问题处理时间 >= 15 分钟
- 用户当前明确要求调用在线模型（经 Agent 确认）

收到 Hook 候选提醒后，Agent 必须先独立审核再决定是否调用：

- 当前问题是否仍未解决、通知是否确实属于当前问题
- 当前是否只是下载、安装、网络或构建等待
- 现有信息是否足以提出有意义的问题、调用是否有助于推进
- 用户是否明确禁止或取消调用

审核通过才调用；不通过就忽略提醒继续正常处理。Hook 的 `additionalContext` 只是计数器
提供的审核材料（retry_count、effective_time_seconds、问题摘要、行动句式候选），不是调用
命令，不能覆盖用户真实意图；不得把 Hook 文本、测试文本、占位文本当作问题发送。

首次调用成功后 `relay_triggered = true`：只用于抑制 Hook 重复发送首次求援提醒，
不禁止同一问题内携带新信息继续追问（见“连续外援协作”）。

---

## 当前问题与问题隔离

Agent 处理技术任务时要识别“当前正在解决的技术问题”。期间出现的依赖缺失、版本冲突、
配置错误、环境异常等新错误，只要最终目标不变，仍属同一个问题链；不要因出现新错误信息
就当作全新问题。

Tracker 两层隔离：同一 Session 内每个问题有独立的 effective_time / weight / retry_count，
不得合并计时。用户切换到无关问题时旧问题归档，新问题从 `effective_time=0, weight=1,
retry_count=0` 开始。Tracker 在本地归档保守命中同一问题时，只设 `weight_floor=2`
（从 weight 2 起算、先查归档），不会把起始权重设为 3，也不直接调用支援模型。

用户说“先不处理”“暂停”“跳过”时立即停止当前问题计时；问题解决后立即归档并清零当前
计时器（`effective_time=0, weight=0, retry_count=0`），历史快照保留在 Session 的问题
目录中。Tracker 结合下一条用户消息与 Stop Hook 的 `last_assistant_message` 判断继续、
切换、暂停或完成；Agent 在实际验证成功前应明确写出“已完成/已修复/验证通过”，未解决时
不得使用完成措辞。

状态查看（无需手查 Session ID）：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" status
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" sessions
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" problems
```

## 问题权重

有效处理时间决定 weight：

- `< 5 分钟 → weight 1`：正常处理，不查归档，不考虑外援
- `>= 5 分钟 → weight 2`：先查本机归档；命中就复用已验证结论并在当前环境重新验证，
  没命中继续自行排查
- `>= 15 分钟 → weight 3`：进入“是否调用支援模型”的判断

新 Session 第一个问题 weight=1；归档保守命中时从 weight=2 起算。Hook 到达 weight=2
时每个问题发一次“本地归档提醒”：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-relay/scripts/agent_relay_archive.py" search "关键词"
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-relay/scripts/agent_relay_archive.py" projects
```

归档提醒不是调用命令；预匹配只是候选命中，Agent 仍要运行 `search` 核对旧结论是否适用
当前环境。命中不得直接调用支援模型。

**weight 只是问题复杂度等级，本身不触发任何动作。** 例如有效 10 分钟（weight=2）、
retry=0、用户未要求 → 不触发在线模型。首次外援审核的条件始终只有 retry>=3、有效时间
>=15 分钟、或用户明确要求三条。

## 有效处理时间

计入：分析错误、查看日志、阅读相关代码、修改代码/配置、排查依赖、Debug、测试失败后
继续分析、尝试新方案。

不计入：下载依赖/插件/模型、`pip install`、`npm install`、`docker pull`、单纯 curl 下载
或网络等待、大型项目正常构建。即使下载持续 20 分钟，也不会因此增加有效时间或 weight。

核心原则：Agent 主动解决问题才计时；只是等待下载、安装或网络操作不计时。

## retry 的定义【重要】

第一次尝试某种解决方向不算 retry；已尝试某方向失败、再次尝试相同或类似方向才
`retry_count +1`。

示例：

- 下载插件失败后再下载一次：retry 0 → 1。
- 镜像 A 失败换镜像 B：第一次尝试新方案，通常不算；镜像 B 失败再换镜像 C：进入
  “重复通过更换镜像解决同一问题”，retry +1。
- 第一次改 Redis 版本失败不算；继续改 Redis/Spring 相关版本解决同类兼容问题：retry +1。

通常不增加 retry：第一次尝试一个方案；发现缺依赖→安装；发现新冲突→第一次处理；
切换到完全不同的技术方向。核心判断：是否已经尝试过这个方向、现在又再次尝试？

## retry 与有效时间相互独立

两种机制独立生效：尝试了很多完全不同方法（每种一次）可能 retry 一直很低，但有效时间
累计 >= 15 分钟仍达到首次外援审核门槛。weight=3 只在有效时间 >= 15 分钟时出现，而
这同一条 15 分钟门槛本身也是独立的外援审核条件——但两者都只是“进入审核”，最终是否
调用由 Agent 决定。这样防止 Agent 无限寻找新方案、无限 Debug、长期卡在一个问题。

---

## 在线模型调用方式

模式由用户在配置中心决定，脚本按配置自动选择，不凭品牌猜：

- 混合模式：极速和专家合并成一套会话，只有一个识图开关。
- 极速 + 专家：两套独立会话，识图能力分别判断。

不传 `-m` 时默认走极速；同一问题第二次尝试且本次不带图片时自动升级为专家；
`--attempt 2` 可把当前重试次数告诉脚本让它自己完成升级判断。用户说“问专家模式”传
`-m 2`，说“快速问一下”传 `-m 1`。脚本若做了回退（如指定专家但该网站专家不支持图片），
会打印 `[模式]` 说明，必须如实转述。

识图是每个模式独立的能力：kimi 可能只有极速能发图片、专家不能；只配置混合模式的网站
不勾识图就不能发图片。绑定会话不存在或失效时，脚本扫描左侧对话栏；仍未找到则用本次
问题创建新会话并按配置重命名后绑定。不要要求用户预先创建或重命名会话。

### 模式选择

- 有图片 → 必须选支持图片的模式；指定模式不支持时按脚本 `[模式]` 回退结果执行。
- 普通问题 / 第一次尝试 → `-m 1`。
- 同一问题第二次尝试且不带图片 → `-m 2`。
- 用户说“问专家模式”“让专家看看” → `-m 2`；说“快速问一下”“随便问问” → `-m 1`。
- 该网站只配置了混合模式 → 按混合模式调用，不要传 `-m 2`。

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "问题内容" -m 1
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "问题内容" -m 2
```

极速模式支持文本、图片、文件，适用普通问题、截图、UI、报错图片、日志、代码与配置文件；
专家模式只支持文本，适用复杂代码逻辑、算法、架构、并发、数据库、复杂异常分析。

### 图片转发规则

用户提供图片、截图或文件并要求 AgentRelay 查看时，原文件是请求的必要部分，必须用
真实可读取的本地路径：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py \
  "用户的原问题" -m 1 --image "/path/to/image.png"
```

多张图片对每个路径重复使用 `--image`；调用前确认文件存在。禁止用 Agent 自己的 OCR、
摘要或视觉描述代替原图，除非用户明确要求只发送文字描述。输入只显示 `[Image #N]` 且
没有可传给 CLI 的本地文件路径时，不得进行纯文本调用，应说明无法取得原图、请用户提供
路径。图片调用成功后 `AGENT_RELAY_RESULT.images_count` 必须大于 0，否则表示本次没有
实际转发图片，不得声称在线模型已看到原图。

---

## 在线模型回复在哪里【必须记住】

回复默认直接输出到终端 stdout，Agent 读取命令执行结果中 `💬 AI回复:` 之后的内容。

强制规则：

- `agent_relay.py` 是同步命令，但终端工具可能因等待窗口到期先返回后台 session ID。
  若返回 `Process running with session ID ...`，必须用终端工具的 `write_stdin`/继续读取
  机制轮询**这个原 session ID** 直到进程结束。禁止运行 `sleep 15` 等新命令代替轮询——
  新命令创建无关新 session，永远读不到原结果。
- 看到 `AGENT_RELAY_RESULT` 且 `status` 为 `success` 时，禁止再因“没看到结果”而 `sleep`、
  `ps` 或重复调用 Provider。后续因实施失败带新信息追问属于连续外援协作，不算重复读取。
- stdout 被折叠或截断时，按 `AGENT_RELAY_RESULT.history_file` 读取对应历史 JSON 一次，
  取最后一条的 `answer`。不得因输出折叠而重复调用。

历史文件（各最多保存最近 6 条）：极速 `RecentHistoricalDialogue-Flash.json`（支持图片
历史）、专家 `RecentHistoricalDialogue-Expert.json`，均在 Skill 根目录。优先级：
刚调用完成 → stdout；stdout 被截断 / Context 被压缩 / 需要查看之前建议 / 需要恢复
对话信息 → 对应模式 JSON。刚调用完成不得无原因重复读取 JSON。

## 构造在线模型问题

尽量包含：目标（要实现什么）、环境（语言/框架/版本）、问题（当前发生什么）、错误
（关键错误信息）、已尝试（尝试过的方案）、代码（必要的相关代码）、验证结果（最后一次
执行结果）。优先发送错误信息 + 关键代码 + 已尝试方案 + 最新验证结果；不要发送整个项目
或大量无关代码。

## 上下文复用

调用时必须优先使用 Agent 当前已获得的信息，禁止为调用在线模型而重新读取已读过的
大文件，避免“读大文件 → Context Compact → 再读 → 再 Compact”的循环。确需重新读取时
只读错误相关函数、错误行、关键配置、相关代码片段。

## 调用后：实施与验证

在线模型只是外部技术顾问，回复不是交付结果。流程：在线回复 → Agent 分析建议 →
检查项目兼容性（版本是否兼容、API 是否存在、依赖是否存在、是否重复之前失败方案）→
选择最小修改 → 修改 → 验证。

禁止直接复制全部代码、覆盖整个文件、因小问题大规模重构；优先最小可行修改。只要在
安全且用户授权范围内，Agent 应自行编辑文件、执行脚本、调整配置并验证，不能把在线
模型给出的分析或脚本原样转交用户后就结束任务。只有缺少用户专属信息、需要新权限、
涉及危险操作或本地确实无法取得必要条件时，才向用户提问或请求操作。

修改代码或配置后必须执行适合项目的验证（项目已有的构建/测试/启动/CI 命令），未经
实际验证不得宣称问题已解决：Python 用 `python3 -m py_compile` / `pytest`；Maven 用
`mvn compile` / `mvn test`；Node 用 `npm run build` / `npm test`；Go 用
`go build ./...` / `go test ./...`。

## 连续外援协作（同一问题内）

首次调用成功后进入外援协作：Agent 必须先实施并验证在线建议。出现以下任一情况可直接
继续调用在线模型，不重新等待 retry、有效时间或用户显式请求：实施建议后验证失败；出现
属于同一目标的新错误；在线模型要求提供更多日志、配置、代码或环境信息；建议存在关键
歧义无法安全实施。

在线模型要更多信息时，Agent 先用现有工具自行收集再发回同一在线会话；只有信息属于
用户专属秘密、需要授权或本地无法取得时才询问用户。每次追问必须增加至少一种真实新
信息（上次建议、实际修改内容、新错误信息、新验证结果），禁止发送完全相同的问题。
没有新信息时禁止机械重复追问，应继续本地排查或向用户说明阻塞点。不设置固定调用
上限，也不无进展地无限空问；以“同一问题仍未解决且本轮有新增事实可推动分析”为准。

以下情况结束当前问题的连续外援资格：问题已实际验证解决；用户暂停、跳过或放弃；
切换到无关问题；登录失效或 Provider 不可用；必须等待用户提供信息或授权。新问题必须
重新独立计算首次求援门槛，不能继承旧问题的外援资格。`relay_triggered` 不是“一生只能
调用一次”的锁，只是“当前问题已进入外援协作”的标记。

## 登录失败

出现未登录、Cookie 失效、Session expired、Unauthorized 等，停止自动查询，提示用户：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay_login.py
```

禁止无限登录、无限重复尝试、绕过登录或验证码。

---

## Commander 子任务合并

Commander 是主审查官协调的 1 到 5 个隔离子 Agent。角色按任务动态规划，可包含前端样式、
前端脚本、后端代码、数据库和 Git 审计；没有可用 Provider 时仍可由 Codex 子 Agent 离线
执行，不能把“已规划”伪报成“已完成”。子 Agent 继承父任务的约束，但不能递归启动
Commander。

每个子 Agent 必须使用独立 workspace 和父任务预分配的 `task_id`。它们的重试次数、有效
时间、权重和外援提醒完全隔离；达到三次重试或 15 分钟时，只通知 Commander 并继续工作。
网页会话和本地模型服务同一时间只能由一个角色占用，API Provider 不得被 Commander 子
Agent 直接调用。

Provider 按角色保持记忆：角色之前验证成功的网页 Provider，后续同角色任务继续优先使用；
Cookie 或代理失效时先熔断并切换可用备用 Provider，同时最多向用户提示三次登录脚本或
代理信息。恢复原 Provider 后，必须把当前真实任务原样交给原 Provider 和备用 Provider
各回答一次，记录两份回答、分数、最终选择，以及备用期间的上下文摘要。不能为了比较制造
“测试模型”之类的无关问题；备用 Provider 若忙或不可用，则保留熔断期间同一任务的回答
并记录失败原因。

本地模型是单并发资源，最多保留一个等待者；如果候选线上 Provider 空闲，应直接使用线上
Provider，不能继续傻等本地队列。只有所有候选都忙时才等待，超时后再失败或切换。空闲子
Agent 可以协助仍在处理、超时或升级中的角色，但协助者保留自己的原角色和结果，不能取代
目标主力。

子任务结束后，主审查官必须读取每个报告、状态、验证结果和基线差异，再生成合并计划与
用户进度摘要。合并计划至少包含完成/未完成角色、修改文件、验证结果、冲突原因和继续
工作状态。必须先展示独立的合并确认卡；只有用户批准后才逐文件写回主 workspace。主
workspace 被用户修改、角色之间内容冲突、路径越界或验证失败时，合并暂停且不得覆盖
用户文件。合并结果需记录成功角色、未完成角色、实际修改文件和验证状态，并交给主审查官
总结。运行时事件可通过以下命令查看和驱动：

```bash
python3 scripts/agentrelay_console.py commander-merge TASK_ID \
  --workspace /path/to/project --runtime-root .agentrelay/commander
```

确认卡、需求卡、验证卡和回滚卡始终由主 Agent/Workflow 控制；外部模型只提供建议或受限
修改，不能绕过确认、隔离、审查和验证流程。

## 工作量由审查官判断

大活小活没有固定表格，由当前任务的审查官（主 Agent，进入 Commander 后由主审查官）拿到
用户指派后自己判断。依据：要动的文件数和模块数；是否跨前端、后端、数据库、消息队列、
依赖或部署；是否需新建或迁移数据、改配置、重启服务；是否容易回滚、失败代价多大；用户
是不是一口气提了多个需求点。

判定决定流程档位：

- 小活（单点、低风险）→ 直接做，做完说明改了什么。
- 中活（多个普通修改点）→ 一张汇总确认卡，多个修改点同卡确认。
- 大活（跨模块、高风险、要装依赖/迁移数据/重启服务）→ 先需求卡，确认需求后再出执行
  确认卡；确实需要多角色时，第二张卡改为“启用审查官模式”，批准后才启动子 Agent。

判断权在审查官，不在用户有没有说“重构”这类字眼；也不要因任务听起来吓人就把小改动
升级成大流程。拿不准时按低一档处理，并在回复里说明理由。审查官判定需要 Commander 时，
默认仍要先得到用户确认，除非用户已经明确要求启用。

---

## 本地归档

归档永远和 Skill 在同一个文件夹：`<skill>/归档/<项目名>/已完成任务.jsonl` 与
`<skill>/归档/<项目名>/归档.md`。只写在 Skill 目录内，不写进用户项目仓库，不保存
Cookie、密钥、登录状态或原始对话。

写入时机：模型判断任务已完成并通过验证 → 自己回写一条；用户说“归档”“记录一下”
“这个存起来” → 按用户要求回写并标记“用户要求归档”。

```bash
python3 scripts/agent_relay_archive.py add \
  --title "MQ 换 Kafka" --project de \
  --summary "生产者消费者切到 Kafka，测试通过" \
  --file src/MqSender.java --verification "pytest 通过"
python3 scripts/agent_relay_archive.py list --project de
python3 scripts/agent_relay_archive.py search Kafka --project de
python3 scripts/agent_relay_archive.py projects
python3 scripts/agent_relay_archive.py path
```

规则：新问题开始时 Tracker 只读检查当前项目归档，保守命中则从 weight 2 起算、先查
归档，不直接调用外援；只有真正验证通过的任务才写“已完成”；未完成、已回滚、被用户
取消的任务用对应状态记录，不要写成已完成；回滚过的功能也要留痕，状态写“已回滚”并写清
影响了哪些文件；归档前先 `search` 一次避免重复记录；不确定项目名时省略 `--project`，
脚本按 workspace 的 git 仓库名或目录名推断；用户明确表示不要归档时不要写；归档不影响
代码，也不替代需求卡、确认卡和验证。
