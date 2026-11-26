# Adaptive Voice

单独下载即可切换 `normal`、`eli5`、`expert`、`adaptive` 四种输出风格，不依赖 AgentRelay。接入 AgentRelay 后，install、Web、自然语言、命令和 skill 可共用 `voice.mode`。

## 单独安装

复制整个目录到宿主的 skills 目录。支持命令文件时，再复制 `commands/adaptive-voice.md`：

- Claude：`~/.claude/commands/adaptive-voice.md`，使用 `/adaptive-voice eli5`；也可明确 `@adaptive-voice` 调用 skill。
- Codex：`~/.codex/prompts/adaptive-voice.md`，使用 `/adaptive-voice eli5`；也可明确 `$adaptive-voice` 调用 skill。
- Pi：放到 `~/.pi/agent/skills/adaptive-voice`，按宿主的 skill 调用方式使用。

也可以直接告诉 Agent：“用大白话并举例”“专业点”“恢复正常输出”。

```bash
python /path/to/adaptive-voice/scripts/voice_mode.py show
python /path/to/adaptive-voice/scripts/voice_mode.py set eli5
python /path/to/adaptive-voice/scripts/voice_mode.py set expert
python /path/to/adaptive-voice/scripts/voice_mode.py set adaptive
python /path/to/adaptive-voice/scripts/voice_mode.py clear
```

独立模式存于 skill 的 `scripts/voice_mode.json`。如需其他位置，设置 `ADAPTIVE_VOICE_HOME` 环境变量。独立版不生成 AgentRelay 加密配置，不声称拥有 AgentRelay 的 Web 或 Hook。

## 随 AgentRelay 安装

Adaptive Voice 在安装清单中**默认勾选，可取消**。它作为内置 skill 安装，不需要手动指定本机源路径，也不会自动下载另一份。

install 向导可以设置默认风格；安装后的本地 Web →「输出风格」可以再次修改。接入后以下入口读写同一份 `voice.mode`：

| 入口 | 用法 |
|---|---|
| install / Web | 选择 normal、eli5、expert 或 adaptive 并保存 |
| 直接告诉 Agent | “用大白话并举例”“专业点”“恢复正常输出” |
| 斜杠命令 | `/adaptive-voice eli5` |
| skill | 按宿主使用 `@adaptive-voice`、`$adaptive-voice` 或对应调用方式 |
| CLI | `python "$CODEX_HOME/skills/agent-relay/scripts/voice_mode.py" set eli5` |

保存后回读真实配置；Hook 对后续对话生效，宿主可能需要重启或重新加载。安装只复制公开 skill 和空画像模板，不复制 `.git`，不覆盖已有画像。

## 四种模式

- **normal**：不额外调整表达。
- **eli5**：短句、大白话、生活例子，术语当场解释。
- **expert**：准确术语、技术细节、可验证依据。
- **adaptive**：按用户明确同意保存的表达偏好回答，未知维度按 normal，不猜测能力或性格。

普通写作任务（例如“用专业术语写一份接口文档”）不会被当成永久切换。当前消息的明确要求优先。

## 画像与隐私

画像和模式是两件不同的事。`/adaptive-voice learn` 只展示规则，不自动收集或保存信息。只有用户明确提供内容、知道写入位置并同意后，才写 `profiles/default.md` 或明确指定的 `profiles/agents/<agent-id>.md`。

不保存密码、token、密钥或其他认证信息；不根据年龄、性别、职业推断能力或性格。画像是普通本地 Markdown，升级时不被空白模板覆盖。

详见 `SKILL.md`、`references/modes.md` 和 `references/profile-memory.md`。

## 许可

MIT，见 `LICENSE`。
