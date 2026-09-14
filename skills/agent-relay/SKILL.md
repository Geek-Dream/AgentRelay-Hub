---
name: agent-relay
description: "当 Agent 长期无法解决技术问题、重复尝试、有效处理时间过长，或用户明确要求调用 AgentRelay/在线模型时，使用已配置的在线模型 Provider 获取外部技术建议。回复默认输出到终端 stdout；Agent 必须自行判断、修改并验证。"
---

# AgentRelay

AgentRelay 是 AI Agent 与外部专家模型之间的中继层，调用当前已配置的在线模型 Provider。
内部 Skill 名称为 `agent-relay`。

用户真正要求当前执行“调用 AgentRelay”“使用 AgentRelay”或“让 AgentRelay 分析”时，
等价于明确请求当前配置的在线模型 Provider。“调用在线模型”等表达同样兼容。
必须由 Agent 结合完整语境确认这是当前行动指令；讨论规则、引用日志、举例、假设、询问
触发机制，或包含否定表达时，都不是显式调用请求。Hook 可以保守预筛选直接行动句式并提醒
Agent 审核，但无权仅凭关键词替 Agent 下结论。

## 1. Skill 目录

Skill 根目录：当前文件所在目录。以下命令均应从 Skill 根目录执行，或使用脚本相对于自身文件定位的路径。

主要文件：

```text
scripts/agent_relay.py
scripts/agent_relay_login.py
scripts/agent_relay_runtime.py

RecentHistoricalDialogue-Flash.json
RecentHistoricalDialogue-Expert.json

agent_relay_login_state.json
agent_relay_session_bindings.json（首次成功绑定会话后生成）
```

调用前确认：

```text
scripts/agent_relay.py
```

存在。

如果必要脚本缺失，停止调用，不要猜测其他路径。

Provider 脚本必须使用安装器创建的 AgentRelay 独立 Python，不能使用系统 `pip` 安装依赖。
macOS/Linux 使用 `${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python`；Windows 使用
`%CODEX_HOME%\agentrelay-env\Scripts\python.exe`。

---





# 2. 首次进入外援协作

以下任意条件可让当前问题进入首次外援审核：

```text
1. retry_count >= 3

2. 有效问题处理时间 >= 15 分钟

3. 用户消息呈现直接调用的行动句式，经 Agent 结合完整语境审核确认```

注意：

```text
weight = 3
```

只是问题难度等级：

```text
不是在线模型的独立触发条件
```

例如：

```text
有效处理时间 = 10 分钟
weight = 3

→ 不触发在线模型```

首次求援的门槛只有：

```text
retry_count >= 3
```

或者：

```text
有效问题处理时间 >= 15 分钟
```

或者用户当前明确要求调用在线模型。

Hook 给出的计数通知只是候选提醒。收到提醒后，Agent 必须先独立审核：

```text
当前问题是否仍未解决
通知是否确实属于当前问题
当前是否只是下载、安装、网络或构建等待
现有信息是否足以提出有意义的问题
调用外援是否有助于推进，而不是打断即将完成的本地方案
用户是否明确禁止或取消调用
```

Agent 的语义判断优先级最高。审核通过才调用；审核不通过就忽略候选提醒并继续正常处理。
Hook 可以识别“现在调用 DeepSeek”“你触发一下 DeepSeek”“`$agent-relay` 帮我测试”等直接
行动句式，即使 retry 和有效时间未达标也应提醒 Agent 审核。但单独出现 `DeepSeek`、
`AgentRelay`、“调用”“询问”等字样不能成立；最终真实意图仍由 Agent 判断。

首次调用成功后：

```text
relay_triggered = true
```

这个状态表示当前问题已经进入外援协作，只用于抑制 Hook 重复发送首次求援提醒，
不禁止同一问题内携带新信息继续追问。后续协作规则见第 17 节。

## 2.1 Codex Hook additionalContext 候选提醒

当 Agent 收到 Codex Hook 的合法 JSON `additionalContext`，且内容包含 AgentRelay 候选求援提醒时，
只能把它视为计数器提供的审核材料：

```text
不是调用命令
不是已经确认的触发结论
不能覆盖用户真实意图
```

Hook 可以提供：

```text
retry_count
effective_time_seconds
当前问题摘要
疑似直接调用的行动句式候选（可在计数未达标时出现）
```

Agent 必须用第 2 节的条件审核这些信息。尤其要检查问题是否已解决、暂停、切换，摘要是否过时，
以及计时是否来自纯等待。只有审核通过，才使用当前问题和已有上下文构造真实的在线模型问题；
不得把 Hook 文本、测试文本、占位文本或通知本身直接当作问题。

审核通过后调用：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "真实问题内容" -m <模式>
```

首次调用成功后，使用当前会话已有的 Tracker 标记机制（例如可用会话 ID 时执行
`agent_relay_tracker.py mark-relay --session <session_id> <reason>`）将
`relay_triggered` 标记为 `true` 并开始记录调用次数。后续成功追问也可用 `mark-relay`
记录次数。不得自行发明新的状态文件；如果当前会话 ID
不可用，必须如实记录该限制，不得修改 Tracker 以绕过它。

调用完成后优先读取 stdout 中 `💬 AI回复:` 之后的内容。在线模型回复只是建议，
仍须由 Agent 分析建议、进行最小修改并完成验证。

`relay_triggered = true` 只禁止 Hook 为同一问题重复发送首次求援提醒。当前问题采用在线建议后
仍未解决、出现新错误，或在线模型明确要求更多信息时，Agent 可以直接继续追问，无需重新等待
retry 或时间门槛，也不需要用户再次明确要求。

---

# 3. 当前问题

Agent 在处理技术任务时，需要识别：

```text
当前正在解决的技术问题
```

例如：

```text
项目无法启动
```

期间可能出现：

```text
缺少依赖
版本冲突
配置错误
环境异常
```

只要最终目标仍然是：

```text
解决项目无法启动
```

通常仍然属于：

```text
同一个问题链
```

不要因为发现新的错误信息，就立即认为是完全新的问题。

Tracker 使用两层隔离：

```text
Codex Session
├─ 问题 A：独立 effective_time / weight / retry_count
├─ 问题 B：独立 effective_time / weight / retry_count
└─ 当前活动问题
```

同一 Session 内的问题不得合并计时。用户切换到无关问题时，旧问题应归档，新问题从：

```text
effective_time_seconds = 0
weight = 1
retry_count = 0
```

开始。用户说“先不处理”“暂停”“跳过”当前问题时，应立即停止当前问题计时；没有新任务时，
当前活动状态回到空闲零值。问题解决后也立即归档并清空当前计时器，历史快照仍保留在该
Session 的问题目录中。

Tracker 会结合下一条用户消息与 Stop Hook 的 `last_assistant_message` 判断继续、切换、暂停或
明确完成。Agent 在实际验证成功后仍应明确写出“已完成/已修复/验证通过”，不要在尚未解决时
使用这些完成措辞。

---

# 4. 问题权重

默认：

```text
新 Session 第一个问题：

weight = 1
retry_count = 0
```

有效问题处理时间对应的 Weight：

```text
0 ~ 5 分钟
weight = 1

5 ~ 10 分钟
weight = 2

10 分钟及以上
weight = 3
```

也就是：

```text
有效处理时间 < 5 分钟：

weight = 1
有效处理时间 >= 5 分钟：

weight = 2
有效处理时间 >= 10 分钟：

weight = 3
```

最大：

```text
weight = 3
```

注意：

```text
weight
只是问题难度等级。
weight = 3
不会单独触发在线模型。
```

例如：

```text
有效处理时间 = 10 分钟
weight = 2

retry_count = 0
用户没有明确要求在线模型
→ 不触发在线模型```

首次进入外援审核的条件仍然只有：

```text
retry_count >= 3
```

或者：

```text
有效问题处理时间 >= 15 分钟
```

或者：

```text
Hook 发现直接行动句式候选，且 Agent 确认用户真正要求现在调用在线模型```

计数条件满足后仍须由 Agent 审核当前问题是否值得调用；weight 本身不负责触发。

Weight 的作用是：

```text
帮助 Agent 判断当前问题的处理复杂度。

weight 越高，
说明当前问题已经持续处理了更长时间，
需要提高警惕。

但是：

weight 本身不负责触发在线模型。
```

---

# 5. 什么时间算有效处理时间

以下属于有效问题处理：

```text
分析错误
查看日志
阅读相关代码
修改代码
修改配置
排查依赖
Debug
测试失败后继续分析
尝试新的解决方案
```

这些时间：

```text
计入有效问题处理时间
```

---

## 不计算的时间

以下属于正常等待：

```text
下载依赖
下载插件
下载模型
pip install
npm install
Docker pull
单纯执行 curl 下载或等待网络响应
网络等待
大型项目正常构建
```

这些动作执行期间：

```text
不增加有效问题处理时间

也就是：

effective_time_seconds 不增加

因此：

weight 也不会因为这些等待操作而增加
```

即使：

```text
下载持续 20 分钟
```

也不能因为：

```text
单纯等待下载

增加有效问题处理时间。
```



核心原则：

```text
Agent 正在主动解决问题
→ 计算有效问题处理时间

Agent 只是等待下载、安装或网络操作
→ 不计算有效问题处理时间
```

---

# 6. retry 的定义【重要】

第一次尝试某种解决方向：

```text
不算 retry
```

只有：

```text
已经尝试过一种解决方向
↓
仍然失败
↓
再次尝试相同或类似解决方向
```

才：

```text
retry_count +1
```

---

## 示例：下载

第一次：

```text
下载某个插件
```

失败：

```text
retry_count = 0
```

再次尝试下载：

```text
retry_count = 1
```

---

## 示例：换镜像

第一次：

```text
使用镜像 A
```

失败。

然后：

```text
使用镜像 B
```

这是第一次尝试新的镜像方案：

```text
通常不算 retry
```

如果继续：

```text
镜像 B 失败
↓
再换镜像 C
```

则：

```text
retry_count +1
```

因为已经进入：

```text
重复通过更换镜像解决同一个问题
```

---

## 示例：依赖版本

第一次：

```text
修改 Redis 版本
```

失败：

```text
不算 retry
```

再次：

```text
继续修改 Redis / Spring 相关版本
```

尝试解决同类兼容问题：

```text
retry_count +1
```

---

# 7. 什么不算 retry

以下情况通常不增加 retry：

```text
第一次尝试一个方案

发现缺少依赖
→ 安装依赖

发现新的版本冲突
→ 第一次处理版本问题

发现新的配置问题
→ 第一次处理配置

切换到完全不同的技术解决方向
```

核心判断：

```text
是否已经尝试过这个解决方向，
现在又再次尝试？
```

如果：

```text
是
```

则增加：

```text
retry_count
```

否则：

```text
继续正常处理
```

---

# 8. 重试与有效问题处理时间独立

需要注意：

```text
retry_count
```

和：

```text
effective_time_seconds
```

是两个独立机制。

例如：

```text
Agent 尝试了很多完全不同的方法
```

每一种方法：

```text
都只尝试一次
```

因此：

```text
retry_count 可能一直很低
```

但是：

```text
整个问题已经持续处理了 15 分钟
```

只要这些属于：

```text
有效问题处理时间
```

则：

```text
effective_time_seconds >= 15 分钟
```

仍然达到：

```text
首次外援审核门槛```

注意：

```text
weight = 3
```

只会在：

```text
有效问题处理时间 >= 10 分钟
```

时出现；15 分钟仍是独立的外援触发门槛。

但是：

```text
weight = 3
```

本身：

```text
不会触发在线模型```

真正触发时间条件的是：

```text
effective_time_seconds >= 15 分钟
```

这样可以防止 Agent：

```text
无限寻找新方案
无限 Debug
长期卡在一个问题
```

---

# 9. 问题解决后的状态

问题必须经过实际验证。

例如：

```text
编译成功
测试成功
程序正常运行
原错误消失
```

确认解决后，当前问题结束。

当前活动计时器立即回到：

```text
effective_time_seconds = 0
weight = 0
retry_count = 0
```

下一个问题默认：

```text
retry_count = 0
weight = 1
```


如果是新的 Session：

```text
新 Session 第一个问题：

weight = 1
retry_count = 0
```

无需手查 Session ID 即可查看最近活动会话：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" status
```

查看会话列表和最近会话内的独立问题：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" sessions
python3 "${CODEX_HOME:-$HOME/.codex}/hooks/agent_relay_tracker.py" problems
```

---

# 10.在线模型调用方式

DeepSeek 当前将极速、图片和专家能力统一到一个会话。`-m 1`、附带图片的请求和 `-m 2`
继续表达请求类型并决定历史文件，但都复用同一个持久化会话绑定。绑定不存在或失效时，脚本
扫描左侧对话栏；仍未找到则用本次问题创建新会话，重命名为 `AgentRelay-DeepSeek` 后绑定。
不要要求用户预先创建或重命名会话。

## 极速模式

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "问题内容" -m 1
```

支持：

```text
文本
图片
文件
```

适用于：

```text
普通问题
截图
UI
报错图片
日志
代码文件
配置文件
```

---

## 专家模式

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "问题内容" -m 2
```

支持：

```text
文本
```

适用于：

```text
复杂代码逻辑
算法
架构
并发
数据库
复杂异常分析
```

---

## 模式选择

```text
有图片或文件
→ 极速模式 -m 1

普通问题
→ 极速模式 -m 1

复杂纯文本问题
→ 专家模式 -m 2
```

## 图片转发规则

用户提供了图片、截图或文件，并要求 AgentRelay 查看时，原文件是请求的必要部分。
必须使用真实、可读取的本地路径调用：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py \
  "用户的原问题" -m 1 --image "/path/to/image.png"
```

多张图片对每个路径重复使用 `--image`。调用前应确认文件存在。禁止用 Agent
自己的 OCR、摘要或视觉描述代替原图，除非用户明确要求只发送文字描述。

如果输入只显示为 `[Image #N]`，且当前运行时没有提供可传给 CLI 的本地文件路径，
不得进行纯文本 AgentRelay 调用；应直接说明无法取得原图文件，请用户提供或保存图片路径。

图片调用成功后，`AGENT_RELAY_RESULT.images_count` 必须大于 `0`；否则表示本次没有
实际转发图片，不得向用户声称在线模型已看到原图。

---

# 11.在线模型回复在哪里【必须记住】

调用：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "问题内容" -m 1
```

或者：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "问题内容" -m 2
```

完成后：

```text
在线模型回复默认直接输出到终端 stdout
```

Agent 必须读取：

```text
命令执行结果
```

寻找：

```text
💬 AI回复:
```

之后的内容。

例如：

```text
💬 AI回复:

这里是在线模型的分析内容……
```

### 强制规则

调用：

```text
agent_relay.py
```

成功完成后：

```text
优先检查终端 stdout
```

`agent_relay.py` 本身是同步命令，但终端工具可能因为等待窗口到期先返回后台 session ID。
如果工具明确返回 `Process running with session ID ...`，必须使用终端工具提供的
`write_stdin`/继续读取机制轮询**这个原 session ID**，直到进程结束并取得最终 stdout。
禁止运行 `sleep 15`、`sleep 60` 等新命令代替轮询；这些命令会创建无关的新 session，
永远读不到原 AgentRelay 结果。

进程结束后应检查：

```text
💬 AI回复:
AGENT_RELAY_RESULT={"status":"success", ...}
```

看到 `AGENT_RELAY_RESULT` 且 `status` 为 `success` 时，禁止再使用 `sleep`、`ps` 或因为
“没看到结果”而重复调用 Provider。后续因实施失败而带新信息继续追问，属于第 17 节规定的
外援协作，不属于重复读取。

如果 stdout 在界面中被折叠或截断，根据 `AGENT_RELAY_RESULT.history_file` 读取对应历史 JSON
一次，取最后一条的 `answer`。不得因输出折叠而重复调用 Provider。

禁止：

```text
调用成功
↓
不知道输出在哪里
↓
再次调用在线模型```

---

# 12.在线模型历史记录

极速模式：Skill 根目录下的

```text
RecentHistoricalDialogue-Flash.json
```

专家模式：Skill 根目录下的

```text
RecentHistoricalDialogue-Expert.json
```

两个文件：

```text
最多保存最近 6 条对话
```

极速模式：

```text
支持图片历史记录
```

---

## stdout 与历史记录优先级

刚刚调用完成：

```text
第一优先级：

终端 stdout
```

只有以下情况才读取 JSON：

```text
stdout 被截断
Context 被压缩
需要查看之前在线模型建议
需要恢复最近对话信息
```

规则：

```text
刚调用在线模型→ stdout

需要历史信息
→ 对应模式 JSON
```

不要：

```text
刚调用完成
↓
无原因重复读取 JSON
```

---

# 13. 构造在线模型问题

发送问题时尽量包含：

```text
目标：
需要实现什么

环境：
语言 / 框架 / 版本

问题：
当前发生什么

错误：
关键错误信息

已尝试：
已经尝试过什么

代码：
必要的相关代码

验证结果：
最后一次执行结果
```

优先发送：

```text
错误信息
+
关键代码
+
已尝试方案
+
最新验证结果
```

不要发送：

```text
整个项目
大量无关代码
```

---

# 14. 上下文复用

调用在线模型时：

```text
必须优先使用 Agent 当前已经获得的信息。
```

禁止因为调用在线模型：

```text
重新读取已经读取过的大文件
```

尤其避免：

```text
读取大文件
↓
Context Compact
↓
再次读取整个大文件
↓
再次 Compact
```

如果需要重新读取：

优先：

```text
错误相关函数
错误行
关键配置
相关代码片段
```

不要无原因重复读取整个文件。

---

# 15.在线模型调用后

在线模型只是：

```text
外部技术顾问
```

正确流程：

```text
在线模型回复
↓
Agent 分析建议
↓
检查项目兼容性
↓
选择最小修改
↓
修改
↓
验证
```

在线回复不是交付结果。只要安全且在用户授权范围内，Agent 应自行编辑文件、执行脚本、调整配置
并验证，不能把在线模型给出的分析或脚本原样转交用户后就结束任务。只有缺少用户专属信息、
需要新的权限或授权、涉及危险操作，或本地确实无法取得必要条件时，才向用户提问或请求操作。

必须检查：

```text
版本是否兼容
API 是否存在
依赖是否存在
是否重复之前失败方案
```

禁止：

```text
直接复制全部代码
覆盖整个文件
因为小问题大规模重构
```

优先：

```text
最小可行修改
```

---

# 16. 修改后验证

修改代码或配置后：

```text
必须执行适合项目的验证。
```

优先：

```text
项目已有构建命令
测试命令
启动命令
CI 命令
```

例如：

Python：

```bash
python3 -m py_compile <文件>
pytest
```

Maven：

```bash
mvn compile
mvn test
```

Node：

```bash
npm run build
npm test
```

Go：

```bash
go build ./...
go test ./...
```

未经实际验证：

```text
不得宣称问题已经解决
```

---

# 17. 当前问题内的连续外援协作

首次调用成功后，当前问题进入外援协作阶段。Agent 必须先实施并验证在线建议。

出现以下任一情况时，可以直接继续调用在线模型，不重新等待 retry、有效时间或用户显式请求：

```text
实施建议后验证失败
出现属于同一目标的新错误
在线模型要求提供更多日志、配置、代码或环境信息
建议存在关键歧义，无法安全实施
```

如果在线模型要求更多信息，Agent 应先使用现有工具自行收集，再把信息发回同一在线会话。
只有信息属于用户专属秘密、需要授权或本地无法取得时，才询问用户。

每次后续追问必须增加至少一种真实的新信息：

禁止：

```text
发送完全相同的问题
```

必须增加新的信息：

```text
上次在线模型建议

实际修改内容

新的错误信息

新的验证结果
```

这样在线模型可以继续分析。

没有新信息时禁止机械重复追问。此时应继续本地排查，或在确实缺少用户信息/权限时向用户说明
阻塞点。不要设置固定的一次调用上限，也不要无进展地无限空问；是否继续以“同一问题仍未解决
且本轮有新增事实可以推动分析”为准。

以下情况结束当前问题的连续外援资格：

```text
问题已实际验证解决
用户暂停、跳过或放弃当前问题
用户切换到无关问题
登录失效或 Provider 不可用
必须等待用户提供信息或授权，当前无法继续
```

新问题必须重新独立计算首次求援门槛，不能继承旧问题的外援资格。

---

# 18. 防重复与问题隔离

同一个问题首次成功调用在线模型后：

```text
relay_triggered = true
```

Hook 禁止再次发送首次求援候选提醒，但 Agent 可以按第 17 节进行有新信息的连续追问。
`relay_triggered` 不是“一生只能调用一次”的锁，而是“当前问题已经进入外援协作”的标记。

---

# 19. 登录失败

如果出现：

```text
未登录
Cookie 失效
Session expired
Unauthorized
无法访问在线模型```

停止自动查询。

提示用户：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay_login.py
```

禁止：

```text
无限登录
无限重复尝试
绕过登录
绕过验证码
```

---

# 20. 完整流程

```text
技术问题
↓
开始处理

weight / retry 记录
↓
Agent 自主分析
↓
是否等待下载？
├─ 是
│  ↓
│ 不计算处理时间
│
└─ 否
   ↓
累计有效问题处理时间
```

同时判断：

```text
是否重复尝试相同解决方向？
```

第一次：

```text
不算 retry
```

再次尝试：

```text
retry_count +1
```

判断是否达到首次求援审核门槛：

```text
retry_count >= 3
```

或者：

```text
有效问题处理时间 >= 15 分钟
```

或者：

```text
Hook 发现直接行动句式候选，交给 Agent 结合完整语境确认```

↓

如果：

relay_triggered = false

↓

Hook 达到计数门槛或发现直接行动句式时发送候选提醒
↓
Agent 审核当前问题、真实意图与求援价值
├─ 不值得调用：忽略提醒，继续正常处理
└─ 值得调用：首次调用在线模型并进入外援协作
   ↓
   relay_triggered = true
   ↓
   Agent 实施建议并验证
   ├─ 成功：结束当前问题
   ├─ 失败或新错误：带新结果继续追问，不重新等待门槛
   ├─ 在线模型要更多信息：Agent 收集后继续追问
   └─ 缺用户信息/授权或危险操作：询问用户

```text
最小修改
```

↓

```text
验证
```

成功：

```text
当前问题结束
```

↓

```text
下一个问题：

weight = 1
retry_count = 0
```

---

# 21. 核心规则

```text
1.在线模型默认回复在终端 stdout。

2. 从「💬 AI回复:」之后获取回答。

3. stdout 优先，历史 JSON 备用。

4. 极速历史：
RecentHistoricalDialogue-Flash.json

5. 专家历史：
RecentHistoricalDialogue-Expert.json

6. 两个历史最多保存 6 条。

7. 极速模式支持图片记录。

8. 第一次尝试不算 retry。

9. 再次尝试同一个解决方向才算 retry。

10. 下载、安装、网络等待不增加 weight。

11. 有效时间小于 5 分钟为 weight 1；达到 5 分钟为 weight 2；达到 10 分钟为 weight 3。

12. weight = 3 只是问题复杂度等级，不会单独触发在线模型。

13. retry_count >= 3 时 Hook 可发首次求援候选提醒，最终由 Agent 审核是否值得调用。

14. 有效问题处理时间 >= 15 分钟时 Hook 可发首次求援候选提醒，最终由 Agent 审核。

15.在线模型回复不等于问题解决。

16. 修改后必须验证。

17. 问题解决后，下一个问题：

weight = 1
retry_count = 0

18. 新 Session 第一个问题：

weight = 1
retry_count = 0

19. Hook 可预筛选直接调用的行动句式，即使计数未达标也发送候选提醒；单纯出现关键词不提醒。

20. 用户显式请求最终必须由 Agent 理解完整语境并审核确认，Hook 候选不能直接下令调用。

21. 同一个问题首次调用在线模型后：

relay_triggered = true

Hook 不再重复发送首次提醒，但 Agent 可在实施失败、新错误或缺信息时带新事实继续追问。

22. 在线回复不是最终交付；Agent 必须自行实施并验证，不能只把回复或脚本交给用户。

23. 工具返回后台 session ID 时必须轮询原 session，禁止用 sleep 代替。

24. 新问题不能继承旧问题的外援资格。

25. 在线模型是技术顾问，Agent 始终保留最高判断权。
```

## Commander 子任务合并

Commander 是主审查官协调的 1 到 5 个隔离子 Agent。角色按任务动态规划，可包含前端样式、
前端脚本、后端代码、数据库和 Git 审计；没有可用 Provider 时仍可由 Codex 子 Agent 离线执行，
不能把“已规划”伪报成“已完成”。子 Agent 继承父任务的约束，但不能递归启动 Commander。

每个子 Agent 都必须使用独立 workspace 和父任务预分配的 `task_id`。它们的重试次数、有效时间、
权重和外援提醒完全隔离；达到三次重试或 15 分钟时，只通知 Commander 并继续工作。网页会话和本地
模型服务同一时间只能由一个角色占用，API Provider 不得被 Commander 子 Agent 直接调用。

子任务结束后，主审查官必须读取每个报告、状态、验证结果和基线差异，再生成合并计划与用户进度
摘要。合并计划至少包含完成/未完成角色、修改文件、验证结果、冲突原因和继续工作状态。必须先
展示独立的合并确认卡；只有用户批准后才逐文件写回主 workspace。主 workspace 被用户修改、角色
之间内容冲突、路径越界或验证失败时，合并暂停且不得覆盖用户文件。

合并结果需记录成功角色、未完成角色、实际修改文件和验证状态，并交给主审查官总结。运行时事件
可通过以下命令查看和驱动：

```bash
python3 scripts/agentrelay_console.py commander-merge TASK_ID \
  --workspace /path/to/project --runtime-root .agentrelay/commander
```

确认卡、需求卡、验证卡和回滚卡始终由主 Agent/Workflow 控制；外部模型只提供建议或受限修改，
不能绕过确认、隔离、审查和验证流程。
