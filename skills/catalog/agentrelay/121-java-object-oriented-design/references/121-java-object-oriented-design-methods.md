---
name: 121-java-object-oriented-design-methods
description: Method design guidance covering validation, defensive copies, signatures, collections, and Optional.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# 方法契约（按需）

- 对非法参数尽早失败，错误信息包含定位所需上下文，不泄露秘密。
- 可变入参/返回值按需防御性复制。
- 集合为空时返回空集合，不返回 null；Optional 只用于明确可缺失的返回值。
- 方法签名保持类型、命名和异常行为一致，避免隐藏副作用。
