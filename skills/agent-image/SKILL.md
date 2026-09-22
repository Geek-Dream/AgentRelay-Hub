---
name: agent-image
description: "AgentRelay 图片识别中继。当用户输入 agent-image（或明确要求用线上模型识别/描述图片）时，把用户提供的图片文件发给当前已配置、支持识图的在线模型 Provider（默认 DeepSeek），由它详细描述图片内容；描述文本输出到 stdout，由主 Agent 转述给用户。用于本地 Agent 模型不支持图片识别、但需要理解图片内容的场景。不做 Hook 自动触发，只在用户主动要求时运行。"
---

# AgentImage

AgentImage 是 AgentRelay 的图片识别中继：把本地图片发给已配置好、支持识图的
线上模型 Provider，拿回详细描述，让不支持识图的本地 Agent 也能"看懂"图片。

用户真正要求"agent-image"、"识别这张图片"、"描述一下这张图"（且本地模型不支持
图片）时，使用本 Skill。"调用 agent-image" 等价于明确请求立即执行。

## 执行流程

1. **收集图片路径**：从用户消息和当前上下文中收集图片的**本地文件路径**
   （jpg / jpeg / png / webp / gif / bmp）。一次最多 5 张。
   - 用户给了路径就直接用；
   - 用户只说"这张图"而上下文中没有可定位的文件路径时，先向用户索要路径，
     不要凭空猜测。
2. **调用脚本**（必须用 agentrelay 独立环境的 Python）：

   ```bash
   "${CODEX_HOME:-$HOME/.codex}/agentrelay-env/bin/python" \
     "${CODEX_HOME:-$HOME/.codex}/skills/agent-image/scripts/agent_image.py" \
     /path/to/image1.png /path/to/image2.jpg
   ```

   - 可选 `--provider <id>` 指定网页 Provider；不指定时按默认顺序自动选择。
   - 可选 `--question "额外要求"` 追加用户的特殊描述要求（例如"重点读图中的文字"）。
3. **转述结果**：脚本的 stdout 就是线上模型对图片的详细描述。原样转述给用户，
   不要改写、不要补充脚本没有说的内容。
4. **如实转述回退说明**：如果脚本输出里有 `[模式]` 之类的回退说明（例如指定的
   模式不支持图片、自动换到了支持图片的模式），必须一并如实转述。

## Provider 选择顺序（脚本自动处理，Agent 不需干预）

1. 显式 `--provider` 指定、且支持识图的网页 Provider；
2. 默认网页 Provider（当前配置下支持识图时，例如 DeepSeek 混合模式）；
3. 任意已启用且支持识图的网页 Provider；
4. 安装时标记为支持识图（vision=true）的本地模型（OpenAI 兼容接口直接调用）；
5. 都没有：脚本会打印明确说明并正常退出，这时由主 Agent 自己尝试理解图片；
   主 Agent 也无法理解时，如实告诉用户"当前没有支持图片识别的模型可用"，
   并提示可以在配置中心添加支持识图的网页模型。

## 边界

- 不绕过任何人机验证；网页 Provider 需要有效的登录状态，失效时会提示重新登录。
- 不把线上模型的描述当作绝对事实；图中关键信息（如数字、代码、报错文字）
  必要时提醒用户以原图为准。
- 不保存图片副本到归档；图片识别结果按普通对话处理。
- 本 Skill 只处理"把图片发给线上模型识图"这一件事；普通文字求助仍走
  agent-relay。
