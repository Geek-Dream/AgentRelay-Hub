---
name: 121-java-object-oriented-design-exceptions
description: Object-oriented exception design guidance covering exceptional conditions, exception types, messages, and handling.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# 异常契约（按需）

- 不用异常控制正常流程；优先使用合适的标准异常。
- 可恢复条件与编程错误区分处理，异常信息说明失败原因和定位上下文。
- 不空 catch、不吞异常；确实无法处理时记录并保留原始异常。
