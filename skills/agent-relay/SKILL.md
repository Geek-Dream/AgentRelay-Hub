---
name: agent-relay
description: "当 Agent 长期无法解决技术问题、重复尝试、有效处理时间过长，或用户明确要求调用 AgentRelay/在线模型时，使用已配置的在线模型 Provider 获取外部技术建议。回复默认输出到终端 stdout；Agent 必须自行判断、修改并验证。"
---

# AgentRelay / Agent Orchestrator

AgentRelay 是 AI Agent 与外部专家模型之间的中继层，也是 Agent Orchestrator 的 Provider 执行层，调用当前已配置的在线模型 Provider。
内部 Skill 名称为 `agent-relay`。

用户明确说“调用 AgentRelay”“使用 AgentRelay”或“让 AgentRelay 分析”时，
等价于明确请求当前配置的在线模型 Provider。“调用在线模型”等表达同样兼容。
此类明确请求应直接调用 AgentRelay；除非用户要求补充上下文，否则不应先搜索工作区、
猜测未知术语或改写用户的问题。

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





# 2.在线模型触发机制

满足以下任意条件时，必须进入在线模型决策：

```text
1. 当前任务本轮 `retry_count >= 3`

2. 当前任务本轮有效处理时间 >= 15 分钟

3. 用户明确要求调用在线模型```

注意：任务按 `task_id` 独立追踪。专家调用完成后开启新的 `relay_round`，本轮计时重新开始，但累计处理时间保留。新任务从自己的 `weight=1` 开始，并不会继承其他任务状态。

权重规则为：0–5 分钟 `weight=1`、5–10 分钟 `weight=2`、10–15 分钟 `weight=3`。`weight=3` 表示接近专家阈值；达到 15 分钟才是时间强制触发条件。

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

只有：

```text
retry_count >= 3
```

或者：

```text
有效问题处理时间 >= 15 分钟
```

或者：

```text
用户明确要求调用在线模型```

才触发在线模型。

达到触发条件后：

```text
禁止继续无限 Debug
禁止继续无休止尝试
必须调用在线模型获取外部建议
```

同一个问题已经调用过在线模型：

```text
relay_triggered = true
```

即使之后再次满足触发条件：

```text
禁止重复调用在线模型```

## 2.1 Codex Hook additionalContext 自动触发通知

当 Agent 收到 Codex Hook 的合法 JSON `additionalContext`，且内容明确包含：

```text
AgentRelay 自动触发条件已经满足
```

则将该通知视为已经确认的自动触发信号：

```text
不需要重新计算 retry_count
不需要重新验证 weight
不需要继续等待更多失败
必须停止当前无限 Debug 或重复尝试
```

应优先使用 `additionalContext` 中的触发原因、`retry_count`、
`effective_time_seconds`，以及 Agent 当前正在解决的问题和已掌握的上下文，
构造真实的在线模型问题。不得把 Hook 测试文本、占位文本或通知本身直接当作问题。

然后必须调用：

```bash
"${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" scripts/agent_relay.py "真实问题内容" -m <模式>
```

调用成功后，使用当前会话已有的 Tracker 标记机制（例如可用会话 ID 时执行
`agent_relay_tracker.py mark-relay --session <session_id> <reason>`）将
`relay_triggered` 标记为 `true`。不得自行发明新的状态文件；如果当前会话 ID
不可用，必须如实记录该限制，不得修改 Tracker 以绕过它。

调用完成后优先读取 stdout 中 `💬 AI回复:` 之后的内容。在线模型回复只是建议，
仍须由 Agent 分析建议、进行最小修改并完成验证。

`relay_triggered = true` 只禁止同一问题的再次自动调用；没有新的用户明确请求时，
不得因 `additionalContext`、`retry_count` 或处理时间再次自动调用。用户之后明确要求
再次调用在线模型时，仍允许按明确请求调用。

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
weight = 3

retry_count = 0
用户没有明确要求在线模型
→ 不触发在线模型```

真正的在线模型触发条件仍然只有：

```text
retry_count >= 3
```

或者：

```text
有效问题处理时间 >= 15 分钟
```

或者：

```text
用户明确要求调用在线模型```

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

仍然：

```text
必须调用在线模型```

注意：

```text
weight = 3
```

可能在：

```text
有效问题处理时间 >= 10 分钟
```

时出现。

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

`agent_relay.py` 是同步命令：工具调用返回即表示该进程已结束。返回后应检查：

```text
💬 AI回复:
AGENT_RELAY_RESULT={"status":"success", ...}
```

看到 `AGENT_RELAY_RESULT` 且 `status` 为 `success` 时，禁止再使用 `sleep`、`ps` 或重复调用
Provider。只有工具明确返回后台 session ID 时，才可轮询该 session。

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

# 17. 再次调用在线模型
同一个问题再次调用时：

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

---

# 18. 最大调用次数

同一个问题自动触发在线模型后：

```text
relay_triggered = true
```

禁止再次自动触发在线模型。

只有用户明确要求再次调用在线模型时，才允许再次调用。

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

判断是否满足触发条件：

```text
retry_count >= 3
```

或者：

```text
有效问题处理时间 >= 15 分钟
```

或者：

```text
用户明确要求调用在线模型```

↓

如果：

relay_triggered = false

↓

满足任意触发条件：

必须调用在线模型
↓

如果：

relay_triggered = true

↓

禁止因为 retry_count 或有效问题处理时间再次自动调用在线模型。

↓

只有用户明确要求再次调用在线模型：

才允许再次调用。

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

11. 每累计 5 分钟有效处理时间增加 1 weight。

12. weight = 3 只是问题复杂度等级，不会单独触发在线模型。

13. retry_count >= 3 必须调用在线模型。

14. 有效问题处理时间 >= 15 分钟必须调用在线模型。

15.在线模型回复不等于问题解决。

16. 修改后必须验证。

17. 问题解决后，下一个问题：

weight = 1
retry_count = 0

18. 新 Session 第一个问题：

weight = 1
retry_count = 0

19. 同一个问题自动调用在线模型后：

relay_triggered = true

禁止再次自动触发。

只有用户明确要求再次调用在线模型时，
才允许再次调用.

20.在线模型是技术顾问，Agent 必须自行判断。
```
