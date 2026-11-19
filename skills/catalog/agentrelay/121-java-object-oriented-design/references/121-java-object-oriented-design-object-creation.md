---
name: 121-java-object-oriented-design-object-creation
description: Object creation guidance covering factories, builders, singletons, dependency injection, and unnecessary objects.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# 对象创建（按需）

- 参数多且有多种组合时使用 Builder 或静态工厂，避免巨型构造器。
- 依赖通过构造器注入，不在业务对象内部硬编码资源。
- 不为简单场景引入 Singleton；避免无意义对象和重复临时对象。
