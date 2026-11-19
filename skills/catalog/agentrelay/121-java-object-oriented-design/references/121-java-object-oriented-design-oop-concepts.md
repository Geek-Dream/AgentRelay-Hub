---
name: 121-java-object-oriented-design-oop-concepts
description: Encapsulation, inheritance, and polymorphism guidance for Java object-oriented design.
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# 封装、继承与多态（按需）

- 内部状态私有化，提供最小必要访问接口；可变对象输入输出注意防御性复制。
- 继承必须满足可替换性；只复用代码不代表应该继承。
- 多态用于替换变化的实现，不用 instanceof 分支模拟多态。
- 组合优先于继承，避免深层继承树和跨层隐式耦合。
