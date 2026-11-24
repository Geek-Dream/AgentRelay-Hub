---
name: agent-image
description: "AgentRelay 主动图片识别次辅助。仅当用户当次显式调用本 skill 时运行——@agent-image（或 Codex 的 $agent-image 提及、以及明确说“让别的模型/在线模型看这张图”）——有本地视觉模型就用本地，没有则使用声明支持视觉的 API；网页模型只有开启 Beta 且明确声明视觉后才作最后辅助。用户问“这是什么”时转述描述；用户让 Agent 按图修改时消化描述后继续任务。当前模型能看图时不要绕道。Hook、页面验收和普通识图措辞都不能被动触发。"
---

# AgentImage

AgentImage 是 AgentRelay 的**主动识图次辅助**：有已启用且声明 `vision=true` 的本地视觉
Provider 时使用本地；没有本地视觉时使用声明支持视觉的 API Provider。只有用户开启
“Beta 网页测试协助”，且网页 Provider 已按会话明确声明图片能力，网页才排最后辅助。
核心价值是让无视觉的主 Agent 在用户明确要求时借其他模型理解图片，而不是接管所有图片。

**仅当用户显式调用本 skill 时运行**——`@agent-image`（Codex 中用 `$agent-image`
提及，或直接说出“让别的模型/在线模型看这张图”这类明确指令）。"调用 agent-image"
等价于明确请求立即执行。

脚本必须携带 `--user-invoked`，这个标记只能表示**当前这条用户消息**确实显式调用了
AgentImage。缺少标记时脚本在读取配置和发送图片前直接拒绝。Hook、页面验收、模型自己的
“需要识图”判断和普通“看看这张图”都不能自行补这个标记。

**不要主动绕道的判断**：如果当前 Agent 模型本身支持图片识别，用户的图直接自己看
即可，不要走中继——多一次转发既浪费流量，二手描述还可能丢失细节、误导当前模型。
只有当前模型确实不支持识图（例如本地纯文本模型、Commander 的本地 9B 辅助模型），
且用户明确要求借其他视觉模型识图时，才使用本 Skill。

## 两种用法（先看用户意图，再决定输出方式）

1. **转述模式**：用户问的是"这张图是什么/写了什么"——执行后把脚本 stdout 的
   描述**原样转述**给用户，不要改写、不要补充脚本没有说的内容。
2. **自用模式**：用户的意思是"你自己看懂这张图，然后继续干活"（例如"按图片
   上的报错改 bug""按这个界面截图修样式"）——**不要把描述全文转述给用户**，
   自己消化描述内容，直接继续执行任务，在回复里简要说明"已通过其他视觉模型理解
   图片"即可。

判断不了的按转述模式处理，并问一句用户想要哪种。

## 执行流程

1. **收集图片路径**：从用户消息和当前上下文中收集图片的**本地文件路径**
   （jpg / jpeg / png / webp / gif / bmp）。一次最多 5 张。
   - 用户给了路径就直接用；
   - 用户只说"这张图"而上下文中没有可定位的文件路径时，向用户索要路径，
     不要凭空猜测、不要搜索无关目录。
   - 用户没有调用本 Skill、只是普通对话时：按没有图片的方式照常处理即可。
     如果本地模型不支持图片，它会自己回复"无法看见图片"之类的话，**这句话
     原路转述给用户**，用户自然知道当前模型不支持识图。
2. **调用脚本**（必须用 agentrelay 独立环境的 Python）：

   macOS/Linux：

   ```bash
   CODEX_ROOT="${CODEX_HOME:-$HOME/.codex}"
   PYTHON="$CODEX_ROOT/agentrelay-env/bin/python"
   IMAGE_AGENT="$CODEX_ROOT/skills/agent-image/scripts/agent_image.py"
   "$PYTHON" "$IMAGE_AGENT" --user-invoked \
     /path/to/image1.png /path/to/image2.jpg
   ```

   Windows PowerShell：

   ```powershell
   $CODEX_ROOT = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
   $PYTHON = Join-Path $CODEX_ROOT "agentrelay-env\Scripts\python.exe"
   $IMAGE_AGENT = Join-Path $CODEX_ROOT "skills\agent-image\scripts\agent_image.py"
   & $PYTHON $IMAGE_AGENT --user-invoked C:\path\to\image1.png C:\path\to\image2.jpg
   ```

   - 可选 `--provider <id>` 优先匹配某个本地、API 或网页 Provider；有本地视觉时仍不
     使用 API 主路由，网页始终排在本地/API 之后且必须显式开启 Beta。
   - 可选 `--question "额外要求"` 追加用户的特殊描述要求（例如"重点读图中的文字"）。
   - 可选 `--no-cache` 强制重新识别（默认同一张图 7 天内命中缓存直接返回）。
3. **处理结果**：
   - 正常输出 = 当前选中模型对图片的详细描述，按上述两种模式处理。
   - 脚本中途的 `[Provider] xxx 失败（原因），切换下一个候选` 说明发生了自动
     切换，**如实转述**切换信息，让用户知道哪个模型出了问题。
   - 输出以 `【AgentImage 注】` 开头 = 所有候选模型都失败了，里面列出了每个
     Provider 的失败原因和建议。**原样转述**，引导用户检查视觉开关、接口或网页登录。

## 识图后的追问闭环

识图成功后，如果用户就**同一张图**继续追问（"图里那段代码什么意思""这个报错
怎么修"），直接用 agent-relay 正常追问即可：网页 Provider 的会话是绑定的，
上下文连续，不需要重新调用 agent-image，也不需要重新满足权重门槛。

## Provider 选择顺序（脚本自动处理，Agent 不需干预）

1. 有本地视觉时，仅尝试已启用且 `vision=true` 的本地 Provider，API 不参与这一分支；
2. 没有本地视觉时，仅尝试已启用且 `vision=true` 的 API Provider，复用现有协议适配；
3. 主路由失败后，才尝试 Beta 已开启且明确声明图片能力的网页 Provider；
4. Beta 关闭、网页未配置或未声明视觉时不检查网页登录、不启动浏览器；
5. 全部失败：注入 `【AgentImage 注】` 说明并正常退出，由主 Agent 转述并引导
   用户重新配置。

## 边界

- 不绕过任何人机验证；网页 Provider 需要有效的登录状态，失效时会提示重新登录。
- 页面视觉验收也不能被动调用；只有用户当次明确要求用 AgentImage 审查该页面时，
  才可运行 `visual_review.py agent-image --user-invoked`。否则登记用户复查。
- 不把其他视觉模型的描述当作绝对事实；图中关键信息（如数字、代码、报错文字）
  必要时提醒用户以原图为准。
- 图片只在调用时发送给选定的模型，不做无关备份；识别结果缓存 7 天用于
  去重，过期自动清理。
- 本 Skill 只处理"把图片交给本地、API 或 Beta 网页中选定的视觉模型"这一件事；普通文字求助仍走
  agent-relay。
