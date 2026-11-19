---
name: 303-frameworks-spring-boot-validation
description: Use when you need to design, review, or improve validation in Spring Boot applications — including Bean Validation on request DTOs, @Valid/@Validated at API boundaries, constraint groups, custom constraints, @ConfigurationProperties validation, nested DTO validation, and consistent validation error handling. This should trigger for requests such as Add validation support in Spring Boot; Review Spring Boot validation rules; Improve request validation in Spring Boot REST APIs; Add custom Bean Validation constraints in Spring Boot; Validate configuration properties in Spring Boot. Part of Plinth Toolkit
license: Apache-2.0
metadata:
  author: Juan Antonio Breña Moral
  version: 0.19.0
---
# Spring Boot 输入校验

## 工作方式

1. 先确认项目能编译；编译失败时停止修改并报告。
2. 先定位实际问题和涉及文件，只做与请求相关的最小改动，保持现有业务行为。
3. 按下面的场景读取对应参考，不要把所有参考都塞进上下文。
4. 修改后运行与项目匹配的真实构建和测试；不要把 Skill 指南当作架构门禁或验收证据。
5. 输出改动内容、真实验证结果和未验证项。

## 核心规则
- 请求 DTO 用 Bean Validation，控制器边界使用 @Valid / @Validated。
- 创建/更新分组校验，嵌套 DTO 正确级联，配置属性也要验证。
- 校验失败统一返回 400 和一致错误结构，不泄露内部实现细节。

## 按需参考
- 完整校验参考：`references/303-frameworks-spring-boot-validation.md`
