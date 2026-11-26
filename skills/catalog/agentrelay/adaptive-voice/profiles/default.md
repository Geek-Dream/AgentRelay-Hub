# Adaptive Voice 默认画像

这是空白模板，**当前没有记录用户画像**。

只有在用户明确提供内容、知道写入位置并同意保存后，才从 `assets/profile-template.md` 复制字段到这里。不要预填年龄、性别、职业、能力、性格或表达偏好。

`adaptive` 模式先读取显式指定 Agent 的 `profiles/agents/<agent-id>.md`，再用本文件补充未覆盖字段。两者都为空时按 normal 输出，并可询问一次是否建立画像。

共享模式 `voice.mode` 或独立模式 `scripts/voice_mode.json` 与画像分开保存；本文件不能覆盖当前模式。
