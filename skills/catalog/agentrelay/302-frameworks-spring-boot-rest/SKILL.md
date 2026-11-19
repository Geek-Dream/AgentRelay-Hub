---
name: 302-frameworks-spring-boot-rest
description: Use when you need to design, review, or improve REST APIs with Spring Boot — including HTTP methods, resource URIs, status codes, DTOs, versioning, deprecation and sunset headers, content negotiation (JSON and vendor media types), ISO-8601 instants in DTOs, pagination/sorting/filtering, Bean Validation at the boundary, idempotency, ETag concurrency, HTTP caching, error handling, security, contract-first OpenAPI (OpenAPI Generator), controller advice, and problem details for errors. This should trigger for requests such as Review Java code for Spring Boot REST API; Apply best practices for Spring Boot REST API in Java code; Design Spring Boot REST controllers and DTOs; Add Problem Details error responses in Spring Boot REST; Improve pagination validation or idempotency in Spring APIs. Part of Plinth Toolkit
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Boot REST API 设计

## 工作方式

1. 先确认项目能编译；编译失败时停止修改并报告。
2. 先定位实际问题和涉及文件，只做与请求相关的最小改动，保持现有业务行为。
3. 按下面的场景读取对应参考，不要把所有参考都塞进上下文。
4. 修改后运行与项目匹配的真实构建和测试；不要把 Skill 指南当作架构门禁或验收证据。
5. 输出改动内容、真实验证结果和未验证项。

## 核心规则
- URL 用资源名词，HTTP 方法和状态码表达真实语义，避免 GET 产生副作用。
- DTO 只暴露必要字段，保持客户端契约稳定；破坏性变化走版本化或弃用流程。
- 列表接口必须限制分页大小；时间字段使用 ISO-8601 / OffsetDateTime / Instant。
- 写操作考虑幂等、冲突和并发条件；错误响应统一、不得泄露堆栈或秘密。
- API-first 时以 OpenAPI 契约为准，Controller 只做边界编排并委托领域服务。

## 按需参考
- 完整 REST 参考：`references/302-frameworks-spring-boot-rest.md`
