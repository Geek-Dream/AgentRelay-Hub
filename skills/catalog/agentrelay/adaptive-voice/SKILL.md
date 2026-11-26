---
name: adaptive-voice
description: "切换 Agent 的输出风格：normal、eli5、expert、adaptive。用户要求大白话、5岁能懂、专业术语、个性化表达，或使用 /adaptive-voice、@adaptive-voice、$adaptive-voice、查询或设置当前风格时使用。"
---

# Adaptive Voice

让 Agent 的**表达方式**可切换、可回读。风格只改变表达，不改变事实、结论、技术判断、安全审批或用户本条消息的明确要求。

## 独立使用（不需要 AgentRelay）

单独下载整个目录，复制到当前宿主的 skills 目录。不要只复制 `SKILL.md`，否则独立模式和命令文件会缺失。

```bash
python /path/to/adaptive-voice/scripts/voice_mode.py show
python /path/to/adaptive-voice/scripts/voice_mode.py set eli5
python /path/to/adaptive-voice/scripts/voice_mode.py set expert
python /path/to/adaptive-voice/scripts/voice_mode.py clear
```

独立模式保存在 `scripts/voice_mode.json`；可通过 `ADAPTIVE_VOICE_HOME` 指定另一个本地配置目录。它不使用 AgentRelay 配置，不需要安装 AgentRelay、Hook 或加密依赖。

宿主支持命令文件时，将 `commands/adaptive-voice.md` 安装到对应命令目录：

- Claude：`~/.claude/commands/adaptive-voice.md`，输入 `/adaptive-voice eli5`；也可明确 `@adaptive-voice` 调用本 skill。
- Codex：`~/.codex/prompts/adaptive-voice.md`，输入 `/adaptive-voice eli5`；也可明确 `$adaptive-voice` 调用本 skill。
- Pi：把本 skill 放到 `~/.pi/agent/skills/adaptive-voice`，用宿主支持的 skill 调用方式，或直接告诉 Agent 设置风格。

也可以直接说“用大白话并举例”“专业点”“恢复正常输出”。只有明确切换时才修改持久模式；普通写作任务不改设置。

## 与 AgentRelay 一起使用

AgentRelay 的安装清单默认勾选本 skill，可以取消。取消时不安装本 skill，不影响其他能力。install 向导和本地 Web 的「输出风格」都能设置模式。

接入后优先使用 AgentRelay 的共享 `voice.mode`，不再读写独立 JSON。以下入口使用同一份配置：

1. install 向导或 install Web →「输出风格」。
2. 对话中明确切换风格。
3. `/adaptive-voice` 及子命令。
4. 调用本 skill 或 AgentRelay 的共享 CLI。

```bash
python "$CODEX_HOME/skills/agent-relay/scripts/voice_mode.py" show
python "$CODEX_HOME/skills/agent-relay/scripts/voice_mode.py" set eli5
python "$CODEX_HOME/skills/agent-relay/scripts/voice_mode.py" clear
```

`CODEX_HOME` 未设置时使用 `~/.codex`。保存后应从真实配置回读并显示 `normal / eli5 / expert / adaptive`。Hook 在后续对话注入简短模式说明；宿主可能需要重启或重新加载。

| 命令 | 行为 |
|---|---|
| `/adaptive-voice` | 显示当前模式及四种模式说明 |
| `/adaptive-voice normal` | 恢复正常输出 |
| `/adaptive-voice eli5` | 大白话、短句、具体例子，术语当场解释 |
| `/adaptive-voice expert` | 准确术语、技术细节、可验证依据 |
| `/adaptive-voice adaptive` | 按已明确同意保存的画像适应表达 |
| `/adaptive-voice clear` / `reset` | 同 normal |
| `/adaptive-voice learn` | 说明画像保存规则；不自行收集或写入画像 |

## 四种模式

| 模式 | 应该怎么做 |
|---|---|
| `normal` | 不额外修改风格，保持 Agent 正常回答 |
| `eli5` | 先结论，再用大白话和生活例子详细解释；术语立即翻译 |
| `expert` | 术语准确、信息密度高，给出技术细节和证据，不解释基础概念 |
| `adaptive` | 使用画像明确记录的表达偏好；未记录维度按 normal，不猜测性格或能力 |

详细示例见 `references/modes.md`。expert 的 Agent 符号协商见 `references/agent-protocol.md`：双方明确确认前不得使用，短码不能代替完整结论，对方不懂就展开全称。

## 画像不是模式开关

`/adaptive-voice learn`、切换到 adaptive、或一次性的“这次短一点”，**都不是保存画像的同意**。

只有用户明确提供内容，并同意写入位置后，才可写：

1. `profiles/agents/<agent-id>.md`：调用方或用户明确指定的 Agent 专属覆盖。
2. `profiles/default.md`：没有专属画像时的默认画像。

没有画像时只询问一次是否建立，不反复追问。画像只记录明确提供的表达偏好；年龄、性别、工作等个人信息仅在用户主动提供且同意保存时记录，**不得据此推断能力或性格**。具体流程见 `references/profile-memory.md`。

## 边界

- 不把“写一个专业术语文档”“为什么不能用大白话？”误判为持久切换。
- 不凭口吻猜测年龄、性别、职业、性格或技术水平。
- 不保存密码、token、密钥或其他认证信息。
- 不因风格切换批准文件写入、危险命令或外部操作。
- 升级时不得用空白模板覆盖已填写画像。
- 当前消息比持久模式更具体时，以当前消息为准，但不自动改写持久模式。
