---
name: 121-java-object-oriented-design-enums-annotations
description: Enum and annotation guidance for expressive, type-safe Java object-oriented design.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# 枚举与注解（按需）

- 用枚举替代整型常量，不依赖 ordinal 表达业务语义。
- 需要集合语义时优先 EnumSet/EnumMap，避免位字段和 ordinal 索引。
- 重写方法使用 @Override，让编译器帮助发现签名错误。
