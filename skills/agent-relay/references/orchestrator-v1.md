# Agent Orchestrator v1 设计约定

## 目标

在现有 AgentRelay（Hook → Tracker → Skill → Provider）基础上增加任务编排能力，保持 Hook 不直接调用 Provider，最终决策仍由 Codex/Skill 完成。

## 任务边界

每个独立问题拥有自己的 `task_id` 和状态。新问题不会继承旧问题的计时、重试次数或专家调用轮次；并发问题之间互不影响。

## 时间权重与专家轮次

`round_effective_time_seconds` 表示自本轮开始（新任务或上一次专家调用完成）以来的有效处理时间：

- 0–5 分钟且未命中归档复发：`weight = 1`
- 5–15 分钟，或当前项目归档保守命中同一问题：`weight = 2`（先查本机归档，复用已验证结论）
- 达到 15 分钟：`weight = 3`，本轮进入专家决策判断

归档复发命中只设置 `weight_floor = 2`，让本轮先进入归档查询；不得直接设为 3，也不得绕过
15 分钟/重试门槛直接调用专家。专家调用完成后开启新的轮次，并清除复发权重下限。
`cumulative_effective_time_seconds` 保留任务全生命周期累计时间，不能因为轮次重置而丢失。

## 触发优先级

1. 用户明确要求调用 AgentRelay/在线模型：立即触发。
2. 当前轮次达到 15 分钟：必须触发。
3. 当前轮次 `retry_count >= 3`：必须触发。
4. `weight` 只表示当前轮次的时间等级；`weight = 3` 表示接近专家阈值，不单独绕过 15 分钟规则。

同一轮只生成一次自动触发信号。专家调用完成后，`relay_round` 加一并重新计算下一轮时间。

## Agent 类型

- `direct`：Codex 自己处理。
- `worker`：一次性子任务，适合本地模型完成低风险修改并返回结果/diff。
- `expert`：只提供分析和建议，默认不直接修改项目。
- `commander`：拆解跨模块任务、分配子任务、协调验证；受并发数和深度预算限制。

默认编排 Provider 是无需外部服务的本地规则/Codex self-subagent。DeepSeek Web/API、`local-9b`、GPT API 等都是用户显式配置后的可选增强；没有任何外部模型时，需求卡、确认卡、执行、验证、回滚和 Memory 仍必须完整工作。

## 资源所有权

任何 Agent 修改文件前必须声明资源 Owner。非 Owner Agent 只能提出建议，除非显式完成 ownership transfer。后续实现文件锁和冲突检测时必须保留 `primary_owner` 标记。

## 预算与防套娃

任务状态应支持 `max_model_calls`、`max_parallel_agents`、`max_depth` 和 `max_minutes`。预算耗尽时停止自动派发并交回 Codex 决策。
