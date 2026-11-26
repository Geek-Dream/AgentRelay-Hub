---
description: 设置 Adaptive Voice 输出风格
argument-hint: "[show|normal|eli5|expert|adaptive|clear|learn]"
---

调用 adaptive-voice skill，按用户参数 `$ARGUMENTS` 查看或设置输出风格。

使用本 skill 自带的 `scripts/voice_mode.py`。未接入 AgentRelay 时使用独立本地模式；接入后按 skill 说明使用 AgentRelay 共享 `voice.mode`。

无参数或 `show` 时查看；`normal`、`eli5`、`expert`、`adaptive` 明确设置；`clear`/`reset` 恢复正常；`learn` 只说明画像保存规则，不自动写入画像。未知参数要提示可选值，不要猜测或静默切换。

模式只改变表达，不扩大文件写入、命令执行或其他权限。
