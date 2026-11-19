---
name: 301-frameworks-spring-boot-core
description: Use when you need to review, improve, or build Spring Boot 4.0.x applications — including proper usage of @SpringBootApplication, component annotations (@Controller, @Service, @Repository), bean definition and scoping, configuration classes and @ConfigurationProperties (with @Validated), component scanning, conditional configuration and profiles, constructor injection, @Primary and @Qualifier for multiple beans of the same type, bean minimization, graceful shutdown, virtual threads, Jakarta EE namespace consistency, scheduled tasks, and @TestConfiguration.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Boot 核心（按需）

- 主类和组件按职责组织，使用合适的 stereotype。
- 构造器注入；多同类型 Bean 用 @Primary / @Qualifier。
- @ConfigurationProperties 集中配置，@Validated 尽早暴露配置错误。
- 条件配置用 @Profile / @ConditionalOn*，避免业务代码散落环境分支。
- 调度任务明确线程池、异常处理和关闭策略。
- Jakarta 命名空间一致；虚拟线程只在支持的 Java/Spring 栈上启用并验证。
