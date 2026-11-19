---
name: 301-frameworks-spring-boot-core
description: Use when you need to review, improve, or build Spring Boot 4.0.x applications — including proper usage of @SpringBootApplication, component annotations (@Controller, @Service, @Repository), bean definition and scoping, configuration classes and @ConfigurationProperties (with @Validated), component scanning, conditional configuration and profiles, constructor injection, @Primary and @Qualifier for multiple beans of the same type, bean minimization, graceful shutdown, virtual threads, Jakarta EE namespace consistency, and scheduled tasks. This should trigger for requests such as Review Java code for Spring Boot application; Apply best practices for Spring Boot application in Java code; Improve Spring Boot configuration properties and beans; Review Spring Boot component scanning and profiles; Tune Spring Boot graceful shutdown or virtual threads. Part of Plinth Toolkit
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Boot 核心与依赖注入

## 工作方式

1. 先确认项目能编译；编译失败时停止修改并报告。
2. 先定位实际问题和涉及文件，只做与请求相关的最小改动，保持现有业务行为。
3. 按下面的场景读取对应参考，不要把所有参考都塞进上下文。
4. 修改后运行与项目匹配的真实构建和测试；不要把 Skill 指南当作架构门禁或验收证据。
5. 输出改动内容、真实验证结果和未验证项。

## 核心规则
- 优先构造器注入，避免字段注入和循环依赖。
- 配置集中到 @ConfigurationProperties，并用 @Validated 提前发现错误。
- 组件按职责放置；条件配置使用 @Profile / @ConditionalOn*，不要在业务代码散落环境分支。
- 多同类型 Bean 用 @Primary / @Qualifier；Bean 数量少而清晰。
- Jakarta 命名空间保持一致，虚拟线程只在真实支持的栈上启用。

## 按需参考
- 完整核心参考：`references/301-frameworks-spring-boot-core.md`
