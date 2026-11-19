---
name: 121-java-object-oriented-design
description: Use when reviewing, improving, or refactoring Java object-oriented design, including applying SOLID, DRY, or YAGNI; improving classes and interfaces; correcting encapsulation, inheritance, or polymorphism; resolving God Class, Feature Envy, or Data Clumps; and improving object creation, methods, or exception contracts. Triggers include review Java OOD, refactor Java OOD, improve Java OOD, fix OOP misuse, and identify Java code smells. Part of Plinth Toolkit
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Java 面向对象设计审查与重构

## 工作方式

1. 先确认项目能编译；编译失败时停止修改并报告。
2. 先定位实际问题和涉及文件，只做与请求相关的最小改动，保持现有业务行为。
3. 按下面的场景读取对应参考，不要把所有参考都塞进上下文。
4. 修改后运行与项目匹配的真实构建和测试；不要把 Skill 指南当作架构门禁或验收证据。
5. 输出改动内容、真实验证结果和未验证项。

## 核心规则
- 只改有证据的问题：职责过多、封装泄漏、继承滥用、重复代码、过度设计。
- 优先组合而非继承，缩小可变性，隐藏内部状态，避免无必要的抽象。
- 不为未来需求预留复杂结构；先保留现有契约和行为。
- 典型异味：God Class、Feature Envy、Data Clumps、Shotgun Surgery、Refused Bequest。

## 按需参考
- SOLID/DRY/YAGNI：`references/121-java-object-oriented-design-principles.md`
- 封装/继承/多态：`references/121-java-object-oriented-design-oop-concepts.md`
- 代码异味：`references/121-java-object-oriented-design-code-smells.md`
- 类与接口边界：`references/121-java-object-oriented-design-classes-interfaces.md`
- 对象创建与依赖注入：`references/121-java-object-oriented-design-object-creation.md`
- 方法与 Optional：`references/121-java-object-oriented-design-methods.md`
- 枚举与注解：`references/121-java-object-oriented-design-enums-annotations.md`
- 异常契约：`references/121-java-object-oriented-design-exceptions.md`
