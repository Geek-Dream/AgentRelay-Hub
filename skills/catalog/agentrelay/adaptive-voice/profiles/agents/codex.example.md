# Agent 专属画像示例（不会自动加载）

只有用户或调用方明确指定 `agent-id` 后，才读取 `profiles/agents/<agent-id>.md`。本文件的 `.example.md` 后缀不会生效，也不代表 Codex 默认使用 expert。

复制 `assets/profile-template.md` 为真实的 `<agent-id>.md`，只填写用户明确提供并同意保存的字段。

不要添加 `default_mode`：当前模式由独立本地配置或 AgentRelay 的共享 `voice.mode` 管理。
