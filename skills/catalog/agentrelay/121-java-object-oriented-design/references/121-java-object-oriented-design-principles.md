---
name: 121-java-object-oriented-design-principles
description: SOLID, DRY, and YAGNI guidance for Java object-oriented design.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Java 设计原则（按需）

- SRP：类/方法只有一个明确职责；发现多个业务原因修改同一类时优先拆分。
- OCP：扩展行为优先新增实现，不随意修改稳定调用方。
- LSP：子类不能破坏父类契约；不能替换时改组合。
- ISP：接口按调用方需要拆分，避免被迫实现无用方法。
- DIP：业务依赖抽象，具体实现由构造器注入。
- DRY/YAGNI：消除真实重复，但不为假想需求预留抽象。
