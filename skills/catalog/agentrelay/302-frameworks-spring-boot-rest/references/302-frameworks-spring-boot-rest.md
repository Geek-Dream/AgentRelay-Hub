---
name: 302-frameworks-spring-boot-rest
description: Use when you need to design, review, or improve REST APIs with Spring Boot — including HTTP methods, resource URIs, status codes, DTOs, versioning, deprecation and sunset headers, content negotiation (JSON and vendor media types), ISO-8601 instants in DTOs, pagination/sorting/filtering, Bean Validation at the boundary, idempotency, ETag concurrency, HTTP caching, error handling, security, contract-first OpenAPI (OpenAPI Generator), controller advice, and problem details for errors.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Boot REST（按需）

- URL 表达资源，HTTP 方法和状态码表达动作与结果；GET 不得修改状态。
- DTO 只暴露必要字段，API 变更保持兼容或走版本化/弃用流程。
- 列表限制分页大小；时间用 ISO-8601、OffsetDateTime 或 Instant。
- 写操作明确幂等键、冲突、ETag/If-Match 等并发语义。
- 错误统一结构（可用 ProblemDetail），不泄露堆栈、SQL 或秘密。
- API-first 以 OpenAPI 为契约，Controller 负责边界编排并委托领域服务。
