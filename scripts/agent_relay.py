#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
AI 网站自动对话脚本
============================================================

【项目说明】

这是 AgentRelay 基于 Playwright 的 Provider 自动对话入口。

当前内置的首个 Provider 示例为：

    DeepSeek

脚本的核心目标：

    1. 使用已经保存的登录状态访问 DeepSeek
    2. 自动读取历史会话
    3. 根据模式自动寻找目标会话
    4. 自动发送用户问题
    5. 通过 DeepSeek 输入框按钮状态判断 AI 是否正在生成
    6. 等待 AI 回复完成
    7. 提取最新一轮 AI 回复

------------------------------------------------------------
【重要：不要随意修改的核心机制】
------------------------------------------------------------

AI 回复完成的判断机制非常重要。

禁止重新使用下面这种方式判断 AI 是否完成：

    page.inner_text("body")

    比较前后两次 body 文本是否相同

旧版本曾经使用：

    当前页面文本
        ↓
    等待 1 秒
        ↓
    再读取页面文本
        ↓
    如果没变化
        ↓
    认为 AI 回复完成

这种方式不可靠。

因为：

    页面文本暂时不变化
    ≠
    AI 已经生成完成

AI 可能仍然处于：

    思考
    生成
    网络传输
    DOM 更新

状态。

------------------------------------------------------------
【当前正确的 AI 状态判断】
------------------------------------------------------------

DeepSeek 输入框发送按钮存在：

    .ds-button__icon.ds-button__icon--last-child

按钮状态：

    ↑
    ↓
    SEND

表示：

    可以发送消息
    或者 AI 回复已经完成

AI 生成过程中：

    ■
    ↓
    GENERATING

表示：

    AI 正在生成回复

完整状态流程：

    ↑
    │
    │ 发送问题
    ▼
    ■
    │
    │ AI 正在生成
    │
    ▼
    ↑
    │
    │ AI 回复完成
    ▼
    提取回复

程序必须按照：

    ↑ → ■ → ↑

这个状态变化判断一次完整的 AI 回复。

------------------------------------------------------------
【DeepSeek SVG Path 特征】
------------------------------------------------------------

当前版本通过 SVG path 的 d 属性识别按钮状态。

发送按钮：

    d 以：

        M8.3125

    开头

停止生成按钮：

    d 以：

        M2 4.88

    开头

注意：

    不要轻易修改这两个特征。

如果未来 DeepSeek 修改了 SVG，
应该只修改：

    DeepSeekAdapter

而不是修改主程序。

------------------------------------------------------------
【网站适配架构】
------------------------------------------------------------

当前程序不是把所有 DeepSeek 逻辑直接写在 main() 中。

采用：

    SiteDetector
          │
          ▼
    SiteAdapter
          │
          ├── DeepSeekAdapter
          │
          ├── KimiAdapter       ← 以后增加
          │
          ├── ChatGPTAdapter    ← 以后增加
          │
          └── ClaudeAdapter     ← 以后增加

因此：

    主程序
        ↓
    判断当前网站
        ↓
    找到对应 Adapter
        ↓
    执行对应网站逻辑

以后新增网站时：

    不要把：

        if deepseek:
        elif kimi:
        elif chatgpt:

    全部堆进 main()。

应该：

    新建对应 Adapter

例如：

    class KimiAdapter(SiteAdapter):
        ...

然后在：

    SiteDetector

中注册。

------------------------------------------------------------
【当前支持的网站】
------------------------------------------------------------

当前：

    DeepSeek

未来可以扩展：

    Kimi
    ChatGPT
    Claude
    Gemini
    其他 AI 网站

------------------------------------------------------------
【使用方法】
------------------------------------------------------------

一、第一次使用

首先确保已经完成 DeepSeek 登录。

运行登录脚本：

    python3 agent_relay_login.py

登录成功以后，会生成：

    agent_relay_login_state.json

默认位置：

    Skill 根目录下的 agent_relay_login_state.json

也可以通过 AGENT_RELAY_LOGIN_STATE 指定其他位置。

这个文件保存 Playwright 的登录状态。

通常只需要第一次登录。

------------------------------------------------------------
二、命令行模式
------------------------------------------------------------

【模式 1：极速 / 图片】

运行：

    python3 agent_relay.py "你的问题" -m 1

例如：

    python3 agent_relay.py "帮我分析一下这个 Java 项目" -m 1

含义：

    -m 1

表示：

    极速 / 图片

程序会自动寻找：

    极速
    图片

相关会话。

------------------------------------------------------------
【模式 2：专家 / 思考】

运行：

    python3 agent_relay.py "你的问题" -m 2

例如：

    python3 agent_relay.py "请深入分析这个系统架构的问题" -m 2

含义：

    -m 2

表示：

    专家 / 思考

程序会自动寻找：

    专家
    思考

相关会话。

------------------------------------------------------------
三、交互模式
------------------------------------------------------------

如果不传问题：

    python3 agent_relay.py

程序会提示：

    请输入你要问的问题:

输入问题后：

    选择模式
    [1=极速/图片(默认), 2=专家/思考]:

输入：

    1

使用：

    极速 / 图片

输入：

    2

使用：

    专家 / 思考

------------------------------------------------------------
【命令行参数】
------------------------------------------------------------

问题：

    python3 agent_relay.py "你的问题"

模式：

    -m 1

或者：

    --mode 1

专家模式：

    -m 2

或者：

    --mode 2

例如：

    python3 agent_relay.py \
        "帮我优化这个 Java 代码" \
        --mode 2

------------------------------------------------------------
【完整使用示例】
------------------------------------------------------------

第一次登录：

    python3 agent_relay_login.py

然后：

    python3 agent_relay.py \
        "帮我分析 Spring Boot 项目的性能问题" \
        -m 2

程序流程：

    检查登录状态
        ↓
    启动 Chromium
        ↓
    打开 DeepSeek
        ↓
    检测当前网站
        ↓
    识别为 DeepSeek
        ↓
    获取历史会话
        ↓
    根据模式寻找目标会话
        ↓
    打开目标会话
        ↓
    找到 textarea
        ↓
    输入问题
        ↓
    Enter
        ↓
    等待 ↑ → ■
        ↓
    AI 开始生成
        ↓
    等待 ■ → ↑
        ↓
    二次确认 ↑
        ↓
    等待 DOM 最终更新
        ↓
    AI 回复完成
        ↓
    从后往前寻找最新问题
        ↓
    使用容错规则匹配问题
        ↓
    提取最新 AI 回复
        ↓
    输出结果
        ↓
    关闭浏览器

------------------------------------------------------------
【登录状态注意事项】
------------------------------------------------------------

当前登录状态文件：

    agent_relay_login_state.json

只是 Playwright 保存的浏览器登录状态。

如果出现：

    登录失效
    找不到会话
    页面显示登录页面
    找不到 textarea
    页面结构异常

首先重新运行：

    python3 agent_relay_login.py

不要第一时间修改主脚本。

------------------------------------------------------------
【浏览器注意事项】
------------------------------------------------------------

默认使用 Playwright 管理的浏览器。

如需指定系统浏览器，可通过环境变量：

    AGENT_RELAY_BROWSER_PATH

------------------------------------------------------------
【超时机制】
------------------------------------------------------------

AI 最大等待时间：

    300 秒

也就是：

    5 分钟

AI 正常生成：

    ↑ → ■ → ↑

程序正常结束。

如果超过：

    300 秒

仍然没有恢复到：

    ↑

程序会：

    停止等待
        ↓
    尝试获取当前已经生成的内容
        ↓
    输出当前结果

不要轻易把：

    300

修改成：

    30
    60

因为深度思考任务可能超过一分钟。

------------------------------------------------------------
【会话查找机制】
------------------------------------------------------------

当前根据模式寻找：

模式 1：

    极速
    图片

模式 2：

    专家
    思考

搜索顺序：

    今天
        ↓
    7天内
        ↓
    30天内
        ↓
    其他日期

如果 DeepSeek 会话名称发生变化：

    例如：

    极速模式
    专业思考
    深度思考

那么需要修改：

    DeepSeekAdapter.find_target_session()

中的关键词。

------------------------------------------------------------
【最新问题提取机制】
------------------------------------------------------------

这是非常重要的部分。

发送问题以后，DeepSeek 页面中的问题文本不一定和：

    question

变量完全一致。

例如：

    question：

        帮我分析这个 Java 项目

页面 DOM 可能出现：

        帮我分析这个 Java 项目

或者由于 DOM 格式：

        "帮我分析这个 Java 项目 "

甚至可能存在：

    多余空格
    换行
    不可见字符
    连续空白字符

因此不能只使用：

    lines[index] == question

当前采用多级容错匹配：

    第一层：
        完全匹配

    第二层：
        strip() 后匹配

    第三层：
        统一空白字符后匹配

    第四层：
        安全的包含匹配

同时：

    必须从页面最后开始寻找问题。

也就是：

    页面末尾
        ↓
    向前寻找
        ↓
    找到最近一次问题
        ↓
    提取后面的 AI 回复

这样可以避免：

    历史对话中存在完全相同的问题

导致程序错误获取旧答案。

------------------------------------------------------------
【为什么不能简单恢复老版本】
------------------------------------------------------------

老版本：

    body 文本稳定
        ↓
    认为 AI 完成

虽然当时可以工作，但这个判断本身不可靠。

新版：

    ↑ → ■ → ↑

才是真正用于判断：

    AI 是否已经开始生成
    AI 是否已经停止生成

因此：

    回复状态检测

和：

    回复内容提取

是两个完全不同的问题。

当前正确架构：

    状态检测
        ↓
    SVG Button
        ↓
    ↑ → ■ → ↑

    内容提取
        ↓
    页面文本
        ↓
    容错匹配

不要因为内容提取出现问题，
就重新把：

    body 文本稳定性

用于判断 AI 是否完成。

------------------------------------------------------------
【当前仍然使用 body 文本的地方】
------------------------------------------------------------

注意：

虽然 AI 回复完成检测已经不再使用：

    body.inner_text()

但是以下两个功能暂时仍然使用页面文本：

    1. 获取聊天列表

    2. 提取最新 AI 回复

也就是说：

    AI 状态检测：
        DOM / SVG
        ✅

    会话列表：
        body 文本
        ⚠️

    AI 回复提取：
        body 文本
        ⚠️

这是当前版本后续最值得继续优化的地方。

------------------------------------------------------------
【后续推荐优化方向】
------------------------------------------------------------

第一优先级：

    将聊天列表从：

        body.inner_text()

    改成：

        DeepSeek 真实侧边栏 DOM

这样可以精确获得：

    会话名称
    日期
    会话链接

而不是解析整个网页文本。

------------------------------------------------------------

第二优先级：

    将 AI 回复提取从：

        body.inner_text()

    改成：

        Assistant Message DOM

直接定位 AI 消息。

这样可以避免：

    页面按钮
    菜单
    状态文字
    页面其他内容

混入 AI 回复。

------------------------------------------------------------

第三优先级：

    登录状态真正验证

当前：

    check_login()

主要检查：

    agent_relay_login_state.json

是否存在有效的 cookies / origins。

未来可以进一步做到：

    打开 DeepSeek
        ↓
    检查是否真的登录
        ↓
    如果登录失效
        ↓
    自动提示重新登录

------------------------------------------------------------

第四优先级：

    增加失败截图

如果出现：

    找不到 textarea
    找不到会话
    AI 状态超时
    页面结构发生变化

自动保存：

    screenshot.png

这样调试 Playwright 会方便很多。

------------------------------------------------------------
【非常重要：修改代码时遵守以下原则】
------------------------------------------------------------

如果未来让 AI 修改这个脚本：

1. 不要删除 SiteAdapter 架构。

2. 不要把 DeepSeek 专属代码重新塞进 main()。

3. 不要使用 body 文本是否变化判断 AI 回复完成。

4. 不要删除：

       ↑ → ■ → ↑

   状态检测。

5. 不要随意修改：

       .ds-button__icon
       .ds-button__icon--last-child

6. 不要随意修改：

       M8.3125

   和：

       M2 4.88

7. 不要把 300 秒随意降低。

8. 修改 DeepSeek DOM 时，
   优先只修改：

       DeepSeekAdapter

9. 增加其他网站时，
   新建对应 Adapter。

10. 如果 DeepSeek 页面结构发生变化，
    优先检查真实 DOM，
    不要凭猜测修改 Selector。

11. 如果只是回复提取失败，
    不要修改 AI 状态检测机制。

12. 如果问题匹配失败，
    优先修改：

        extract_latest_answer()

    不要重新引入：

        body 文本稳定检测。

13. `extract_latest_answer()` 必须优先从后往前寻找问题，
    防止历史重复问题导致获取旧答案。

14. `↑ → ■ → ↑` 是“生成状态判断”。

    `extract_latest_answer()` 是“内容提取”。

    两者必须保持职责分离。

------------------------------------------------------------
【核心原则】
------------------------------------------------------------

这个项目不是：

    “写死一个 DeepSeek 自动化脚本”

而是：

    “一个可以不断增加 AI 网站适配器的自动化框架”。

当前：

    SiteDetector
          ↓
    DeepSeekAdapter
          ↓
    DeepSeek

以后：

    SiteDetector
          │
          ├── DeepSeekAdapter
          ├── KimiAdapter
          ├── ChatGPTAdapter
          ├── ClaudeAdapter
          └── 其他 Adapter

因此后续维护时：

    网站通用逻辑
        ↓
    放在基础层

    DeepSeek 专属逻辑
        ↓
    放在 DeepSeekAdapter

    Kimi 专属逻辑
        ↓
    放在 KimiAdapter

不要混在一起。

============================================================
【本版本特别修复】
============================================================

本版本针对之前“DeepSeek 已经回答，但是脚本没有获取到回复”
的问题进行了专门修复。

修复内容：

    1. 保留 ↑ → ■ → ↑ 状态机

    2. AI 从 ■ 恢复到 ↑ 后，
       再等待 1 秒，确保最后一次 DOM 更新完成

    3. 问题匹配不再只依赖：
           line == question

    4. 增加：
           strip()
           空白规范化
           安全包含匹配

    5. 仍然从后往前搜索问题

    6. 超时后仍然尝试提取已经生成的内容

注意：

    这并不意味着以后可以随意使用 body 文本判断生成完成。

    body 文本只负责：
        内容读取

    SVG 状态只负责：
        生成状态判断

============================================================
【脚本开始】
============================================================
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional

# ============================================================
# 全局配置
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
WORKSPACE = SKILL_ROOT
CODEX_HOME = Path(
    os.environ.get(
        "CODEX_HOME",
        str(Path.home() / ".codex"),
    )
).expanduser()

DEFAULT_STATE_FILE = (
    CODEX_HOME
    / "skills"
    / "agent-relay"
    / "agent_relay_login_state.json"
)
STATE_FILE = Path(
    os.path.expanduser(
        os.environ.get(
            "AGENT_RELAY_LOGIN_STATE",
            str(DEFAULT_STATE_FILE),
        )
    )
)

LOGIN_SCRIPT = SCRIPT_DIR / "agent_relay_login.py"

BROWSER_PATH = os.environ.get(
    "AGENT_RELAY_BROWSER_PATH"
)

PROVIDER_URL = "https://chat.deepseek.com/"

DEBUG_MODE = os.environ.get(
    "AGENT_RELAY_DEBUG", "false"
).strip().lower() in {"1", "true", "yes", "on"}
LOG_DIR = SKILL_ROOT / "logs"
ERROR_LOG = LOG_DIR / "error.log"


def log_error(message):
    """Write diagnostics without mixing them into the parsed reply output."""

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n"
            )
    except OSError:
        pass


def debug_log(message):
    if DEBUG_MODE:
        print(f"[DEBUG] {message}")

# 新增：历史对话存储配置
HISTORY_DIR = WORKSPACE
HISTORY_FILE_PREFIX = "RecentHistoricalDialogue"
HISTORY_FILE_SUFFIX = {
    "default": "Flash",
    "expert": "Expert",
}
MAX_HISTORY_ITEMS = 6  # 最多保存6次对话

# 新增：图片存储配置
TMP_DIR = os.path.join(WORKSPACE, "tmp")
MAX_TMP_IMAGES = 20  # tmp目录最多20张图片


# ============================================================
# 新增：保存历史对话
# ============================================================

def save_dialogue_history(mode, question, answer, image_paths=None):
    """
    保存对话记录到对应的历史JSON文件。

    Args:
        mode: "default" 或 "expert"
        question: 用户问题
        answer: AI回复
        image_paths: 本次对话包含的图片路径列表（原始路径）
    """
    try:
        # 确定文件名
        suffix = HISTORY_FILE_SUFFIX.get(mode, "Flash")
        history_file = os.path.join(
            HISTORY_DIR,
            f"{HISTORY_FILE_PREFIX}-{suffix}.json"
        )

        # 读取现有历史
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
        else:
            history = []

        # 处理图片（如有）
        new_image_paths = []
        if image_paths:
            new_image_paths = manage_images(image_paths)

        # 创建新记录
        record = {
            "timestamp": time.time(),
            "question": question,
            "answer": answer,
            "images": new_image_paths,  # 存储tmp中的新路径
        }

        # 添加到历史列表
        history.append(record)

        # 只保留最近6条
        if len(history) > MAX_HISTORY_ITEMS:
            history = history[-MAX_HISTORY_ITEMS:]

        # 写回文件
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        print(f"✅ 对话已保存到 {history_file}，当前共 {len(history)} 条记录")

    except Exception as e:
        log_error(f"保存历史对话失败: {e}")
        print(f"⚠️ 保存历史对话失败: {e}")


def manage_images(image_paths):
    """
    将图片复制到tmp目录，并保持tmp目录最多20张图片。

    Args:
        image_paths: 原始图片路径列表

    Returns:
        list: 复制后的新图片路径列表
    """
    try:
        # 确保tmp目录存在
        os.makedirs(TMP_DIR, exist_ok=True)

        new_paths = []
        timestamp = int(time.time() * 1000)  # 毫秒时间戳

        for i, src_path in enumerate(image_paths):
            if not os.path.isfile(src_path):
                print(f"⚠️ 图片不存在，跳过: {src_path}")
                continue

            # 生成唯一目标文件名（避免冲突）
            basename = os.path.basename(src_path)
            target_name = f"{timestamp}_{i}_{basename}"
            target_path = os.path.join(TMP_DIR, target_name)

            # 复制文件
            shutil.copy2(src_path, target_path)
            new_paths.append(target_path)
            print(f"✅ 图片已复制: {basename} -> {target_name}")

        # 清理tmp目录，只保留最新20个文件
        clean_tmp_images()

        return new_paths

    except Exception as e:
        log_error(f"管理图片失败: {e}")
        print(f"⚠️ 管理图片失败: {e}")
        return []


def clean_tmp_images():
    """
    确保tmp目录最多保留20个图片文件，删除最旧的文件。
    """
    try:
        if not os.path.exists(TMP_DIR):
            return

        # 获取tmp目录所有文件（不含子目录）
        files = [
            os.path.join(TMP_DIR, f)
            for f in os.listdir(TMP_DIR)
            if os.path.isfile(os.path.join(TMP_DIR, f))
        ]

        # 如果文件数超过最大值，按修改时间排序，删除最旧的
        if len(files) > MAX_TMP_IMAGES:
            files.sort(key=lambda x: os.path.getmtime(x))  # 最旧在前
            files_to_delete = files[:len(files) - MAX_TMP_IMAGES]
            for file_path in files_to_delete:
                os.remove(file_path)
                print(f"🗑️ 删除旧图片: {os.path.basename(file_path)}")

    except Exception as e:
        log_error(f"清理临时图片失败: {e}")
        print(f"⚠️ 清理tmp图片失败: {e}")


# ============================================================
# DeepSeek 状态
# ============================================================

class DeepSeekStatus:
    """
    DeepSeek 输入框按钮状态。

    SEND：
        ↑
        可以发送消息 / AI 回复完成

    GENERATING：
        ■
        AI 正在生成

    UNKNOWN：
        无法判断
    """

    SEND = "send"
    GENERATING = "generating"
    UNKNOWN = "unknown"


# ============================================================
# 网站适配器基类
# ============================================================

class SiteAdapter:
    """
    网站适配器基类。

    后续新增网站时继承这个类即可。
    """

    name = "unknown"

    def is_match(self, page) -> bool:
        """
        判断当前页面是否属于这个网站。
        """
        raise NotImplementedError

    def wait_for_page(self, page):
        """
        等待网站页面基本加载完成。
        """
        raise NotImplementedError

    def get_chat_list(self, page):
        """
        获取聊天列表。
        """
        raise NotImplementedError

    def find_target_session(self, chat_list, mode="default"):
        """
        查找目标会话。
        """
        raise NotImplementedError

    def send_message(self, page, question):
        """
        发送消息。
        """
        raise NotImplementedError

    def wait_for_response(self, page, question=None, timeout=300):
        """
        等待 AI 回复完成。
        """
        raise NotImplementedError

    def extract_latest_answer(self, page, question):
        """
        提取最新 AI 回复。
        """
        raise NotImplementedError


# ============================================================
# DeepSeek Adapter
# ============================================================

class DeepSeekAdapter(SiteAdapter):
    """
    DeepSeek 网站适配器。

    DeepSeek 回复状态检测：

        ↑
        ↓
      发送消息

        ↓

        ■
        ↓
      AI 正在生成

        ↓

        ↑
        ↓
      AI 回复完成
    """

    name = "deepseek"

    URL_KEYWORDS = (
        "chat.deepseek.com",
        "deepseek.com",
    )

    # DeepSeek 输入框发送/停止按钮 icon
    ICON_SELECTOR = (
        ".ds-button__icon.ds-button__icon--last-child"
    )

    # 发送箭头 SVG Path
    SEND_PATH_PREFIX = "M8.3125"

    # 停止生成 SVG Path
    STOP_PATH_PREFIX = "M2 4.88"

    # 默认最长等待 5 分钟
    DEFAULT_TIMEOUT = 300

    # AI 回复完成以后，等待 DOM 最终更新的时间
    FINAL_RENDER_DELAY = 1.0

    # ========================================================
    # 网站判断
    # ========================================================

    def is_match(self, page) -> bool:
        """
        判断当前页面是否是 DeepSeek。

        第一层：
            URL

        第二层：
            DeepSeek 特征 DOM
        """

        try:
            url = page.url.lower()

            if any(
                    keyword in url
                    for keyword in self.URL_KEYWORDS
            ):
                return True

        except Exception:
            pass

        # URL 判断失败时，尝试页面特征
        try:
            icon = page.locator(
                self.ICON_SELECTOR
            )

            if icon.count() > 0:
                return True

        except Exception:
            pass

        return False

    # ========================================================
    # 页面加载
    # ========================================================

    def wait_for_page(self, page):
        """
        加载 DeepSeek。

        不再使用 networkidle。

        聊天网站通常存在：

            WebSocket
            心跳
            后台请求

        networkidle 并不是一个很好的聊天网站判断标准。
        """

        print("打开 DeepSeek...")

        page.goto(
            PROVIDER_URL,
            wait_until="domcontentloaded",
            timeout=30000
        )

        # 给 React / Vue 页面一点初始化时间
        page.wait_for_timeout(2000)

        if not self.is_match(page):
            raise RuntimeError(
                f"当前页面不是 DeepSeek：{page.url}"
            )

    # ========================================================
    # 获取聊天列表
    # ========================================================

    def get_chat_list(
            self,
            page
    ) -> Dict[str, List[str]]:
        """
        获取 DeepSeek 对话列表。

        当前仍然使用页面文本解析。

        后续如果确定 DeepSeek 侧边栏 DOM，
        可以进一步升级成 DOM 定位。
        """

        body_text = page.inner_text(
            "body"
        )

        lines = [
            line.strip()
            for line in body_text.split("\n")
            if line.strip()
        ]

        result = {}
        current_date = None

        date_keywords = [
            "今天",
            "7天内",
            "30天内",
        ]

        for line in lines:

            is_date = False

            # --------------------------------------------
            # 中文日期分组
            # --------------------------------------------

            for keyword in date_keywords:

                if line == keyword:
                    current_date = keyword
                    result[current_date] = []

                    is_date = True
                    break

            if is_date:
                continue

            # --------------------------------------------
            # YYYY-MM-DD 日期
            # --------------------------------------------

            parts = line.split("-")

            if (
                    len(parts) == 3
                    and len(parts[0]) == 4
                    and parts[0].isdigit()
                    and parts[1].isdigit()
                    and parts[2].isdigit()
            ):
                current_date = line
                result[current_date] = []

                continue

            # --------------------------------------------
            # 会话名称
            # --------------------------------------------

            if current_date:
                result.setdefault(
                    current_date,
                    []
                ).append(line)

        return result

    # ========================================================
    # 查找目标会话
    # ========================================================

    def find_target_session(
            self,
            chat_list,
            mode="default"
    ):
        """
        根据模式寻找目标会话。

        default：
            极速 / 图片

        expert：
            专家 / 思考
        """

        keywords_map = {

            "default": [
                "极速",
                "图片",
            ],

            "expert": [
                "专家",
                "思考",
            ],
        }

        target_keywords = keywords_map.get(
            mode,
            keywords_map["default"]
        )

        date_order = [
            "今天",
            "7天内",
            "30天内",
        ]

        sorted_dates = []

        for date in date_order:

            if date in chat_list:
                sorted_dates.append(date)

        # 加入其他日期
        for date in chat_list:

            if date not in sorted_dates:
                sorted_dates.append(date)

        # 从最新日期开始寻找
        for date in sorted_dates:

            sessions = chat_list.get(
                date,
                []
            )

            for session_name in sessions:

                for keyword in target_keywords:

                    if keyword in session_name:
                        return {
                            "name": session_name,
                            "date": date,
                            "mode": mode,
                        }

        return None

    # ========================================================
    # 获取输入框按钮图标状态
    # ========================================================

    def get_button_status(
            self,
            page
    ) -> str:
        """
        获取 DeepSeek 输入框按钮当前状态。

        ↑：
            SEND

        ■：
            GENERATING

        其他：
            UNKNOWN
        """

        try:

            icons = page.locator(
                self.ICON_SELECTOR
            )

            count = icons.count()

            if count == 0:
                return DeepSeekStatus.UNKNOWN

            # 从后往前检查。
            #
            # 避免页面中存在多个相同 icon 时，
            # 直接依赖 .last。

            for index in range(
                    count - 1,
                    -1,
                    -1
            ):

                icon = icons.nth(index)

                paths = icon.locator(
                    "svg path"
                )

                path_count = paths.count()

                if path_count == 0:
                    continue

                for path_index in range(
                        path_count
                ):

                    d = paths.nth(
                        path_index
                    ).get_attribute("d")

                    if not d:
                        continue

                    d = d.strip()

                    # ■ 停止生成
                    if d.startswith(
                            self.STOP_PATH_PREFIX
                    ):
                        return (
                            DeepSeekStatus.GENERATING
                        )

                    # ↑ 发送
                    if d.startswith(
                            self.SEND_PATH_PREFIX
                    ):
                        return (
                            DeepSeekStatus.SEND
                        )

        except Exception:
            return DeepSeekStatus.UNKNOWN

        return DeepSeekStatus.UNKNOWN

    # ========================================================
    # 等待 AI 开始生成
    # ========================================================

    def wait_for_generation_start(
            self,
            page,
            timeout=None
    ) -> bool:
        """
        发送问题后：

            ↑
            ↓
          等待

            ↓

            ■
            ↓
          AI 开始生成
        """

        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT

        start_time = time.time()

        print(
            "等待 AI 开始生成..."
        )

        while (
                time.time() - start_time
                < timeout
        ):

            status = self.get_button_status(
                page
            )

            if (
                    status
                    == DeepSeekStatus.GENERATING
            ):
                print(
                    "\n✅ 检测到 AI 开始生成"
                )

                return True

            elapsed = int(
                time.time() - start_time
            )

            print(
                f"\r⏳ 等待 AI 开始生成... "
                f"{elapsed}s",
                end="",
                flush=True
            )

            time.sleep(0.2)

        print(
            "\n❌ 等待 AI 开始生成超时"
        )

        return False

    # ========================================================
    # 等待 AI 回复完成
    # ========================================================

    def wait_for_response(
            self,
            page,
            question=None,
            timeout=None
    ) -> bool:
        """
        核心机制：

            ↑
            ↓
          发送

            ↓

            ■
            ↓
        AI 正在生成

            ↓

            ↑
            ↓
        AI 回复完成

        新增快速回复兜底：
            发送后先等待 3 秒。
            如果 3 秒内出现 ■，则进入正常状态机。
            如果 3 秒后仍未出现 ■，则尝试提取一次内容，
            若能提取到有效回答，则认为 AI 已完成（快速回复）。

        最大等待：
            timeout 秒（默认 300）
        """

        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT

        start_time = time.time()

        # ----------------------------------------------------
        # 快速回复检测阶段
        # ----------------------------------------------------
        quick_check_delay = 3.0  # 先等待 3 秒

        print(f"发送完成，先等待 {quick_check_delay} 秒观察按钮状态...")
        time.sleep(quick_check_delay)

        # 检查当前按钮状态
        current_status = self.get_button_status(page)

        if current_status == DeepSeekStatus.GENERATING:
            # 已经进入生成状态，继续走正常状态机
            print("检测到生成中（■），进入正常状态机等待。")
        else:
            # 未检测到 ■，可能是极速回复，尝试提取内容
            print("未检测到生成中（■），尝试提取快速回复内容...")

            # 快速提取答案（不等待加载指示器）
            answer = self._quick_extract_answer(page, question)
            # 修改：只要非空就认为有效（过滤占位符）
            if answer and answer.strip() and answer.strip() != "未获取到有效AI回复":
                print("快速回复内容提取成功，AI 已完成。")
                return True
            else:
                print("未提取到有效内容，继续进入正常状态机等待。")

        # ----------------------------------------------------
        # 第一阶段：等待 ↑ → ■
        # 如果之前已经检测到 ■，这里会立即通过；
        # 否则继续等待直到出现 ■ 或超时。
        # ----------------------------------------------------
        remaining_time = timeout - (time.time() - start_time)
        if not self.wait_for_generation_start(
                page,
                timeout=remaining_time
        ):
            # 超时后，再尝试提取一次内容，如果成功则认为完成
            print("等待生成开始超时，尝试提取已有内容...")
            answer, _ = self.extract_latest_answer(page, question)
            # 同样判断非空且不是占位符
            if answer and answer.strip() and answer.strip() != "未获取到有效AI回复":
                print("超时后成功提取到内容，视为完成。")
                return True
            return False

        # ----------------------------------------------------
        # 第二阶段：等待 ■ → ↑
        # ----------------------------------------------------
        print("等待 AI 回复完成...")

        while (time.time() - start_time) < timeout:
            status = self.get_button_status(page)

            # AI 已经停止生成
            if status == DeepSeekStatus.SEND:
                # 第一次确认
                time.sleep(0.5)
                confirm_status = self.get_button_status(page)
                if confirm_status == DeepSeekStatus.SEND:
                    # 第二次确认后，等待最终 DOM 渲染
                    time.sleep(self.FINAL_RENDER_DELAY)
                    print("\n✅ AI 回复已完成")
                    return True

            elapsed = int(time.time() - start_time)
            print(f"\r⏳ AI 正在生成... {elapsed}s", end="", flush=True)
            time.sleep(0.2)

        print(f"\n⚠️ AI 回复超过 {timeout} 秒，停止等待")
        return False

    def _quick_extract_answer(self, page, question):
        """
        快速提取答案，不等待加载指示器，用于快速回复检测。
        """
        try:
            # 尝试 JS 提取
            result = self._extract_by_javascript(page, question)
            if result and result.strip() and result.strip() != "未获取到有效AI回复":
                return result.strip()
            # 尝试 DOM 提取（修复后使用新方法）
            result = self._extract_by_dom_selectors_fixed(page)
            if result and result.strip() and result.strip() != "未获取到有效AI回复":
                return result.strip()
            # 尝试文本降级
            result = self._extract_by_text_fallback(page, question)
            if result and result.strip() and result.strip() != "未获取到有效AI回复":
                return result.strip()
        except Exception as e:
            log_error(f"快速提取失败: {e}")
        return ""

    def _extract_by_dom_selectors_fixed(self, page):
        """
        修复后的 DOM 提取：直接获取最后一个 .ds-markdown 元素文本。
        """
        try:
            # 查找所有可能的 AI 回复内容元素
            markdown_elements = page.query_selector_all(
                '.ds-markdown, [class*="markdown"]'
            )
            if not markdown_elements:
                return ""
            # 取最后一个
            last_element = markdown_elements[-1]
            text = last_element.inner_text().strip()
            return text
        except Exception as e:
            log_error(f"DOM 提取失败: {e}")
            return ""

    # ========================================================
    # 打开会话
    # ========================================================

    def open_session(
            self,
            page,
            session_name
    ):
        """
        打开指定 DeepSeek 会话。
        """

        print(
            f"\n正在打开会话: "
            f"{session_name}"
        )

        # 优先使用文本定位
        link = page.locator(
            "a",
            has_text=session_name
        ).first

        if link.count() == 0:
            raise RuntimeError(
                f"未找到会话：{session_name}"
            )

        link.click()

        page.wait_for_timeout(2000)

        print(
            f"已打开会话: "
            f"{session_name}"
        )

    # ========================================================
    # 发送消息
    # ========================================================

    def send_message(
            self,
            page,
            question,
            image_path=None
    ):
        """
        发送问题。

        支持：
            纯文本
            1 张图片
            多张图片

        图片上传使用 Playwright 原生：
            set_input_files()

        不使用：
            page.evaluate()
            require('fs')
            Clipboard API

        image_path 支持：

            单张：
                "/tmp/a.png"

            多张：
                [
                    "/tmp/a.png",
                    "/tmp/b.png",
                    "/tmp/c.png"
                ]
        """

        textarea = page.locator(
            "textarea"
        ).first

        if textarea.count() == 0:
            raise RuntimeError(
                "未找到 DeepSeek 输入框"
            )

        textarea.click()

        # ========================================================
        # 处理图片
        # ========================================================

        image_paths = []

        if image_path:

            if isinstance(
                    image_path,
                    str
            ):
                image_paths = [
                    image_path
                ]

            elif isinstance(
                    image_path,
                    (list, tuple)
            ):
                image_paths = list(
                    image_path
                )

            else:
                raise TypeError(
                    "image_path 必须是字符串、"
                    "list 或 tuple"
                )

        # DeepSeek 极速/图片模式最多 5 张
        if len(image_paths) > 5:
            raise ValueError(
                "最多只能上传 5 张图片"
            )

        # --------------------------------------------------------
        # 检查图片文件
        # --------------------------------------------------------

        for path in image_paths:

            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"图片不存在：{path}"
                )

        # ========================================================
        # 上传图片
        # ========================================================

        if image_paths:
            print(
                f"\n📷 准备上传 "
                f"{len(image_paths)} 张图片..."
            )

            self.upload_images(
                page,
                image_paths
            )

            print(
                "✅ 图片上传完成"
            )

            # 给 DeepSeek 图片预览 DOM 一点时间
            page.wait_for_timeout(
                1000
            )

        # ========================================================
        # 输入问题
        # ========================================================

        textarea.fill(
            question
        )

        page.wait_for_timeout(
            300
        )

        # ========================================================
        # 发送
        # ========================================================

        textarea.press(
            "Enter"
        )

        print(
            f"已发送: {question}"
        )

    # ========================================================
    # 上传图片
    # ========================================================

    def upload_images(
            self,
            page,
            image_paths
    ):
        """
        使用 Playwright 原生文件上传。

        优先寻找：
            input[type=file]

        DeepSeek 的文件 input 很可能是隐藏的，
        但 Playwright 的 set_input_files()
        不要求它必须可见。

        支持一次上传最多 5 张。
        """

        file_input = page.locator(
            'input[type="file"]'
        ).first

        if file_input.count() == 0:
            raise RuntimeError(
                "未找到 DeepSeek 图片上传 input[type=file]"
            )

        print(
            "🔍 找到文件上传控件"
        )

        file_input.set_input_files(
            image_paths
        )

        # 等待上传/预览 DOM 更新
        page.wait_for_timeout(
            1000
        )

    # 文本规范化
    # ========================================================

    @staticmethod
    def normalize_text(
            text: str
    ) -> str:
        """
        规范化页面文本。

        用于解决：

            多余空格
            多个换行
            Tab
            不可见空白字符
            零宽字符

        导致的：

            question != page_text

        问题。

        注意：

            这个方法只用于“文本匹配”。

            不用于判断 AI 是否完成。
        """

        if not text:
            return ""

        # 删除常见零宽字符
        text = (
            text
            .replace("\u200b", "")
            .replace("\u200c", "")
            .replace("\u200d", "")
            .replace("\ufeff", "")
        )

        # 把连续空白统一成一个空格
        return " ".join(
            text.split()
        )

    # ========================================================
    # 提取最新回复（DeepSeek专家模式优化版）
    # ========================================================

    def extract_latest_answer(
            self,
            page,
            question
    ):
        """
        从 DeepSeek 页面提取最新的 AI 回复内容

        使用三重策略逐级降级：
            1. JavaScript IIFE DOM 查询（最优）
            2. Playwright DOM 选择器
            3. body.inner_text() 文本分析

        Args:
            page: Playwright Page 对象
            question: 用户提出的问题文本

        Returns:
            tuple: (ai_answer, full_body) AI回复内容和完整页面文本
        """
        try:
            # 等待页面稳定
            self._wait_for_response_complete(page)

            # 策略1：JavaScript IIFE DOM查询
            result = self._extract_by_javascript(page, question)
            if result and result.strip():
                full_body = page.inner_text("body")
                debug_log(f"策略1成功，AI回复长度={len(result)}")
                return (result.strip(), full_body)

            # 策略2：修复后的 DOM 选择器
            result = self._extract_by_dom_selectors_fixed(page)
            if result and result.strip():
                full_body = page.inner_text("body")
                debug_log(f"策略2成功，AI回复长度={len(result)}")
                return (result.strip(), full_body)

            # 策略3：文本分析降级方案
            result = self._extract_by_text_fallback(page, question)
            if result and result.strip():
                full_body = page.inner_text("body")
                debug_log(f"策略3成功，AI回复长度={len(result)}")
                return (result.strip(), full_body)

            # 所有策略都失败
            log_error("所有回复提取策略都失败")
            fallback_text = page.inner_text("body")
            return ("未获取到有效AI回复", fallback_text)

        except Exception as e:
            log_error(f"extract_latest_answer 异常: {e}")
            try:
                fallback_text = page.inner_text("body")
                return ("提取失败", fallback_text)
            except:
                return ("提取失败", "")

    def _wait_for_response_complete(self, page, timeout=30):
        """
        等待 AI 回复生成完成。

        等待生成中的指示器消失，然后额外等待确保内容完全渲染。
        """
        import time as _time

        try:
            # 等待生成中的指示器消失
            page.wait_for_selector(
                'div[class*="loading"], div[class*="generating"], div[class*="thinking"]',
                state='detached',
                timeout=timeout * 1000
            )
        except Exception:
            # 如果没有找到加载指示器，等待固定时间
            _time.sleep(3)

        # 额外等待确保内容完全渲染
        _time.sleep(1)

    def _extract_by_javascript(self, page, question):
        """
        策略1：使用 JavaScript IIFE 在浏览器中查询 DOM。

        这是最可靠的方案，直接在页面环境中执行DOM查询。
        """
        try:
            js_code = """
            (userQuestion) => {
                const cleanQuestion = userQuestion.trim().replace(/\\s+/g, ' ').slice(0, 100);

                // 查找所有可能的消息元素（排除侧边栏）
                const allElements = document.querySelectorAll(
                    'div[class*="ds-message"], div[class*="user-message"], div[class*="message"]'
                );

                let targetUserMsg = null;
                let maxMatchScore = 0;

                for (const el of allElements) {
                    // 排除侧边栏中的消息
                    const isInSidebar = el.closest('aside, nav, [class*="sidebar"], [class*="history"]');
                    if (isInSidebar) continue;

                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text) continue;

                    // 计算匹配度
                    let matchScore = 0;
                    const cleanText = text.replace(/\\s+/g, ' ').slice(0, 200);

                    if (cleanText.includes(cleanQuestion)) {
                        matchScore = 100;
                    } else if (cleanQuestion.includes(cleanText) ||
                               cleanText.includes(cleanQuestion.slice(0, 50))) {
                        matchScore = 80;
                    } else {
                        const keywords = cleanQuestion.split(' ').filter(w => w.length > 2);
                        const matchedKeywords = keywords.filter(w => cleanText.includes(w));
                        if (keywords.length > 0) {
                            matchScore = (matchedKeywords.length / keywords.length) * 60;
                        }
                    }

                    if (matchScore > maxMatchScore && matchScore >= 40) {
                        maxMatchScore = matchScore;
                        targetUserMsg = el;
                    }
                }

                if (!targetUserMsg || maxMatchScore < 40) {
                    return null;
                }

                // 向上查找包含 AI 回复的容器
                let container = targetUserMsg.parentElement;
                let depth = 0;

                while (container && depth < 10) {
                    const aiElements = container.querySelectorAll(
                        '.ds-markdown.ds-assistant-message-main-content, ' +
                        '[class*="assistant-message"], [class*="ai-message"], [class*="markdown"]'
                    );

                    if (aiElements.length > 0) break;
                    container = container.parentElement;
                    depth++;
                }

                if (!container) return null;

                // 在容器中查找 AI 回复内容
                const aiSelectors = [
                    '.ds-markdown.ds-assistant-message-main-content',
                    '[class*="assistant-message-main-content"]',
                    '[class*="assistant-message"]',
                    '[class*="markdown"]',
                    '[class*="ai-message"]'
                ];

                let aiContent = null;
                for (const selector of aiSelectors) {
                    const elements = container.querySelectorAll(selector);
                    if (elements.length > 0) {
                        aiContent = elements[elements.length - 1];
                        if (aiContent.innerText && aiContent.innerText.trim().length > 0) break;
                    }
                }

                if (aiContent && aiContent.innerText) {
                    return aiContent.innerText.trim();
                }

                // 降级：从容器文本中提取
                const containerText = (container.innerText || container.textContent || '');
                const questionIndex = containerText.lastIndexOf(userQuestion);

                if (questionIndex !== -1) {
                    let response = containerText.substring(questionIndex + userQuestion.length);
                    response = response.replace(/^\\s*[:：]?\\s*/, '');
                    response = response.replace(/^(AI|助手|Assistant)[:：]\\s*/i, '');
                    return response.trim();
                }

                return null;
            }
            """

            result = page.evaluate(js_code, question)
            return result if isinstance(result, str) else None

        except Exception as e:
            log_error(f"JavaScript 提取失败: {e}")
            return None

    def _extract_by_text_fallback(self, page, question):
        """
        策略3：降级方案 - 文本分析。

        当DOM查询都失败时使用。
        """
        try:
            body_text = page.inner_text("body")

            # 使用 rfind 查找最后一个匹配（避开侧边栏）
            question_pos = body_text.rfind(question)

            if question_pos == -1:
                question_short = question[:50]
                question_pos = body_text.rfind(question_short)

                if question_pos == -1:
                    return ""

            text_after = body_text[question_pos + len(question):]
            return self._extract_ai_part_from_text(text_after, question)

        except Exception as e:
            log_error(f"文本降级提取失败: {e}")
            return ""

    def _extract_ai_part_from_text(self, text, question):
        """
        从文本中提取 AI 回复部分。

        通过识别用户消息分隔符和AI标记来分离回复内容。
        """
        try:
            if not text:
                return ""

            lines = text.strip().split('\n')
            response_lines = []
            collecting = False

            for line in lines:
                stripped_line = line.strip()

                # 保留段落分隔的空行
                if not stripped_line:
                    if collecting:
                        response_lines.append('')
                    continue

                # 检查是否遇到下一个用户消息
                if (stripped_line.startswith('用户:') or
                        stripped_line.startswith('用户：') or
                        stripped_line.startswith('User:') or
                        stripped_line.startswith('Human:')):
                    if collecting:
                        break
                    else:
                        continue

                # 开始收集 AI 回复
                if not collecting:
                    cleaned = stripped_line.replace('AI:', '').replace('AI：', '')
                    cleaned = cleaned.replace('助手:', '').replace('助手：', '')
                    cleaned = cleaned.replace('Assistant:', '').strip()

                    if cleaned:
                        collecting = True
                        response_lines.append(cleaned)
                else:
                    response_lines.append(stripped_line)

            # 清理尾部空行
            while response_lines and not response_lines[-1]:
                response_lines.pop()

            return '\n'.join(response_lines).strip()

        except Exception as e:
            log_error(f"AI 部分提取失败: {e}")
            return ""

    def _is_question_match(self, msg_text, question):
        """
        判断消息文本是否匹配用户问题。

        支持完全匹配、包含匹配和部分匹配。
        """
        try:
            clean_msg = msg_text.strip().replace('\n', ' ').replace('\r', ' ')
            clean_question = question.strip().replace('\n', ' ').replace('\r', ' ')

            if clean_msg == clean_question:
                return True

            if clean_question in clean_msg or clean_msg in clean_question:
                return True

            if len(clean_question) > 20:
                short_q = clean_question[:50]
                if short_q in clean_msg or clean_msg[:50] in short_q:
                    return True

            return False

        except Exception:
            return False


class SiteDetector:
    """
    网站检测器。

    后续增加网站时：

        adapters = [
            DeepSeekAdapter(),
            KimiAdapter(),
            ChatGPTAdapter(),
        ]

    即可。
    """

    def __init__(self):

        self.adapters = [
            DeepSeekAdapter(),
        ]

    def detect(
            self,
            page
    ) -> Optional[SiteAdapter]:
        """
        检测当前网站。
        """

        print(
            f"\n正在识别网站: "
            f"{page.url}"
        )

        for adapter in self.adapters:

            try:

                if adapter.is_match(
                        page
                ):
                    print(
                        f"✅ 检测到网站: "
                        f"{adapter.name}"
                    )

                    return adapter

            except Exception as e:

                log_error(f"检测网站 {adapter.name} 失败: {e}")

                print(
                    f"⚠️ 检测 "
                    f"{adapter.name} 失败: "
                    f"{e}"
                )

        return None


# ============================================================
# 登录状态
# ============================================================

def check_login() -> bool:
    """
    检查当前 Provider 的登录状态文件。

    注意：

        这里只检查 storage_state 文件是否存在。

    真正登录是否有效，
    仍然由访问 DeepSeek 后进一步确认。
    """

    if not os.path.exists(
            STATE_FILE
    ):
        return False

    try:

        with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
        ) as file:

            data = json.load(
                file
            )

        if (
                "cookies" in data
                or "origins" in data
        ):
            return True

    except (
            json.JSONDecodeError,
            OSError,
            TypeError
    ):
        pass

    return False


# ============================================================
# 创建浏览器
# ============================================================

def create_browser_context(
        playwright
):
    """
    创建 Playwright Browser Context。
    """

    launch_options = {
        "headless": True,
    }

    if BROWSER_PATH:
        browser_path = os.path.expanduser(BROWSER_PATH)

        if os.path.isfile(browser_path):
            launch_options["executable_path"] = browser_path
        else:
            print(
                "AGENT_RELAY_BROWSER_PATH does not exist; "
                "falling back to Playwright's browser.",
                file=sys.stderr,
            )

    browser = playwright.chromium.launch(
        **launch_options
    )

    context = browser.new_context(
        storage_state=STATE_FILE
    )

    return browser, context


# ============================================================
# 主业务
# ============================================================

def run_provider(
        question,
        mode,
        image_paths=None
):
    """
    执行当前 Provider 的自动对话。
    """

    from playwright.sync_api import (
        sync_playwright
    )

    adapter = DeepSeekAdapter()

    with sync_playwright() as p:

        browser = None
        context = None

        try:

            browser, context = (
                create_browser_context(
                    p
                )
            )

            page = context.new_page()

            # ------------------------------------------------
            # 打开网站
            # ------------------------------------------------

            adapter.wait_for_page(
                page
            )

            # ------------------------------------------------
            # 网站检测
            # ------------------------------------------------

            detector = SiteDetector()

            detected_adapter = (
                detector.detect(
                    page
                )
            )

            if detected_adapter is None:
                raise RuntimeError(
                    "无法识别当前网站"
                )

            # 确保当前使用对应 Adapter
            adapter = detected_adapter

            # ------------------------------------------------
            # 获取会话列表
            # ------------------------------------------------

            print(
                "\n正在获取对话列表..."
            )

            chat_list = (
                adapter.get_chat_list(
                    page
                )
            )

            if not chat_list:
                print(
                    "❌ 未能获取到对话列表。"
                )

                print(
                    "可能原因："
                )

                print(
                    "  1. 登录状态失效"
                )

                print(
                    "  2. 页面加载失败"
                )

                print(
                    "  3. DeepSeek 页面结构发生变化"
                )

                return None

            # ------------------------------------------------
            # 输出对话列表
            # ------------------------------------------------

            print(
                "\n=== 你的 DeepSeek 对话列表 ===\n"
            )

            for date, sessions in (
                    chat_list.items()
            ):

                print(
                    f"[{date}]"
                )

                for session in sessions:
                    print(
                        f"    - {session}"
                    )

            # ------------------------------------------------
            # 查找两个模式的目标会话
            # ------------------------------------------------

            default_target = (
                adapter.find_target_session(
                    chat_list,
                    mode="default"
                )
            )

            expert_target = (
                adapter.find_target_session(
                    chat_list,
                    mode="expert"
                )
            )

            print(
                "\n=== 智能匹配结果 ===\n"
            )

            if default_target:

                print(
                    "✅ 极速/图片对话（默认）："
                    f"{default_target['name']}"
                    f"（{default_target['date']}）"
                )

            else:

                print(
                    "❌ 未找到极速/图片类对话"
                )

            if expert_target:

                print(
                    "✅ 专家/思考对话（深度思考）："
                    f"{expert_target['name']}"
                    f"（{expert_target['date']}）"
                )

            else:

                print(
                    "❌ 未找到专家/思考类对话"
                )

            # ------------------------------------------------
            # 选择目标
            # ------------------------------------------------

            target = (
                default_target
                if mode == "default"
                else expert_target
            )

            if not target:
                print(
                    "\n❌ 未找到当前模式对应的目标会话"
                )

                return None

            # ------------------------------------------------
            # 打开目标会话
            # ------------------------------------------------

            adapter.open_session(
                page,
                target["name"]
            )

            # ------------------------------------------------
            # 发送问题
            # ------------------------------------------------

            print(
                "\n" + "=" * 50
            )

            # ------------------------------------------------
            # 发送问题（支持图片）
            # ------------------------------------------------

            print(
                "\n" + "=" * 50
            )

            # ----------------------------------------------------
            # 收集图片路径：函数参数 + 环境变量 AGENT_RELAY_IMAGE_N
            # ----------------------------------------------------
            final_images = list(image_paths) if image_paths else []

            for _idx in range(1, 6):
                env_path = os.environ.get(f"AGENT_RELAY_IMAGE_{_idx}")
                if env_path:
                    final_images.append(env_path)

            # 去重但保持顺序
            seen = set()
            unique_images = []
            for p in final_images:
                rp = os.path.realpath(p)
                if rp not in seen:
                    seen.add(rp)
                    unique_images.append(p)
            final_images = unique_images

            if final_images:
                print(f"📷 附加图片数量: {len(final_images)}")
                for p in final_images:
                    print(f"   - {p}")

            adapter.send_message(
                page,
                question,
                image_path=final_images if final_images else None
            )

            # ------------------------------------------------
            # 等待 AI 回复
            # ------------------------------------------------

            success = (
                adapter.wait_for_response(
                    page,
                    question=question,
                    timeout=300
                )
            )

            if not success:
                print(
                    "\n❌ AI 回复等待失败或超时"
                )

                # 即使超时，也尝试获取当前已经生成的内容
                print(
                    "⚠️ 尝试获取当前已经生成的内容..."
                )

            # ------------------------------------------------
            # 提取回复
            # ------------------------------------------------

            answer, full_content = (
                adapter.extract_latest_answer(
                    page,
                    question
                )
            )

            # ------------------------------------------------
            # 返回结果（包含实际使用的图片列表）
            # ------------------------------------------------

            return {
                "session_name": target["name"],
                "date": target["date"],
                "mode": mode,
                "question": question,
                "answer": answer,
                "full_content": full_content,
                "images": final_images if final_images else [],
            }

        finally:

            if context:

                try:
                    context.close()

                except Exception:
                    pass

            if browser:

                try:
                    browser.close()

                except Exception:
                    pass


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="AgentRelay 在线专家模型中继"
    )

    parser.add_argument(
        "question",
        nargs="?",
        default=None,
        help="要问的问题"
    )

    parser.add_argument(
        "-m",
        "--mode",
        choices=[
            "1",
            "2"
        ],
        default=None,
        help=(
            "模式："
            "1=极速/图片（默认），"
            "2=专家/思考"
        )
    )

    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help=(
            "要附加的图片文件路径，"
            "最多 5 张。可以重复使用 --image。"
        )
    )

    args = parser.parse_args()

    print(
        "=" * 60
    )

    print(
        "  AgentRelay 在线专家模型中继"
    )

    print(
        "=" * 60
    )

    # ========================================================
    # 检查登录
    # ========================================================

    if not check_login():
        print(
            "\n❌ 未检测到当前 Provider（DeepSeek）的登录状态。"
        )

        print(
            "\n请先运行登录脚本："
        )

        print(
            f"   python3 {LOGIN_SCRIPT}"
        )

        print()

        return

    print(
        "\n✅ 检测到登录状态文件。"
    )

    # ========================================================
    # 获取问题
    # ========================================================

    if args.question is not None:

        question = (
            args.question.strip()
        )

    else:

        question = input(
            "\n请输入你要问的问题: "
        ).strip()

    if not question:
        print(
            "❌ 问题为空，退出。"
        )

        return

    # ========================================================
    # 获取模式
    # ========================================================

    if args.mode is not None:

        mode_choice = args.mode

    else:

        mode_choice = input(
            "\n选择模式 "
            "[1=极速/图片(默认), "
            "2=专家/思考]: "
        ).strip()

    mode = (
        "expert"
        if mode_choice == "2"
        else "default"
    )

    print(
        "\n当前模式："
        + (
            "专家/思考"
            if mode == "expert"
            else "极速/图片"
        )
    )

    # ========================================================
    # 执行
    # ========================================================

    try:

        # 收集命令行 --image 参数
        cli_images = list(args.image) if args.image else []

        result = run_provider(
            question,
            mode,
            image_paths=cli_images if cli_images else None
        )

        if not result:
            return

        # ----------------------------------------------------
        # 保存历史对话（新增）
        # ----------------------------------------------------
        save_dialogue_history(
            mode=result["mode"],
            question=result["question"],
            answer=result["answer"],
            image_paths=result.get("images", [])
        )

        # ----------------------------------------------------
        # 输出结果
        # ----------------------------------------------------

        print(
            "\n" + "=" * 60
        )

        print(
            f"📋 会话: "
            f"{result['session_name']} "
            f"({result['date']})"
        )

        print(
            "🔖 模式: "
            + (
                "专家/思考"
                if result["mode"] == "expert"
                else "极速/图片"
            )
        )

        print(
            f"❓ 问题: "
            f"{result['question']}"
        )

        print(
            "\n💬 AI 回复:"
        )

        print(
            result["answer"]
        )

        print(
            "\n" + "=" * 60
        )

    except KeyboardInterrupt:

        print(
            "\n\n⚠️ 用户主动停止。"
        )

    except Exception as e:

        log_error(f"运行失败: {e}\n{traceback.format_exc()}")

        print(
            f"\n⚠️ 出错: {e}（详情已写入 {ERROR_LOG}）"
        )


# ============================================================
# Python 入口
# ============================================================

if __name__ == "__main__":
    main()
