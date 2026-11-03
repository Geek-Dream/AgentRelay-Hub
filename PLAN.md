# PLAN — 清理、隐私审计与 GitHub 提交（2026-11-03）

- 仅处理 AgentRelay-Hub 当前改动；保留测试源码，不混入 all-agent-config。
- 删除可再生/忽略的 runtime 截图与事件、日志、预览图、Python/pytest 缓存、.DS_Store、未跟踪历史对话残留；不删已跟踪源码或配置。
- 对待提交文件检查密钥、Cookie、会话、个人路径和敏感运行数据；运行测试、真实页面检查和静态检查。
- 用户已明确授权 commit + push；在 main 上直接提交并推送 origin/main，随后核对远端 commit 与 CI 状态。

## 状态
- [x] 已删除忽略的 runtime/logs/预览图、Python/pytest 缓存、.DS_Store 和历史对话残留；保留测试源码。最终截图仅在 /tmp。
- [x] 隐私审计：无真实密钥/Cookie/会话；旧测试中的个人路径已替换，私钥字样仅安全测试的虚构负面夹具。
- [x] 提交前：285 项通过（1 项跳过），真实 Chromium 通过，静态检查通过，本地与 origin/main 同步。
- [ ] 提交并推送后核对 GitHub。

- [x] 首次推送 9cef840 后远端 CI 在 DeepSeek 路由测试失败；干净环境复现根因是测试读取本机私有配置。已用固定 hybrid mock 隔离测试，生产逻辑不变；截图 artifact 仅在有结果时上传。

---

# PLAN — 配置中心视觉优化（2026-11-03）

## 范围
只优化 AgentRelay-Hub `scripts/config_web.py` 的布局、配色、卡片、表单和移动端显示；不改 Provider API、保存逻辑、安装流程或数据。

## 设计
- 深色/浅色共用一套更克制的工作台视觉：减少霓虹和扫描线，蓝绿主色、清晰边界、统一圆角与间距。
- 侧栏、页面标题、统计卡、Provider 信息与操作区建立明确层级；手机上统计卡三列紧凑显示，操作按钮自适应。
- 保留六个导航、主题切换、表单及全部原功能。

## 验证
- 真实 Chromium 六导航、双主题、桌面/移动检查与截图；补看浅色主题和各 Provider 表单。
- 全量 unittest、Python/Shell/JSON 静态检查、`git diff --check`。
- 同步已安装 Skill 的 `config_web.py`，再用安装后的真实页面生成器检查。

## 状态
- [x] 视觉优化完成：克制的工作台配色、统一卡片与按钮、紧凑统计卡、Provider 操作区和手机三列统计。
- [x] 真实 Chromium：六导航、双主题、Web 新增表单、桌面/移动检查通过；保存 4 张截图，无控制台错误或横向溢出。
- [x] 全量 285 项通过（1 项原有跳过）；py_compile 与 diff 检查通过。
- [x] 已同步安装版 Skill，并再次用安装版生成器通过相同浏览器检查。
- 证据：`runtime/page-verification-ui-v2/`、`runtime/page-verification-ui-installed/`。

---

# PLAN — 页面改动真实验证门禁（2026-11-03）

## 已核实的问题
- CI 只有 Python/shell/JSON 静态检查，没有配置页浏览器回归。
- Playwright 用于 Provider 自动化及一条回答提取测试，不是页面交付门禁。
- Stop 只按助手完成措辞结算问题，没有页面修改与验证证据关联。

## 目标
修改 HTML/CSS/JS/页面模板后，必须以真实 Chromium 打开页面、检查控制台、切换主要导航/主题、截图，并留下与当前文件版本绑定的验证记录。无记录、检查失败或文件后来又被修改时，禁止以“页面已完成/已验证”收尾。

## 方案
1. `scripts/page_verification.py`：Hook 侧记录页面文件路径和 SHA-256，核对当前版本与验证证据；只在页面改动且助手声称完成时触发，兼容 Stop 重试。
2. `scripts/verify_page.py`：真实 Chromium 加载本地配置页，捕获 console/pageerror/requestfailed，逐个点击导航、切换主题、检查桌面及手机布局并截图；原子写 JSON 证据。默认不访问外部 Provider。
3. Hook 接入 PostToolUse 与 Stop，不修改既有计时/归档结构；验证失败不伪造完成状态。
4. 浏览器专项测试 + 无浏览器单元测试；完整 unittest、静态检查和真实截图检查。

## 边界与回滚
- 非页面任务不触发；没有 Playwright/Chromium 时明确失败并列出未验证项，不自动放行。
- 只写 AgentRelay 自己的验证状态与截图，不修改用户配置、不登录网站。
- 移除 Hook 两处接线即可恢复原行为，验证器可独立运行。

## 状态
- [x] 真实 Chromium 配置页检查器：六导航、明暗主题、桌面/移动布局、console/pageerror/requestfailed、截图与 JSON 证据。
- [x] Hook 文件指纹、Bash 工作区增量、Stop 阻断与下一提示补验证提醒；非页面任务不触发。
- [x] 安装清单与已安装 Skill 同步；旧 tracker/SKILL 已由安装器备份。
- [x] CI 单元测试、Chromium 页面检查与截图 artifact。
- [x] 验证：285 项通过（1 项原有跳过）；源码及已安装检查器均真实 Chromium 通过；端到端“阻断→验证→放行”通过；py_compile、shell/JSON、diff 检查通过。
- 未验证：GitHub 远端 CI 尚未运行；其他项目页面需在会话模式显式传 `--url`，不能只凭配置页通过。
