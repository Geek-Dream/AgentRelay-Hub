---
name: code-review-excellence
description: Master effective code review practices to provide constructive feedback, catch bugs early, and foster knowledge sharing while maintaining team morale. Use when reviewing pull requests, establishing review standards, or mentoring developers.
---
# 通用代码审查与反馈

## 工作方式

1. 先确认项目能编译；编译失败时停止修改并报告。
2. 先定位实际问题和涉及文件，只做与请求相关的最小改动，保持现有业务行为。
3. 按下面的场景读取对应参考，不要把所有参考都塞进上下文。
4. 修改后运行与项目匹配的真实构建和测试；不要把 Skill 指南当作架构门禁或验收证据。
5. 输出改动内容、真实验证结果和未验证项。

## 核心规则
- 先判断真实 bug、边界条件、安全、架构影响和测试缺口，再看命名与风格。
- 反馈必须具体、可执行、区分阻断级别；不把个人偏好当成缺陷。
- 关注重复实现、隐式契约、错误处理、并发、性能和目标外影响。
- 不用 Skill 替代编译、测试、静态检查或人工验收。

## 按需参考
- 审查方法：`references/review-techniques.md`
- 语言与架构检查：`references/review-patterns.md`
