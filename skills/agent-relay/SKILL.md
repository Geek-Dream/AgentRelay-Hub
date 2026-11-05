---
name: agent-relay
description: "AgentRelay 外援协作、工作流与本地归档。当 Agent 长期无法解决技术问题、重复尝试、有效处理时间过长，或用户明确要求调用 AgentRelay/在线模型时，使用已配置的在线模型 Provider 获取外部技术建议；回复默认输出到终端 stdout，主 Agent 必须自行判断、修改并验证。用户指派较大改动时由主 Agent 判断工作量并生成需求卡/确认卡，需要时协调 1-5 个隔离子 Agent；任务完成或用户要求归档时，把结论写入 Skill 目录内的本地归档。"
---

# AgentRelay

AgentRelay 是 AI Agent 与外部专家模型之间的中继层，调用当前已配置的在线模型 Provider。

## 分档交付审计（按修改范围触发）

参考 gstack 的 review/QA 闭环，但不依赖安装 gstack。参考来源和 MIT 许可见
`references/delivery-audit-gstack.md`。`/agent-approval` 同时绑定权限与审计深度：

- `strict`：所有写入仍需确认；实际修改任务收尾时执行完整审计：逐项需求、真实 diff、行为、
  主 Agent 审查和独立审查。页面还要完成现有目标/边界/视觉认证。缺一项都不能声称完成。
- `standard`（默认）：普通代码小改走轻量检查；多文件、复杂/共享/高风险和不确定结果自动升级。
  单文件、单目标的颜色/圆角/按钮方圆/字号/间距等样式修改，只做目标 computed style、
  目标外关键布局和基本交互验证，不强制完整需求审计或独立审查。
- `relaxed`：小任务不创建正式交付审计，不强制 Fusion；仅大型、多文件、多目标、共享或高风险
  修改进入完整审计。危险命令、越界、目标未达成和虚假完成声明仍不豁免。

### 触发边界与禁止自审循环

- 普通问答、概念解释、搜索和只读检查不创建审计。Hook 的待办/Stop 提醒只是历史状态通知，
  **不是用户授权继续执行**。不得因提醒自动修改文件、运行测试、调用 Fusion 或开始新审计。
- 用户主动输入 `/agent-audit` 仅查看清单，不等于要求立即执行。是否继续由用户后续明确指示决定。
- 已结束任务或新问题的旧审计不能劫持当前问答。非页面任务不触发截图门禁。
- 同一待办不反复出卡，不通过审计结果本身制造下一轮审计；后续实际代码修改才需刷新证据。

### 实际执行方式

1. Hook 只在本档位需要的修改任务中建立审计待办；relaxed 小任务不建立正式审计。
2. 完成实现后输入 `/agent-audit`，阅读当前真实待办。该命令只显示状态，**不等于审计通过**。
3. 主 Agent 必须读取用户需求和真实 diff，逐项对照 DONE/CHANGED/UNVERIFIABLE；漏做或越界先处理。
4. 真实运行与改动匹配的项目测试或行为探针，不能只复制一个“测试通过”结论：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-relay/scripts/delivery_audit.py" \
  run-behavior --home "${CODEX_HOME:-$HOME/.codex}" --session "<当前真实 session_id>" \
  --cmd "<项目已有测试或本地行为检查命令>" --kind test
```

`--kind test` 要真实执行测试且数量大于 0；CLI/API/任务探针用 `--kind probe`，不强制截图。
失败、超时、未运行和覆盖缺口必须保留，不得只保存成功结论。
5. 逐项登记真实证据（`requirements`、`diff`、`self_review`、`visual`、`independent_review`）：

```bash
AUDIT="${CODEX_HOME:-$HOME/.codex}/skills/agent-relay/scripts/delivery_audit.py"
python3 "$AUDIT" record --home "${CODEX_HOME:-$HOME/.codex}" --session "<当前真实 session_id>" \
  --check requirements --passed --evidence '[{"item":"原始需求","status":"done","evidence":"文件:行/真实行为"}]'
python3 "$AUDIT" record --home "${CODEX_HOME:-$HOME/.codex}" --session "<当前真实 session_id>" \
  --check diff --passed --evidence '已读取完整 diff；修改仅限允许文件，未发现未声明行为'
python3 "$AUDIT" record --home "${CODEX_HOME:-$HOME/.codex}" --session "<当前真实 session_id>" \
  --check self_review --passed --evidence '已检查异常路径、相邻调用、目标外影响和覆盖缺口；未发现问题'
```

6. 页面按上文完成真实截图和视觉认证，Hook 会复用该证据，不需要重复勾选。无视觉仍只能用户复查。
7. 完整审计需要独立审查时，运行 `delivery_audit.py independent-review --home ... --session ...`。
   它会先走现有 Fusion 状态检查，`delegable=true` 才实际 `delegate` 一次并保存真实结果；
   不可用或失败时脚本只登记 `fallback=main_agent_local_adversarial_review_required` 且
   `passed=false`；主 Agent 必须先实际完成本地对抗性审查，之后才能单独登记
   `fallback=main_agent_completed_local_adversarial_review` 且 `passed=true`。
   不能把“需要审查”伪写成“已经审查”，也不得写“已独立复核”；未真实调用不得登记
   `provider=fusion` 成功。
8. 后续任何文件修改都使审计过期，必须重新检查受影响证据。通过前只能报告进度和未验证项，
   不能写“全部完成/已验证/审计通过”。最终回复列出完成内容、验证结果、未验证项。

当用户只是问“`/agent-audit` 是干嘛的”“这是干嘛的”“为什么要审计”等功能或机制问题时，
只解释用途和触发条件；不得运行 `delivery_audit.py`、读取审计状态卡、创建审计待办或把
普通问答切换成交付审计。只有用户明确要求执行审计，或当前修改任务出现真实完成声明时，
才进入上述审计流程。

## 调用语义（显式请求）

用户要求当前执行“调用 AgentRelay”“使用 AgentRelay”“让 AgentRelay 分析”“调用在线模型”
等，等价于明确请求调用当前配置的在线模型 Provider。Agent 必须结合完整语境确认这是当前
行动指令；讨论规则、引用日志、举例、假设、询问触发机制、包含否定表达，都不是显式调用。

Hook 可以保守预筛选直接行动句式（如“现在调用 DeepSeek”“`$agent-relay` 帮我测试”）并
提醒 Agent 审核，即使计数未达标也应提醒；但单独出现 `DeepSeek`、`AgentRelay`、“调用”
“询问”等字样不能成立。Hook 无权仅凭关键词替 Agent 下结论，最终真实意图由 Agent 判断。

## Hook 门禁

- 目标外检查必须真实计算：`--invariant <目标外选择器>` 在修改前记录位置/尺寸和计算样式；
  `--after` 自动继承这些选择器，即使漏写参数也必须逐项比较。没有实际 `unchanged_results`、
  检查失败或验收后文件又变化，不得写“无副作用”。公共组件复用路径必须真实存在，不能编造。
- 普通 UI 修改（包括按钮文字、输入框尺寸等）会生成辅助契约草案，不造成二次审批；
  草案不代替真实目标/边界验收。显式严格契约仍需具体参考、允许文件和复用依据。
- **目标必须真的改对**：整页没乱不等于目标成功。修改前用
  `verify_page.py --session <当前真实 session_id> --url <本地页面> --target-selector <选择器> --expected <属性JSON> --invariant <目标外选择器>`
  采集目标 computed style、位置尺寸和局部截图；修改后用 `--after`（相同目标参数）逐项比较，
  生成 `target_verified`。目标未达预期不能交付；目标达成但目标外有副作用同样不能交付。
- **页面验收不是“截图存在”**：有视觉时必须实际读取整页/容器截图，检查目标与
  `unchanged_checks`，然后运行 `visual_review.py record --passed`，每张截图重复传
  `--screenshot <路径>`，并逐项传 `--observation 'target=实际观察'` 和
  `--observation '<目标外检查项>=实际观察'`，再加 `--self-reviewed` 和
  `--attestation '已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出'`。
  多个目标必须用 `--target-item <目标>` 逐项声明，用 `--item-screenshot '<目标>=<独立截图路径>'`
  绑定不同的真实截图，观察项使用目标名，不能拿一张图或功能点击代替全部视觉检查。
  多个目标复用同一图片会按内容哈希拒绝；点击、DOM、控制台和尺寸测量属于功能/结构检查，
  不能冒充某个目标的视觉观察。完成声明声称“1–4 象限、四张表、全部页面”等范围时，
  认证目标数必须覆盖该范围，否则 Hook 阻断。只有图片文件头、泛泛的“没问题”或未逐项观察
  都不得通过。
- **观察必须具体**：每个目标项和目标外项目都要写出实际可见内容或布局结果，例如
  “表格显示 2 行，列宽对齐，底部连续空白约 400px”，不能只写“符合/正常/无变化”。
  自评审签名至少覆盖两类视觉检查（内容、布局、间距、对齐、空白、溢出、遮挡、尺寸等）。
  截图会做真实像素布局分析；出现连续大块近白区域时不能登记 `passed`，必须修复后重新截图，
  或如实转为 `user_review_required` 请用户复查。
- **原生视觉默认**：当前主 Agent 能读取用户图片或 Playwright 截图时，使用 `native` 视觉审查；
  不调用 agent-image 不代表没有视觉。只有主 Agent 明确无法读图时才考虑借视觉 Provider。
- 无视觉时调用 `visual_review.py agent-image` 借视觉 Provider；Provider 必须逐项返回
  `target: 通过: 实际观察` 和各目标外项目的实际观察。失败或无法判断只能用户复查。
- 无视觉 Provider 时不强制截图：用 `verify_page.py --no-screenshots` 完成真实 Chromium 的
  DOM/布局、控制台、文件哈希、按钮点击/输入和目标外检查，随后 `visual_review.py nonvisual`
  登记结构化结果。最终只能 `user_review_required`，明确请用户看页面，不得声称视觉通过。
- **截图必须清理**：验收完成后先运行
  `visual_review.py cleanup --session <当前 session_id> --home "$HOME/.codex"`，删除本轮登记的
  全部截图，仅保留文字证据和清理状态。若截图未登记在状态中，对每个绝对路径重复传
  `--screenshot <绝对路径>` 再运行 cleanup，不能使用通配符。若宿主拒绝 `rm -f`，不要重复
  相同命令或再次弹同类确认；立即改为一次一个文件执行 `unlink -- <绝对路径>`，全部执行后
  用 `test ! -e <绝对路径>` 回查。仅允许系统临时目录或 AgentRelay 验收证据目录内、明确
  逐个列出的截图；禁止目录、项目文件、用户输入图片、符号链接、`-r/-R`、通配符或与其他
  路径混合删除。
- 多目标/复杂修改由 Hook 出确认卡，不能由 Agent 自行省略；目标外边界是执行与验收底线，
  不是确认卡装饰。发现父布局、公共组件或其他模块受影响必须先重新确认。

## 语义审批桥

用户不需要记固定批准口令。`好的`、`OK`、`没问题`、`执行吧`、`行吧`、`1`、`[1]`
或其他自然表达，都要由主 Agent 结合确认卡和完整对话理解真实含义；Hook 只能提醒，
不能因为命中关键词就替用户授权。

收到 `[AgentRelay 语义审批桥]` 时：

- 先判断用户是在明确批准、明确拒绝、提问、犹豫、补充条件，还是提出了新范围。
- 只有明确同意**当前确认卡展示的同一范围**，才运行上下文给出的
  `resolve-intent --decision approve --source-sha256 ...`，并把 `reason` 换成真实的
  语义判断依据。
- 只有明确拒绝当前范围，才运行 `--decision reject`。拒绝后写入继续被 Hook 阻断。
- 问句、否定、假设、犹豫、补充条件或新范围不得写回批准；需要时先解释、询问或更新确认卡。
- `source-sha256` 必须使用当前提醒中的值，对应最后一条用户消息。旧哈希、范围已变化的
  快照和重复写回都会失败，不能重试旧批准。
- 危险命令批准仍只对完全相同的工具输入生效一次；语义批准不能扩展到其他命令、文件或任务。


## 严格变更约束（默认遵守，授权后发挥）

- **复用优先，禁止重复造轮子**：实现前必须检索项目已有组件、骨架/加载态、按钮、表格、
  样式变量和相似页面，先复用或扩展现有实现。不得因为“自己也能画出来”就另造一套骨架。
- `change_contract` 对显式 UI 作用域要求 `reuse_checked=true` 和实际 `reuse_evidence`；
  未检索或没有复用依据时写入会被阻断。确需新建组件必须说明现有实现缺口、共享影响，
  获得用户明确授权后设置 `new_component_approved=true`，不能只凭“更好看/更厉害”自由发挥。
- 公共组件不能误改，也不能绕开公共组件复制一套。验收必须说明复用了什么、未新增何种重复实现，
  并真实检查页面；代码能运行不等于符合项目设计系统。


- 默认严格按用户指定的 UI 参考执行；只有用户明确说“自由发挥/自由优化”才可调整设计，
  但仍不能扩大文件、数据、接口或业务范围。参考不清楚时先问，不靠印象猜。
- 确认卡必须列出：具体 UI 参考文件/页面/截图、创作权限、允许修改文件、禁止范围、
  额外 Bug 只报告、真实 diff 与参考验收方式。名单外写入会被 Hook 阻断，不是只提醒。
- 建立约束时，在 Hook `UserPromptSubmit` payload 中传 `change_contract`：
  `{version:1, workspace:"...", allowed_files:["..."], references:["..."], mode:"strict", style_only:true}`。
  `mode:"creative"` 只能来自用户明确授权。批准后 Hook 冻结基线；后续改变约定会重新待确认。
- 样式任务默认冻结接口、字段、数据、排序和业务逻辑。发现其他 Bug 只报告位置、原因和影响，
  不擅自修复；需要新增文件、修改数据或扩大业务范围时必须重新确认。
- 完工前运行真实 diff 与参考对照。浏览器可运行不等于参考一致；用截图/对照报告核对后执行
  `scripts/change_contract.py verify-reference --session <当前 session_id> --evidence <路径>`，
  将真实证据绑定到当前约束。缺参考证据、文件越界或样式任务改到业务文件，都不得声称完成。
- 作用域按用户指定对象决定：指定模块/容器/元素就只改该对象；指定某大模块下全部元素，
  只能影响该模块。只有明确“整个项目/全部/全局”才可修改公共组件或全局样式。
- 建立元素级约束时同时填写 `target_scope`、`allowed_properties`、`forbidden_effects`、
  `shared_files/shared_impact`、`unchanged_checks`。局部目标修改共享文件会被 Hook 阻断，
  即使文件在 allowed_files 中也不例外；需要父布局或公共组件影响时必须重新确认。
- 目标外默认全部冻结：兄弟元素位置/尺寸、父容器布局、其他模块和交互不得因本次修改改变。
  例如缩小输入框不能把导出按钮挤到旁边；改 A 模块滚动条不能改公共滚动条影响 B/C 模块。
- 参考对照报告必须逐项写出目标与目标外检查的实际结果，每行使用 `项目: 实际对照结果`，
  例如 `订单输入框: computed style 和位置尺寸与参考一致`、`导出按钮不移位: before/after rect 相同`。
  任意非空文件、只有截图而没有逐项观察，都不是参考一致证明。
- Commander/Fusion 必须继承同一约束，不得因子 Agent 隔离工作区而放宽范围。


收到 `[AgentRelay 任务门禁]` 时：

- 先向用户展示简短的需求卡或确认卡，写清修改范围、关键风险和验证方式。
- 用户明确批准前，只做只读检查、搜索和安全测试；禁止写文件、调用 Fusion、启动
  Commander 或调用其他协作模型。
- 用户用自然语言回复后，由主 Agent 判断语义，再通过 `[AgentRelay 语义审批桥]` 给出的
  `resolve-intent` 命令写回；不要要求用户改用固定短语，也不要由 Hook 关键词直接授权。
- 写回批准后只执行已经展示的当前方案。新增修改范围或改变关键方案时重新确认。
- 任务预检只使用本地确定性规则，不额外调用第二个模型；最终任务解释仍由主 Agent 负责。

收到 `[AgentRelay 安全门禁]` 时：

- 当前工具已经被硬阻断。向用户解释命令用途、影响范围、回滚方式和验证方式，再展示确认卡。
- 用户自然回复是否批准，由主 Agent 结合语境判断并通过语义审批桥写回；Hook 不猜关键词。
- 写回批准后，只能重试完全相同的工具输入一次；任何参数变化都需要重新确认。
- 不要把一次命令批准扩展到同类命令、其他路径或其他任务。

## 卡片显示样式

## 权限容忍度与确认卡去重

- 默认 `standard`：清晰的小修改可直接执行；多文件、多领域、大工程、共享/全局影响、
  模糊目标仍需确认。`strict`：所有新增/修改/删除先确认；`relaxed`：仅危险命令、
  大工程、共享/越界影响需确认。纯问答与只读检查任何模式都不需要确认。
- 用户输入 `/agent-approval` 或 `/设置` 时，显示带说明的数字设置卡；`1=strict`、
  `2=standard`（默认）、`3=relaxed`。Pi 使用原生菜单；其他支持可见 Hook 的宿主由用户
  下一次回复数字完成设置，未打开菜单时不得把普通数字消息当成选择。
- 设置只改变任务确认容忍度，不批准待处理任务，不放宽危险命令、共享影响和元素边界。
  保存必须复用 `approval_policy.py` 并回读 `~/.codex/agent_relay_tracker/approval-policy.json`。
- 无法使用宿主菜单时运行 `scripts/approval_policy.py` 进入本地数字菜单；兼容
  `scripts/approval_policy.py show` 与 `scripts/approval_policy.py set strict|standard|relaxed`。
- 一次动作只显示一张用户可见确认卡。`systemMessage` 给宿主显示，`additionalContext`
  只提醒模型“卡已显示、等待批准”，不得再生成默认提示卡或要求二次确认。
- 危险命令、共享组件/目标外影响、未批准 change_contract 不受宽松模式豁免。
  命令/路径/参数或影响范围变化是新的动作，必须重新确认。


- 默认使用现有纯文本卡片，保证所有 Agent 和终端都能正常阅读。
- 在 Codex 终端中需要彩色详细卡片时，命令显式传 `--card-style codex`，或设置
  `AGENTRELAY_CARD_STYLE=codex`。`auto` 仅在目标 Agent 明确为 Codex 时启用彩色样式。
- Codex 彩色卡使用红色标题、蓝色字段名、绿色英文值、红色加粗风险，并保留
  `[1]`、`[2]`、`[3]` 三个操作选项。
- Codex CLI 的用户可见颜色必须来自脚本真实 stdout 或 Hook 顶层 `systemMessage`。
  `additionalContext` 只给模型读取，不会显示成彩色终端卡片。
- 禁止在普通助手回复里用 Markdown、HTML、CSS、代码块或彩色 emoji 冒充终端颜色；
  这类内容不会得到稳定的 ANSI 渲染。
- 需要彩色卡片时，让 `UserPromptSubmit` 或 `PreToolUse` Hook 返回 `systemMessage`；
  非 Codex 场景继续使用原纯文本卡片。

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

修改 HTML/CSS/JS 或页面模板后，还必须真实打开页面检查，不能用静态测试或“看起来正常”
代替。运行 `scripts/verify_page.py --session <当前 session_id>`（或按 README 使用
`--output` 做独立检查），确认 Chromium 控制台、主要导航、主题、桌面/移动截图均正常。
浏览器不可用、检查失败或文件在检查后再次修改时，必须列为未验证项，不得声称页面完成。
即使会话状态里没有页面文件记录，只要最终回复声称“亲自查看截图、逐项检查、视觉验收通过”，
Hook 也会阻断；必须先形成当前会话可核对的浏览器证据和视觉认证证书。

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
