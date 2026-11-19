---
name: 312-frameworks-spring-data-jdbc
description: Use when you need to use Spring Data JDBC with Java records — including entity design with records, repository pattern, immutable updates, aggregate relationships, custom queries, transaction management, and avoiding N+1 problems. For programmatic JDBC (`JdbcTemplate`, `NamedParameterJdbcTemplate`), hand-written SQL, and maximum control without repository abstraction, use `@311-frameworks-spring-jdbc`. For Flyway-backed DDL and versioned schema changes, use `@313-frameworks-spring-db-migrations-flyway`.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Data JDBC（仅 JDBC 项目）

- 只有实际使用 Spring Data JDBC 才应用；不要套用到 JPA/MyBatis。
- Repository 围绕聚合根，事务在服务层管理，避免跨聚合任意加载。
- 查询使用参数绑定，禁止拼接用户输入；检查 N+1、乐观锁和更新语义。
- 记录类型不可变，更新通过明确工厂/with 方法表达。
