---
name: 303-frameworks-spring-boot-validation
description: Use when you need to design, review, or improve validation in Spring Boot applications — including Bean Validation on request DTOs, @Valid/@Validated at API boundaries, constraint groups, custom constraints, @ConfigurationProperties validation, nested DTO validation, and consistent validation error handling.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Boot 校验（按需）

- 请求 DTO 声明约束，控制器参数使用 @Valid / @Validated。
- 创建/更新用校验组，嵌套对象和集合正确级联。
- 自定义约束保持单一职责，配置属性也要验证。
- 校验失败统一 400 与一致错误体，不泄露内部字段和堆栈。
