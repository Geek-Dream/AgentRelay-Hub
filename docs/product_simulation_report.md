# AgentRelay 产品级模拟验收报告

## 验收范围

本报告使用 `tests/test_product_simulation.py` 重放最初需求中的 7 个用户场景。测试全部使用临时目录、离线规则、Mock Provider 和 Mock Executor，不访问外部模型，不修改仓库工作区。

本轮还验证了 P1 的实际运行链路：Hook 事件先写入 `runtime/events/queue.jsonl`，daemon 消费队列并绑定 `TaskContext`，达到阈值后由 `WorkflowEngine.evaluate_escalation()` 咨询一次配置的 Provider，并将结果持久化。

回归结果：`129 tests passed, 1 skipped`，并通过 `compileall`、Hook shell 语法和 `git diff --check`。

## 结果摘要

| 场景 | 结果 | 结论 |
|---|---|---|
| 1. Redis 依赖持续失败 | 通过 | Hook 事件进入统一队列并绑定 TaskContext，时间权重、重试和升级条件生效；DeepSeek 不可用时回退 LocalProvider，经验写入 Memory |
| 2. 简单按钮样式修改 | 通过 | 路由到 LocalLLMProvider，解析结构化 edits，经过 allowed_files 和安全 Executor，未生成需求卡 |
| 3. RabbitMQ 迁移 Kafka | 通过 | RequirementCard 字段完整，ConfirmationCard 阻止未确认执行，确认后完成修改、验证、Checkpoint 和 ChangeLog |
| 4. 验证失败自动回滚 | 通过 | changed_files 存在时执行 FAILED → ROLLBACK → ROLLED_BACK，原文件内容恢复 |
| 5. Commander 多 Agent | 通过 | Agent 有独立 workspace 和上下文，并行执行、失败隔离、主审查官汇总、基线差异审查和显式合并确认可用 |
| 6. Memory 命中 | 通过（基础版） | 历史经验进入新 TaskContext 和 Provider 分析/计划输入，并按项目隔离；不会未经验证直接改代码 |
| 7. 模型路由 | 通过（基础版） | 简单→Local、普通技术问题→DeepSeek、复杂→GPT；Provider 不可用时回退 local，结果回写 Workflow |

P1 事件队列闭环：**通过**。真实 Hook 适配器将带 `task_id` 的事件写入 `runtime/events/queue.jsonl`；daemon 可在后续轮询消费，权重和升级条件由统一上下文计算，重复轮询不会重复触发同一阈值的咨询。

P2/P3 模型反馈与升级：**通过（受配置条件约束）**。模型分析/计划结果会写入 Workflow 结果和 TaskContext；达到阈值后，已有批准卡的任务会咨询外援并复用原授权范围重试，成功时权重降为 1，失败继续原有回滚/升级流程。

Commander 合并闭环：**通过**。每个子 Agent 的 workspace 会与启动前基线比较，主审查官输出完成角色、修改文件、验证状态和冲突原因；主工作区只有在独立合并确认卡批准后才逐文件更新。并发修改或角色间内容冲突会暂停合并，不覆盖用户文件。

## 场景详情

### 场景 1：Spring Redis 依赖问题

输入是“添加 Redis 缓存组件”。测试按 `INSTALL_FAILED`、`BUILD_FAILED`、`RETRY` 写入同一个 `task_id`。有效时间推进到 5、10、15 分钟后，`weight` 达到 1、2、3，`retry_count` 达到 3，`need_escalation=true`。`TaskDecisionEngine` 将其识别为 `BLOCKED_TASK`，路由器在有 Provider 时选择 DeepSeek；模拟 DeepSeek 不可用，`EscalationManager` 返回成功的 LocalProvider fallback。解决方案写入项目 Memory。

### 场景 2：简单样式任务

本地模型返回 JSON `edits`，WorkflowEngine 只接受 `button.css` 授权范围内的修改，最终由安全 Executor 写入。结果是 `SIMPLE_TASK` 和 `SUCCESS`，没有 RequirementCard，也没有调用高级模型。

### 场景 3：MQ 到 Kafka

需求卡包含需求评估、白话解释、影响模块/文件、风险、验证方案和回滚方案。提交后状态是 `WAITING_CONFIRMATION`，Checkpoint 尚未创建且 Executor 未执行；批准后完成写入、验证、Checkpoint 和 ChangeLog。

### 场景 4：验证失败

模拟 Executor 先写入临时内容，再返回 pytest 失败。WorkflowEngine 发现 `changed_files`，执行回滚并恢复原内容，最终状态为 `ROLLED_BACK`。没有修改用户的其他文件。

### 场景 5：Commander

Frontend、Backend、Git 三个 Agent 拥有不同 workspace 和 TaskContext。Backend 抛出异常时，Frontend/Git 仍然完成，汇总结果保留失败信息和审查建议。真实 Codex 子进程属于显式 opt-in，未在离线产品测试中冒充已验证。

### 场景 6：Memory

第一次 Redis 连接异常保存成功方案；第二次相同项目任务提交时，历史记录出现在 `TaskContext.memory_hits`，并进入 Provider 的分析/计划数据。项目隔离有效；系统仍要求当前验证，不会盲目跳过执行。

### 场景 7：模型路由

注册 Local Qwen、DeepSeek、GPT API 三个可用 Provider 后，简单任务选择 Local，带联网需求的技术研究选择 DeepSeek，高复杂度任务选择 GPT。移除外部 Provider 后复杂任务回退到 local，默认离线原则保持。

## 验收判定

这些测试验证了 AgentRelay 的核心产品闭环，而不是只验证单个类能实例化。确认卡、安全执行、失败回滚、Hook 绑定、模型 fallback、并行隔离和 Memory 检索均有场景证据。Hook 还会将常见 `event_name`/`hookEventName` 与失败错误字段映射到统一任务事件。未通过或未验证的部分已在表格中单独标出，没有将真实凭据、真实联网服务或真实 Codex 进程的缺席伪造成通过。
