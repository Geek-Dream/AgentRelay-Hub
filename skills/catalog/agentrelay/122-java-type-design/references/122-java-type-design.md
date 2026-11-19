---
name: 122-java-type-design
description: Use when you need to review, improve, or refactor Java code for type design quality — including establishing clear type hierarchies, applying consistent naming conventions, eliminating primitive obsession with domain-specific value objects, leveraging generic type parameters, creating type-safe wrappers, designing fluent interfaces, ensuring precision-appropriate numeric types (BigDecimal for financial calculations), and improving type contrast through interfaces and method signature alignment.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Java 类型设计（按需）

- 建立清晰类型边界：领域对象、值对象、服务、DTO 不混用。
- 使用 EmailAddress、Money 等领域类型替代 primitive obsession。
- 金额/精度敏感计算使用 BigDecimal；计数使用整数类型。
- 泛型约束具体，避免 unchecked cast；优先不可变记录和明确工厂。
- 命名与签名表达业务含义，接口与实现保持一致。
- 不要为“类型漂亮”制造无必要层级；先验证调用方兼容性。
