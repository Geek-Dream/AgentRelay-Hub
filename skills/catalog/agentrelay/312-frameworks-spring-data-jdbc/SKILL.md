---
name: 312-frameworks-spring-data-jdbc
description: Use when you need to use Spring Data JDBC with Java records — including entity design with records, repository pattern, immutable updates, aggregate relationships, custom queries, transaction management, and avoiding N+1 problems. This should trigger for requests such as Review Java code for Spring Data JDBC; Apply best practices for Spring Data JDBC in Java code; Model Spring Data JDBC aggregates with Java records; Review Spring Data JDBC repositories and aggregate boundaries; Improve optimistic locking or domain events in Spring Data JDBC. Part of Plinth Toolkit
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Data JDBC 持久化边界

## 工作方式

1. 先确认项目能编译；编译失败时停止修改并报告。
2. 先定位实际问题和涉及文件，只做与请求相关的最小改动，保持现有业务行为。
3. 按下面的场景读取对应参考，不要把所有参考都塞进上下文。
4. 修改后运行与项目匹配的真实构建和测试；不要把 Skill 指南当作架构门禁或验收证据。
5. 输出改动内容、真实验证结果和未验证项。

## 核心规则
- 仅在项目确实使用 Spring Data JDBC 时启用；不要套用到 JPA/MyBatis。
- Repository 围绕聚合根设计，服务层管理事务，避免跨聚合任意加载。
- 查询使用参数绑定，禁止拼接用户输入；关注 N+1 和乐观锁语义。
- 记录类型保持不可变，更新操作通过明确工厂/with 方法表达。

## 按需参考
- 完整 JDBC 参考：`references/312-frameworks-spring-data-jdbc.md`
