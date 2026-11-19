---
name: 121-java-object-oriented-design-classes-interfaces
description: Class and interface design guidance covering accessibility, mutability, composition, and inheritance.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# 类与接口（按需）

- 降低类、字段和方法的可访问性，禁止无必要 public 可变字段。
- 优先不可变对象；必须可变时限制修改入口。
- 组合复用行为，继承只用于真正可替换的层级。
- 不打算支持继承时明确禁止或谨慎设计构造器和重写契约。
