# AgentRelay Orchestrator v1 实施计划

现有基线：`a1287ea`（当前版本可正常使用）。本计划按阶段推进，每阶段都保持现有 DeepSeek Provider 可用。

## 阶段 1：任务状态与触发状态机

- 将 Tracker 的状态明确为任务级，而不是隐含的全局问题。
- 引入 `task_id`、`relay_round`、`round_effective_time_seconds`、`cumulative_effective_time_seconds`。
- 实现新任务独立计时、专家调用后轮次重置、累计时间保留。
- 保留现有 Hook 输入兼容性和 `additionalContext` 输出格式。
- 为时间阈值、重试阈值、重复触发防护增加行为测试。

## 阶段 2：模型注册表与路由（默认能力已完成）

- 增加 Provider/模型能力描述：能力标签、评分、成本、速度、联网能力。默认 Provider 为已实现的 DeepSeek Web。
- 实现 `direct/worker/expert/commander` 四级决策结果。
- 默认采用“Memory → Codex 自己处理或 DeepSeek Expert”的策略；本地模型和 API 模型只有在用户配置后才进入候选，不把未部署它们算作未完成。
- 将路由结果设计为纯决策对象，不改变现有 Provider 调用入口。

## 阶段 3：Worker 与 Expert 调度协议（当前任务）

- 定义统一任务请求、结果、错误和验证字段。
- 先将现有 DeepSeek Playwright 调用包装为 `DeepSeekWebProvider`，跑通默认 Expert 闭环。
- Worker 返回 diff/结果，由 Codex 审核后合并。
- Expert 默认只返回建议，不直接写工作区。
- 增加调用预算、最大深度、最大并发和超时控制。
- 本地 Provider、API Provider 作为可选扩展，按用户实际配置接入。

## 阶段 4：Memory

- 保存问题、环境、症状、方案、来源、置信度和使用次数。
- 调用前按问题和环境检索；命中后先由 Codex 验证再复用。
- 与聊天历史分离，避免把原始对话当作知识库。

## 阶段 5：Workflow、需求卡与修改历史

- 为跨模块高风险任务生成结构化需求卡。
- 增加 checkpoint、diff 记录和最多 3–6 个可配置历史点。
- 支持验证清单、回滚点和任务状态审计。

## 阶段 6：Commander

- Commander 负责拆解、分配、监控、冲突解决和最终验收。
- 引入 Frontend/Backend/DevOps 等角色和资源 Owner/文件锁。
- 最后再实现空闲 Agent 转岗，避免早期引入并发修改冲突。

## 每阶段验收原则

- 旧版 DeepSeek 登录、会话绑定和命令行调用保持可用。
- Hook/Tracker 出错不能阻塞 Codex。
- 所有自动调用都可解释、可限制、可审计。
- 每次改动完成后运行 Python 编译检查和针对性状态机测试。
