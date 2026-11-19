---
name: 121-java-object-oriented-design-code-smells
description: Guidance for identifying and refactoring common object-oriented design code smells.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# 常见代码异味（按需）

- God Class：一个类承担多个独立职责，按业务职责拆分。
- Feature Envy：方法主要操作其他对象的数据，考虑移动行为或引入领域对象。
- Inappropriate Intimacy：类之间直接依赖内部实现，改为公开契约。
- Refused Bequest：子类不需要父类大部分行为时停止继承。
- Shotgun Surgery：一个需求要改许多类，先聚合相关规则。
- Data Clumps：总是一起出现的字段考虑封装为值对象。
