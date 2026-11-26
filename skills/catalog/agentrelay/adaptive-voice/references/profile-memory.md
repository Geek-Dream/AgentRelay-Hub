# 用户画像与记忆

`adaptive` 使用画像；模式本身保存在独立 `scripts/voice_mode.json` 或 AgentRelay 的 `voice.mode`。两者不能混为一谈。

## 保存位置

| 层级 | 路径（相对 skill 根） | 作用 |
|---|---|---|
| Agent 专属 | `profiles/agents/<agent-id>.md` | 用户或调用方显式指定 Agent 的覆盖 |
| Skill 默认 | `profiles/default.md` | 无专属画像时的默认表达偏好 |

读取专属画像，再读取默认画像补充未覆盖字段。`agent-id` 未明确指定时只读默认画像。`*.example.md` 不生效。

安装或升级不覆盖已有画像。画像是普通本地 Markdown，不包含加密保护，因此不能用来保存认证信息或不必要的高度敏感个人信息。

## 保存前必须完成的两步

1. 用户提供具体内容，并说明希望长期使用，例如“以后都用中文、先给结论、代码例子简短”。
2. Agent 明确说明要写的字段、目标文件和生效方式，用户再明确同意。

“切换到 adaptive”“`/adaptive-voice learn`”“这次短一点”“写一篇五岁能懂的文章”都不等于同意保存。不要自行推断、补全或收集年龄、性别、工作等信息。

用户主动提供年龄、性别、工作且明确同意保存时，可以按请求记录；这些事实只用于用户明确指定的表达适配，**不得推断能力、性格、智力或偏好**。

## 可记录字段

只填用户明确提供的内容，其余删除或留空。模板见 `assets/profile-template.md`。

```markdown
- language: zh
- length: short
- structure: conclusion-first
- term_density: medium
- example_style: code
- tone: direct
- focus: [correctness]

## Notes
- 用户明确说过：不要客套开头。
```

不在画像中另建 `default_mode` 或 `symbol_protocol` 作为第二套开关。

## 更新、查看和删除

- 更新：说明变化字段，再次取得同意后只改这些字段，保留其他内容。
- 查看：显示真实文件内容，不虚构已保存信息。
- 删除：用户明确指定字段或文件后执行；删除是需要确认的文件操作。
- 无画像：正常输出，并可询问一次是否建立；不要反复追问。

画像只负责表达，不替代项目事实、代码历史或宿主 Agent 的长期记忆。
