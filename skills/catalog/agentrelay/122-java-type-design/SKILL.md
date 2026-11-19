---
name: 122-java-type-design
description: Use when you need to review, improve, or refactor Java code for type design quality — including establishing clear type hierarchies, applying consistent naming conventions, eliminating primitive obsession with domain-specific value objects, leveraging generic type parameters, creating type-safe wrappers, designing fluent interfaces, ensuring precision-appropriate numeric types (BigDecimal for financial calculations), and improving type contrast through interfaces and method signature alignment. This should trigger for requests such as Review Java code for type design; Improve type design in Java code; Fix primitive obsession in Java code; Create value objects in Java code. Part of Plinth Toolkit
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Java 类型设计与值对象

## 工作方式

1. 先确认项目能编译；编译失败时停止修改并报告。
2. 先定位实际问题和涉及文件，只做与请求相关的最小改动，保持现有业务行为。
3. 按下面的场景读取对应参考，不要把所有参考都塞进上下文。
4. 修改后运行与项目匹配的真实构建和测试；不要把 Skill 指南当作架构门禁或验收证据。
5. 输出改动内容、真实验证结果和未验证项。

## 核心规则
- 用领域类型替代 String/int 过度使用，避免 primitive obsession。
- 金额和精度敏感计算使用 BigDecimal，不使用 double/float。
- 命名表达业务含义，接口与实现保持一致签名。
- 泛型边界应具体；优先不可变值对象，避免不必要的类型层级。

## 按需参考
- 完整类型设计参考：`references/122-java-type-design.md`
