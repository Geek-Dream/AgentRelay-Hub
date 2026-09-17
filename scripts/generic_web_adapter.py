#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
通用网页兜底适配器。

任何一个只保存了网页登录配置、但没有专属适配器的网站（例如千问、Kimi），
都通过这个适配器尝试自动对话。它不依赖写死的网站选择器，全部使用启发式：

    输入框    textarea → [contenteditable=true] → [role=textbox]
    发送      Enter；数秒内没有生成迹象则补点发送按钮
    会话      路径型 /chat/xxx 链接 + SPA 的 [data-session-id] 条目
    等回复    生成控件消失 + 回复内容连续稳定（兼容秒回，不依赖按钮状态机）
    取回复    先按本次问题定位消息轮次，再读取该轮的助手回答
    风控      识别 baxia/滑块/验证码等拦截；有头模式暂停等人工验证，
              无头模式明确报错；成功后回写登录状态延长验证有效期

限制：

    启发式不是标准。网站改版、验证码弹窗、特殊输入控件会让兜底失败；
    失败时报具体环节。需要高稳定性的网站应实现专属适配器
    （参考 agent_relay.py 里的 DeepSeekAdapter），注册后优先于本兜底。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

try:
    from .agent_relay import SiteAdapter
    from .agent_relay_runtime import (
        ProviderSpec,
        SessionRef,
        normalize_provider_name,
    )
except ImportError:  # 直接以脚本方式运行时
    from agent_relay import SiteAdapter
    from agent_relay_runtime import (
        ProviderSpec,
        SessionRef,
        normalize_provider_name,
    )


class GenericWebAdapter(SiteAdapter):
    """通用兜底网站适配器；registry 中作为 fallback 最后尝试。"""

    fallback = True
    # 有头运行：反风控更稳，且验证弹窗可以交给用户处理。可用
    # AGENT_RELAY_WEB_HEADFUL=0 强制无头。
    prefer_headful = True
    # 对话成功后把新 Cookie 回写到登录状态文件（验证通过的凭据可以复用）。
    refresh_state = True
    # 支持人工代问模式：用户自己在浏览器输入问题，工具监听并提取回答。
    supports_human_assisted = True

    # 输入框候选，按优先级
    INPUT_SELECTORS = (
        "textarea",
        '[contenteditable="true"]',
        '[role="textbox"]',
    )

    # 发送按钮可访问名称特征
    SEND_HINTS = ("发送", "send", "提交", "submit", "arrow", "箭头")

    # 停止生成控件特征（出现表示 AI 正在生成）
    STOP_HINTS = (
        "停止生成", "停止回答", "停止", "stop generating",
        "stop answering", "stop", "暂停", "pause",
    )

    # 新对话按钮/链接文字特征
    NEW_CHAT_HINTS = ("新对话", "新建对话", "开启新对话", "new chat", "new conversation")

    # 会话链接的路径特征（命中后取后续段作为 session_id）
    SESSION_PATH_MARKERS = (
        "/chat/", "/c/", "/conversation/", "/conversations/",
        "/session/", "/s/", "/dialog/", "/topic/", "/t/",
    )

    # URL 查询参数里可能放会话 id 的键名
    SESSION_QUERY_KEYS = (
        "chatid", "chat_id", "session_id", "conversation_id",
        "conv_id", "sid", "c", "id",
    )

    # 助手回复内容块的特征 class
    REPLY_CLASS_HINTS = (
        "markdown", "assistant", "ai-message", "ai_message", "aimsg",
        "bot-message", "bot_message", "model-message", "answer-content",
        "response-text", "msg-content", "reply-content", "message-content",
        "answer-text", "chat-answer", "answer-card", "answer-text-card",
    )

    # 默认最长等待 5 分钟
    DEFAULT_TIMEOUT = 300

    # 回复 DOM 稳定判定：连续多少轮无变化视为生成完成
    STABLE_ROUNDS = 3
    STABLE_INTERVAL = 1.5

    # AI 回复完成后，等待 DOM 最终更新的时间
    FINAL_RENDER_DELAY = 1.0

    # 发送后等待"生成迹象"的时间；超时未出现则补点一次发送按钮
    GENERATION_START_GRACE = 5.0

    # 有头模式下等待人工完成风控验证的秒数
    WALL_WAIT_SECONDS = 120

    def __init__(self, provider_name: Optional[str] = None,
                 base_url: Optional[str] = None):
        configured = str(provider_name or "").strip()
        name = normalize_provider_name(configured) if configured else "generic"
        entry = self._provider_entry(name) if configured else None
        self._entry = entry or {}
        resolved_base = str(base_url or "").strip()
        if not resolved_base:
            resolved_base = str(
                (entry or {}).get("base_url")
                or (entry or {}).get("url")
                or ""
            ).strip()
        if not resolved_base:
            # 占位网址只用于通过 ProviderSpec 校验；调用时会明确报错。
            resolved_base = "https://invalid.example/"
        self.spec = ProviderSpec(name=name, base_url=resolved_base)
        headful_env = os.environ.get("AGENT_RELAY_WEB_HEADFUL", "").strip().lower()
        if headful_env:
            self._headed = headful_env not in {"0", "false", "no", "off"}
        else:
            self._headed = self.prefer_headful
        self._wall_wait = int(os.environ.get("AGENT_RELAY_WEB_WALL_WAIT", "")
                              or self.WALL_WAIT_SECONDS)
        self._pre_send_text = ""

    # ========================================================
    # 配置读取
    # ========================================================

    @staticmethod
    def _provider_entry(name: str) -> Optional[dict]:
        try:
            try:
                from .config_manager import load_config
            except ImportError:
                from config_manager import load_config
            try:
                from .agent_relay import CODEX_HOME
            except ImportError:
                from agent_relay import CODEX_HOME
            entries = load_config(CODEX_HOME).get("web_providers", [])
        except Exception:
            return None
        candidates = (name, name + "-web")
        for item in entries:
            if isinstance(item, dict) and str(item.get("id", "")) in candidates:
                return item
        return None

    def _conversation(self) -> dict:
        conversation = (self._entry or {}).get("conversation")
        return conversation if isinstance(conversation, dict) else {}

    def _display_brand(self) -> str:
        brand = self.name.removesuffix("-web")
        return "-".join(part.capitalize() for part in brand.split("-") if part) or "Web"

    # ========================================================
    # 网站判断
    # ========================================================

    def _host_of(self, url: str) -> str:
        try:
            return (urlparse(url).hostname or "").lower()
        except Exception:
            return ""

    def is_match(self, page) -> bool:
        target = self._host_of(self.base_url)
        current = self._host_of(getattr(page, "url", ""))
        if not target or not current or target == "invalid.example":
            return False
        return (
            current == target
            or current.endswith("." + target)
            or target.endswith("." + current)
        )

    # ========================================================
    # 风控墙（人机验证）识别与处理
    # ========================================================

    _WALL_JS = """
    () => {
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) return false;
            const style = getComputedStyle(el);
            return style.visibility !== 'hidden' && style.display !== 'none';
        };
        const ifr = [...document.querySelectorAll('iframe')].some(f =>
            visible(f) && /punish|baxia|captcha|verify|x5sec/i.test(
                (f.src || '') + ' ' + (f.id || '') + ' ' + (f.getAttribute('class') || '')));
        const cap = [...document.querySelectorAll(
            '[class*="captcha" i], [id*="baxia" i], [class*="verify-slider" i], ' +
            '[class*="geetest" i], [class*="nc_iconfont" i], [class*="slidebtn" i]'
        )].some(el => el.tagName !== 'SCRIPT' && visible(el));
        const url = /punish|verify|captcha|x5sec/i.test(location.href);
        return ifr || cap || url;
    }
    """

    def _has_wall(self, page) -> bool:
        try:
            return bool(page.evaluate(self._WALL_JS))
        except Exception:
            return False

    def _ensure_no_wall(self, page, start_time: float) -> None:
        """检测到风控墙时：有头模式等人工验证，无头模式明确报错。

        带防抖：墙必须持续存在约 1.5 秒才认定，避免加载瞬态误报。
        """
        if not self._has_wall(page):
            return
        page.wait_for_timeout(1500)
        if not self._has_wall(page):
            return
        if self._headed:
            remaining = max(10.0, self._wall_wait - (time.time() - start_time))
            print(
                "\n⚠️ 网站弹出了人机验证/风控（如滑块验证）。"
                "请在打开的浏览器窗口里手动完成验证……"
            )
            deadline = time.time() + remaining
            while time.time() < deadline:
                page.wait_for_timeout(1500)
                if not self._has_wall(page):
                    print("✅ 验证已通过，继续。")
                    return
            raise RuntimeError(
                "等待人工验证超时；网页对话被该网站风控拦截"
            )
        raise RuntimeError(
            "网站弹出了人机验证/风控拦截（如滑块验证），无头模式无法完成。"
            "可：1) 设置 AGENT_RELAY_WEB_HEADFUL=1 后在弹出的浏览器里手动完成验证；"
            "2) 稍后重试；3) 改用该网站的 API 模型"
        )

    # ========================================================
    # 页面加载
    # ========================================================

    def wait_for_page(self, page):
        if self._host_of(self.base_url) in ("", "invalid.example"):
            raise RuntimeError(
                f"未配置 Provider（{self.name}）的对话页面网址，无法自动打开"
            )
        print(f"打开 {self.base_url} ...")
        page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
        # 给 React / Vue 页面初始化时间
        page.wait_for_timeout(2500)
        if not self.is_match(page):
            raise RuntimeError(
                f"当前页面不是已配置的 {self.name} 页面：{page.url}"
            )
        if self._has_wall(page):
            self._ensure_no_wall(page, time.time())
        if self._find_input(page) is None:
            raise RuntimeError(
                "页面上找不到可用输入框；可能未登录、网址不对，"
                "或该网站页面结构不兼容通用适配器"
            )

    # ========================================================
    # 输入框
    # ========================================================

    def _find_input(self, page):
        for selector in self.INPUT_SELECTORS:
            try:
                locator = page.locator(f"{selector}:visible")
                if locator.count() == 0:
                    continue
                candidate = locator.first
                try:
                    if not candidate.is_editable():
                        continue
                except Exception:
                    pass
                return candidate
            except Exception:
                continue
        return None

    def _input_tag(self, locator) -> str:
        try:
            return str(locator.evaluate("el => el.tagName.toLowerCase()"))
        except Exception:
            return ""

    # ========================================================
    # 会话引用：路径型 + SPA 型
    # ========================================================

    def _session_from_link(self, title: str, href: str) -> Optional[SessionRef]:
        if not href or href.startswith("javascript"):
            return None
        absolute_href = urljoin(self.base_url, href)
        parsed = urlparse(absolute_href)
        if parsed.scheme not in {"http", "https"}:
            return None
        target_host = self._host_of(self.base_url)
        current_host = (parsed.hostname or "").lower()
        if not (
            current_host == target_host
            or current_host.endswith("." + target_host)
            or target_host.endswith("." + current_host)
        ):
            return None
        path = parsed.path.lower()
        session_id = ""
        for marker in self.SESSION_PATH_MARKERS:
            index = path.find(marker)
            if index < 0:
                continue
            session_id = path[index + len(marker):].split("/", 1)[0].strip()
            if session_id:
                break
        if not session_id:
            # 部分 SPA 把会话 id 放在查询参数里（键名大小写不敏感）
            query = {
                str(key).lower(): value
                for key, value in parse_qs(parsed.query).items()
            }
            for key in self.SESSION_QUERY_KEYS:
                value = (query.get(key) or [""])[0].strip()
                if value:
                    session_id = value
                    break
        if not session_id:
            return None
        normalized_title = " ".join(str(title or "").split())[:100]
        try:
            return SessionRef(
                provider=self.name,
                session_id=session_id,
                title=normalized_title,
                href=absolute_href,
            )
        except ValueError:
            return None

    @staticmethod
    def _is_spa_ref(session: SessionRef) -> bool:
        """会话引用是否来自 SPA 发现（href 片段里存着 session_id）。"""
        try:
            return bool(session) and urlparse(session.href).fragment == session.session_id
        except Exception:
            return False

    def _spa_sessions(self, page) -> List[dict]:
        """扫描 [data-session-id] 条目（SPA 网站常见写法）。"""
        try:
            rows = page.evaluate(
                """
                () => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('[data-session-id]').forEach(el => {
                        const id = el.getAttribute('data-session-id');
                        if (!id || seen.has(id)) return;
                        seen.add(id);
                        out.push({
                            id: id,
                            title: (el.innerText || '').trim().split('\\n')[0].slice(0, 100),
                            active: el.getAttribute('data-session-active') === 'true',
                        });
                    });
                    return out;
                }
                """
            )
        except Exception:
            return []
        return [row for row in (rows or []) if isinstance(row, dict) and row.get("id")]

    def list_sessions(self, page) -> List[SessionRef]:
        sessions: List[SessionRef] = []
        seen_ids = set()

        try:
            rows = page.evaluate(
                """
                () => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('a[href]').forEach(anchor => {
                        const href = anchor.getAttribute('href') || '';
                        const text = (anchor.innerText || anchor.textContent || '').trim();
                        if (!href || !text || seen.has(href)) return;
                        seen.add(href);
                        out.push({title: text, href: href});
                    });
                    return out;
                }
                """
            )
        except Exception:
            rows = []
        for row in rows or []:
            session = self._session_from_link(
                str(row.get("title", "")), str(row.get("href", ""))
            )
            if session is None or session.session_id in seen_ids:
                continue
            seen_ids.add(session.session_id)
            sessions.append(session)

        # SPA：没有链接的站点从 data-session-id 条目发现会话
        for row in self._spa_sessions(page):
            session_id = str(row.get("id", "")).strip()
            if not session_id or session_id in seen_ids:
                continue
            title = " ".join(str(row.get("title", "")).split())[:100]
            try:
                sessions.append(SessionRef(
                    provider=self.name,
                    session_id=session_id,
                    title=title,
                    href=f"{self.base_url}#{session_id}",
                ))
                seen_ids.add(session_id)
            except ValueError:
                continue
        return sessions

    def is_session_reference_valid(self, session: SessionRef) -> bool:
        if not super().is_session_reference_valid(session):
            return False
        if self._is_spa_ref(session):
            host = self._host_of(session.href)
            target = self._host_of(self.base_url)
            return bool(session.session_id) and (
                host == target or host.endswith("." + target)
                or target.endswith("." + host)
            )
        parsed = self._session_from_link(session.title, session.href)
        return parsed is not None and parsed.session_id == session.session_id

    # ========================================================
    # 会话标题与模式
    # ========================================================

    def canonical_session_title(self, mode: str) -> str:
        brand = self._display_brand()
        defaults = {
            "default": f"AgentRelay-{brand}",
            "flash": f"AgentRelay-{brand}-Flash",
            "expert": f"AgentRelay-{brand}-Expert",
            "hybrid": f"AgentRelay-{brand}",
        }
        conversation = self._conversation()
        titles = conversation.get("titles", {})
        if not isinstance(titles, dict):
            titles = {}
        if conversation.get("supports_hybrid"):
            # 混合模式 = 一套统一会话，同时处理极速和专家提问
            return str(titles.get("hybrid") or defaults["hybrid"])
        return str(
            titles.get(mode)
            or titles.get("hybrid")
            or defaults.get(mode, defaults["default"])
        )

    def session_scope(self, mode: str) -> str:
        if self._conversation().get("supports_hybrid"):
            return "hybrid"
        return "expert" if mode == "expert" else "flash"

    def configure_mode(self, page, mode: str) -> None:
        # 通用兜底不操作网站的模式开关；需要精准控制时应写专属适配器。
        return None

    # ========================================================
    # 打开/新建会话
    # ========================================================

    def open_session(self, page, session: SessionRef):
        print(f"\n正在打开会话: {session.title or session.session_id}")
        if not self.is_session_reference_valid(session):
            raise RuntimeError(f"无效的 {self.name} 会话引用：{session.session_id}")
        if self._is_spa_ref(session):
            # SPA：回到首页后点击会话条目，比拼 URL 更可靠
            page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            item = page.locator(f'[data-session-id="{session.session_id}"]').first
            if item.count() == 0:
                raise RuntimeError(f"会话不存在或已删除：{session.session_id}")
            item.click()
            page.wait_for_timeout(1500)
        else:
            page.goto(session.href, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
        if not self.is_session_open(page, session):
            raise RuntimeError(f"会话不可用或已删除：{session.session_id}")

    def is_session_open(self, page, session: SessionRef) -> bool:
        url = getattr(page, "url", "")
        if self._is_spa_ref(session):
            if session.session_id in url:
                return self._find_input(page) is not None
            try:
                active = page.evaluate(
                    f'() => !!document.querySelector('
                    f"'[data-session-active=\"true\"][data-session-id=\"{session.session_id}\"]')"
                )
                if active:
                    return self._find_input(page) is not None
                item = page.locator(f'[data-session-id="{session.session_id}"]').first
                if item.count() > 0:
                    item.click()
                    page.wait_for_timeout(1200)
                    return session.session_id in getattr(page, "url", "") \
                        and self._find_input(page) is not None
            except Exception:
                return False
            return False
        current = self._session_from_link(session.title, url)
        if current is None or current.session_id != session.session_id:
            return False
        return self._find_input(page) is not None

    def prepare_new_session(self, page, mode: str) -> None:
        print(f"\n未找到 {self.canonical_session_title(mode)}，准备新会话...")
        clicked = False
        for hint in self.NEW_CHAT_HINTS:
            try:
                pattern = re.compile(re.escape(hint), re.IGNORECASE)
                button = page.get_by_role("button", name=pattern)
                if button.count() > 0:
                    button.first.click()
                    clicked = True
                    break
                link = page.get_by_role("link", name=pattern)
                if link.count() > 0:
                    link.first.click()
                    clicked = True
                    break
            except Exception:
                continue
        page.wait_for_timeout(1500)
        if not clicked:
            print("未找到显式的新对话按钮；将直接在输入框发送，多数网站会自动创建会话。")
        if self._find_input(page) is None:
            raise RuntimeError("无法打开新会话输入框")

    def current_session(self, page, title="") -> Optional[SessionRef]:
        ref = self._session_from_link(title, getattr(page, "url", ""))
        if ref is not None:
            return ref
        # SPA 回退：当前激活会话的 data-session-id
        try:
            session_id = page.evaluate(
                '() => { const el = document.querySelector('
                "'[data-session-active=\"true\"][data-session-id]');"
                ' return el ? el.getAttribute("data-session-id") : ""; }'
            )
        except Exception:
            session_id = ""
        if not session_id:
            return None
        href = getattr(page, "url", "") or self.base_url
        if session_id not in href:
            # 仅作记录；打开 SPA 会话时走 data-session-id 点击
            href = self.base_url.rstrip("/") + "/chat/" + session_id
        try:
            return SessionRef(
                provider=self.name,
                session_id=session_id,
                title=" ".join(str(title or "").split())[:100],
                href=href,
            )
        except ValueError:
            return None

    # ========================================================
    # 重命名会话
    # ========================================================

    def rename_session(self, page, session: SessionRef, title: str) -> SessionRef:
        if self._is_spa_ref(session):
            return self._rename_spa_session(page, session, title)
        try:
            link = page.locator(f'a[href*="{session.session_id}"]').first
            if link.count() == 0:
                raise RuntimeError("侧栏中找不到该会话入口")
            link.hover()
            page.wait_for_timeout(300)
            clicked = False
            container = link.locator("xpath=..")
            for hint in ("重命名", "rename", "编辑", "edit", "更多", "more"):
                pattern = re.compile(re.escape(hint), re.IGNORECASE)
                button = container.get_by_role("button", name=pattern)
                if button.count() == 0:
                    button = page.get_by_role("button", name=pattern)
                if button.count() > 0:
                    button.first.click()
                    clicked = True
                    break
            if not clicked:
                raise RuntimeError("找不到重命名入口")
            self._fill_name_input(page, title)
            page.wait_for_timeout(800)
        except Exception as exc:
            raise RuntimeError(f"通用重命名失败：{exc}") from exc
        print(f"✅ 新会话已重命名为：{title}")
        return SessionRef(
            provider=session.provider,
            session_id=session.session_id,
            title=title,
            href=session.href,
        )

    def _rename_spa_session(self, page, session: SessionRef, title: str) -> SessionRef:
        """SPA 重命名：悬停会话条目 → 点更多按钮 → 菜单选重命名 → 输入新名称。"""
        try:
            item = page.locator(f'[data-session-id="{session.session_id}"]').first
            if item.count() == 0:
                raise RuntimeError("侧栏中找不到该会话")
            item.scroll_into_view_if_needed()
            item.hover()
            page.wait_for_timeout(400)
            button = item.locator("button").first
            if button.count() == 0 or not button.is_visible():
                raise RuntimeError("悬停后没有出现会话操作按钮")
            button.click()
            page.wait_for_timeout(600)
            option = None
            menu_items = page.locator('[role="menuitem"]')
            for index in range(min(menu_items.count(), 12)):
                candidate = menu_items.nth(index)
                text = (candidate.inner_text() or "").strip()
                if any(h in text for h in ("重命名", "Rename", "rename", "编辑")):
                    option = candidate
                    break
            if option is None:
                for hint in ("重命名", "Rename"):
                    fallback = page.locator(f'text={hint}').first
                    if fallback.count() > 0 and fallback.is_visible():
                        option = fallback
                        break
            if option is None:
                raise RuntimeError("操作菜单里没有重命名入口")
            option.click()
            page.wait_for_timeout(400)
            self._fill_name_input(page, title)
            # 验证：条目文本应变成新名称
            deadline = time.monotonic() + 5
            actual = ""
            while time.monotonic() < deadline:
                page.wait_for_timeout(300)
                actual = " ".join((item.inner_text() or "").split())
                if title in actual:
                    break
        except Exception as exc:
            raise RuntimeError(f"SPA 会话重命名失败：{exc}") from exc
        print(f"✅ 新会话已重命名为：{title}")
        return SessionRef(
            provider=session.provider,
            session_id=session.session_id,
            title=title,
            href=session.href,
        )

    def _fill_name_input(self, page, title: str) -> None:
        name_input = page.locator('input[type="text"]:visible').first
        if name_input.count() > 0:
            name_input.fill(title)
            name_input.press("Enter")
            return
        editable = page.locator('[contenteditable="true"]:visible').first
        if editable.count() > 0:
            editable.click()
            page.keyboard.press("ControlOrMeta+a")
            page.keyboard.insert_text(title)
            page.keyboard.press("Enter")
            return
        raise RuntimeError("找不到重命名输入框")

    # ========================================================
    # 发送消息
    # ========================================================

    def send_message(self, page, question, image_path=None):
        image_paths: List[str] = []
        if image_path:
            if isinstance(image_path, str):
                image_paths = [image_path]
            elif isinstance(image_path, (list, tuple)):
                image_paths = [str(item) for item in image_path]
            else:
                raise TypeError("image_path 必须是字符串、list 或 tuple")
        for path in image_paths:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"图片不存在：{path}")

        # 发送前的页面回答快照：用于区分"旧内容"和"新回复"
        try:
            self._pre_send_text = self._extract_answer_text(page, question) or ""
        except Exception:
            self._pre_send_text = ""

        text_input = self._find_input(page)
        if text_input is None:
            raise RuntimeError(
                "未找到可用输入框（textarea / contenteditable / textbox 都没有）"
            )
        text_input.click()

        if image_paths:
            self._upload_images(page, image_paths)
            page.wait_for_timeout(800)

        if self._input_tag(text_input) == "textarea":
            text_input.fill(question)
        else:
            # contenteditable / role=textbox 无法用 fill，用键盘插入
            page.keyboard.insert_text(question)
        page.wait_for_timeout(300)
        text_input.press("Enter")
        print(f"已发送: {question}")

    def _upload_images(self, page, image_paths):
        # 部分网站需要先点开附件按钮才挂载 input[type=file]
        for hint in ("图片", "附件", "照片", "photo", "image", "attach", "clip"):
            try:
                button = page.get_by_role(
                    "button", name=re.compile(re.escape(hint), re.IGNORECASE)
                )
                if button.count() > 0:
                    button.first.click()
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue
        file_input = page.locator('input[type="file"]').first
        if file_input.count() == 0:
            raise RuntimeError(
                "该网站找不到图片上传入口，通用适配器暂不能给它发送图片"
            )
        file_input.set_input_files(list(image_paths))
        page.wait_for_timeout(1000)

    def _click_send_button(self, page, input_locator) -> bool:
        """在输入框附近找发送按钮并点击；找不到返回 False 让调用方按 Enter。"""
        hints = json.dumps(self.SEND_HINTS, ensure_ascii=False)
        stops = json.dumps(self.STOP_HINTS, ensure_ascii=False)
        try:
            handle = input_locator.element_handle()
            if handle is None:
                return False
            return bool(page.evaluate(
                """
                (input, hints, stops) => {
                    let container = input;
                    for (let i = 0; i < 4 && container.parentElement; i++) {
                        container = container.parentElement;
                    }
                    const buttons = container.querySelectorAll(
                        'button, [role="button"]'
                    );
                    let best = null;
                    for (const btn of buttons) {
                        const label = (
                            (btn.getAttribute('aria-label') || '') + ' ' +
                            (btn.getAttribute('title') || '') + ' ' +
                            (btn.innerText || '')
                        ).toLowerCase().trim();
                        if (!label) continue;
                        if (stops.some(w => label.includes(w.toLowerCase()))) continue;
                        if (hints.some(w => label.includes(w.toLowerCase()))) {
                            best = btn;
                            break;
                        }
                    }
                    if (!best) return false;
                    best.click();
                    return true;
                }
                """, handle, json.loads(hints), json.loads(stops)))
        except Exception:
            return False

    # ========================================================
    # 等待 AI 回复
    # ========================================================

    # 组件签名 + 文本兜底的状态机：generating / send / unknown
    _STATUS_JS = """
    (hints) => {
        const lower = hints.map(w => w.toLowerCase());
        const vis = (el) => {
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) return false;
            const s = getComputedStyle(el);
            if (s.visibility === 'hidden' || s.display === 'none') return false;
            return r.bottom > 0 && r.right > 0 &&
                   r.top < window.innerHeight && r.left < window.innerWidth;
        };
        // 1) 千问按钮槽：同一个按钮在“发送消息 / 停止回答”之间切换
        //    （快回复可能抓不到“停止回答”，回到“发送消息”即视为完成）
        const slot = document.querySelector('[data-session-switch-target="send-query"]');
        if (slot && vis(slot)) {
            const label = (slot.getAttribute('aria-label') || '').trim();
            if (/停止/.test(label)) return 'generating';
            if (/发送/.test(label)) return 'send';
        }
        // 2) 组件签名：生成中是 10px 黑方块（■），空闲是 sendChat 图标
        const square = [...document.querySelectorAll('span[class*="bg-black-button"]')]
            .find(el => {
                const r = el.getBoundingClientRect();
                if (!(r.width > 0 && r.width <= 16 && r.height > 0 && r.height <= 16)) {
                    return false;
                }
                // 隐藏的停止按钮副本（opacity:0 占位）不能算生成中
                const s = getComputedStyle(el);
                return s.visibility !== 'hidden' && s.display !== 'none' &&
                       s.opacity !== '0';
            });
        if (square) return 'generating';
        for (const use of document.querySelectorAll('use')) {
            const ref = use.getAttribute('xlink:href') || use.getAttribute('href') || '';
            if (!/sendChat/i.test(ref)) continue;
            const holder = use.closest('button, [role="button"]') || use.closest('svg') || use;
            if (vis(holder)) return 'send';
        }
        // 3) 文本提示兜底
        const nodes = document.querySelectorAll(
            'button, [role="button"], [aria-label]'
        );
        for (const el of nodes) {
            if (!vis(el)) continue;
            const label = (
                (el.getAttribute('aria-label') || '') + ' ' +
                (el.getAttribute('title') || '') + ' ' +
                (el.getAttribute('class') || '') + ' ' +
                (el.innerText || '')
            ).toLowerCase();
            if (!label.trim()) continue;
            if (lower.some(w => label.includes(w))) return 'generating';
        }
        // 4) loading 类兜底
        const busy = document.querySelector(
            '[class*="generating"], [class*="loading"], [class*="typing"]'
        );
        return (busy && vis(busy)) ? 'generating' : 'unknown';
    }
    """

    def _status(self, page) -> str:
        hints = json.dumps(self.STOP_HINTS, ensure_ascii=False)
        try:
            return str(page.evaluate(self._STATUS_JS, json.loads(hints)))
        except Exception:
            return "unknown"

    def _is_generating(self, page) -> bool:
        return self._status(page) == "generating"

    def _click_send_icon(self, page) -> bool:
        """点击组件签名的发送图标（如千问 qwpcicon-sendChat）。"""
        try:
            return bool(page.evaluate(
                """
                () => {
                    for (const use of document.querySelectorAll('use')) {
                        const ref = use.getAttribute('xlink:href') ||
                                    use.getAttribute('href') || '';
                        if (!/sendChat/i.test(ref)) continue;
                        const btn = use.closest('button, [role="button"]') ||
                                    use.closest('svg');
                        if (!btn) continue;
                        const r = btn.getBoundingClientRect();
                        if (!r.width || !r.height) continue;
                        btn.click();
                        return true;
                    }
                    return false;
                }
                """))
        except Exception:
            return False

    def wait_for_response(self, page, question=None, timeout=None) -> bool:
        """状态机 + 内容稳定双重判定，极速模式友好。

        采样节奏：前 3 秒每 0.1 秒高频采样（极速回复秒回也能抓住），
        之后每 3 秒慢采样等内容稳定；生成中（■）消失后再做最终确认。
        """
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT
        start = time.time()
        pre_text = self.normalize_text(getattr(self, "_pre_send_text", ""))

        # 阶段 0：3 秒高频采样，抓住极速回复与生成方块
        saw_generating = False
        retried_send = False
        fast_deadline = start + 3.0
        while time.time() < fast_deadline and (time.time() - start) < timeout:
            self._ensure_no_wall(page, start)
            if self._status(page) == "generating":
                saw_generating = True
                break
            text = self._extract_answer_text(page, question)
            if self.normalize_text(text) and self.normalize_text(text) != pre_text:
                break
            if not retried_send and (time.time() - start) > 1.5:
                # Enter 可能没触发发送，补点一次发送图标
                if self._click_send_icon(page):
                    retried_send = True
                    print("已补点发送图标，继续等待。")
            time.sleep(0.1)

        # 阶段 1：生成中 → 等待 ■ 消失（连续两次确认）
        if saw_generating:
            print("检测到生成中（■），等待完成…")
            while (time.time() - start) < timeout:
                self._ensure_no_wall(page, start)
                if self._status(page) != "generating":
                    time.sleep(0.3)
                    if self._status(page) != "generating":
                        break
                time.sleep(0.3)

        # 阶段 2：内容稳定检测（3 秒间隔，连续 2 轮一致）
        print("等待内容稳定…")
        last_text = ""
        stable = 0
        while (time.time() - start) < timeout:
            self._ensure_no_wall(page, start)
            if self._status(page) == "generating":
                saw_generating = True
                stable = 0
                time.sleep(0.5)
                continue
            text = self._extract_answer_text(page, question)
            normalized = self.normalize_text(text)
            if normalized and normalized != pre_text:
                if text == last_text:
                    stable += 1
                    if stable >= 2:
                        time.sleep(self.FINAL_RENDER_DELAY)
                        print("\n✅ AI 回复已完成")
                        return True
                else:
                    stable = 0
                last_text = text
            else:
                stable = 0
            time.sleep(3.0)

        print(f"\n⚠️ 等待超过 {timeout} 秒，停止等待")
        return False

    # ========================================================
    # 人工代问模式：用户自己提问，后台监听并提取
    # ========================================================

    def _read_input_text(self, page) -> str:
        try:
            return str(page.evaluate(
                """
                () => {
                    const el = document.querySelector('textarea') ||
                               document.querySelector('[contenteditable="true"]');
                    if (!el) return '';
                    if (el.tagName === 'TEXTAREA') return el.value || '';
                    const text = (el.innerText || '').trim();
                    // 有些网站把占位符渲染成真实文本（如千问输入框），
                    // 与占位文案一致时视为空输入
                    const ph = (el.getAttribute('data-placeholder') ||
                                el.getAttribute('aria-placeholder') || '').trim();
                    if (ph && text === ph) return '';
                    return text;
                }
                """) or "").strip()
        except Exception:
            return ""

    def _last_question_text(self, page) -> str:
        try:
            return str(page.evaluate(
                """
                () => {
                    const vis = el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    // 千问等网站的问题正文卡片，文本最干净（不含操作图标）
                    const cards = [...document.querySelectorAll(
                        '.question-text-card, [class*="question-text"]'
                    )].filter(el => vis(el) &&
                                   !el.closest('aside, [class*="sidebar"], nav'));
                    if (cards.length) {
                        return (cards[cards.length - 1].innerText || '')
                            .trim().slice(0, 500);
                    }
                    const els = [...document.querySelectorAll('[class*="question"]')]
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 &&
                                   !el.closest('aside, [class*="sidebar"], nav');
                        });
                    if (!els.length) return '';
                    return (els[els.length - 1].innerText || '').trim().slice(0, 500);
                }
                """) or "").strip()
        except Exception:
            return ""

    # ========================================================
    # 千问专用：回答正文在 #qk-markdown-react，带完成标记 class
    # ========================================================

    def _is_qianwen(self) -> bool:
        return "qianwen" in self._host_of(str(self.base_url or ""))

    _QW_MARKDOWN_JS = """
    () => {
        const blocks = [...document.querySelectorAll(
            '#qk-markdown-react, .qk-markdown-react')]
            .filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
        if (!blocks.length) return {text: '', complete: false};
        const el = blocks[blocks.length - 1];
        const clone = el.cloneNode(true);
        // 反馈工具栏（复制/点赞等）不属于正文
        clone.querySelectorAll('[data-answer-feedback-toolbar]')
            .forEach(n => n.remove());
        // 末尾"要不要接着聊聊…"追问段：最后一个 hr 之后的内容全部去掉
        const hrs = [...clone.querySelectorAll('hr')];
        if (hrs.length) {
            const lastHr = hrs[hrs.length - 1];
            const tail = [];
            let n = lastHr.nextSibling;
            while (n) { tail.push(n); n = n.nextSibling; }
            tail.forEach(x => x.remove());
            lastHr.remove();
        }
        return {
            text: (clone.innerText || '').trim(),
            complete: el.className.includes('qk-markdown-complete'),
        };
    }
    """

    def _qianwen_markdown(self, page) -> dict:
        try:
            result = page.evaluate(self._QW_MARKDOWN_JS)
            if isinstance(result, dict):
                return {
                    "text": str(result.get("text") or ""),
                    "complete": bool(result.get("complete")),
                }
        except Exception:
            pass
        return {"text": "", "complete": False}

    def _extract_qianwen_answer(self, page) -> str:
        return self._qianwen_markdown(page)["text"]

    def _wait_content_stable(self, page, question, start, timeout) -> bool:
        """生成结束后等内容稳定；期间若又开始生成则返回 False 让外层重新监听。

        千问答复带 qk-markdown-complete 完成标记：正文块存在但未完成时
        视为仍在生成，防止把流式输出误判为已结束。
        """
        last_text = ""
        stable = 0
        qianwen = self._is_qianwen()
        while (time.time() - start) < timeout:
            if self._status(page) == "generating":
                return False
            if qianwen:
                block = self._qianwen_markdown(page)
                if block["text"] and not block["complete"]:
                    stable = 0
                    last_text = ""
                    time.sleep(0.5)
                    continue
                text = block["text"]
            else:
                text = self._extract_answer_text(page, question)
            normalized = self.normalize_text(text)
            if normalized:
                if text == last_text:
                    stable += 1
                    if stable >= 2:
                        time.sleep(self.FINAL_RENDER_DELAY)
                        return True
                else:
                    stable = 0
                last_text = text
            else:
                stable = 0
            time.sleep(2.0)
        return False

    def monitor_human_chat(self, page, timeout: int = 600) -> dict:
        """人工代问：用户在浏览器里自行输入问题并发送，工具监听状态并取回答。

        对人机验证友好：出现验证时提示用户手动完成并刷新页面，刷新后
        继续监听（页面刷新不会中断监听循环）。

        武装条件：状态为生成中，且输入框里有真实文字或页面已有问题卡片。
        页面刷新/加载中的"生成中"是噪声（输入为空、没有问题卡片），不会
        误触发；千问下占位符文本（"向千问提问"）也被视为空输入。
        """
        start = time.time()
        last_question = ""
        armed = False
        wall_hinted = False
        print(
            "\n请在打开的浏览器窗口里自行输入问题并发送，我来监听回答。"
            "\n出现人机验证时请手动完成并刷新页面，我会继续监听。"
        )
        while (time.time() - start) < timeout:
            if self._has_wall(page):
                page.wait_for_timeout(1500)
                if self._has_wall(page):
                    if not wall_hinted:
                        print(
                            "\n⚠️ 出现人机验证：请在浏览器里完成验证并刷新页面，"
                            "然后重新提问，我会继续监听……"
                        )
                        wall_hinted = True
                    page.wait_for_timeout(2000)
                    continue
            else:
                wall_hinted = False

            status = self._status(page)

            if not armed:
                current = self._read_input_text(page)
                if status != "generating":
                    time.sleep(0.3)
                    continue
                # 生成中：区分"用户真的发送了"和"页面刷新/加载噪声"
                question = ""
                if current:
                    question = current
                else:
                    # 输入已清空但问题卡片已渲染（用户发送后或刷新续答）
                    question = self._last_question_text(page)
                if not question:
                    time.sleep(0.3)
                    continue
                armed = True
                last_question = question
                print(
                    f"\n已检测到你发送问题：{last_question[:60]}，"
                    "等待回答完成…"
                )
                time.sleep(0.3)
                continue

            # 已武装：等待回答完成
            if status == "generating":
                time.sleep(0.3)
                continue

            print("回答已结束，等待内容稳定…")
            if self._wait_content_stable(page, last_question, start, timeout):
                answer, full = self.extract_latest_answer(page, last_question)
                print("\n✅ 已提取回答")
                return {
                    "question": last_question,
                    "answer": answer,
                    "full_content": full,
                }
            # 稳定等待期间又开始生成（用户追问了），回到监听
            armed = False
            time.sleep(0.3)

        raise RuntimeError(
            f"监听超时（{timeout} 秒）：没有检测到完整的提问-回答过程"
        )

    # ========================================================
    # 提取最新回复
    # ========================================================

    # 结构兜底：不依赖任何 class，以问题文本为锚点找它之后的正文块
    _STRUCTURAL_EXTRACT_JS = """
    (questionNorm) => {
        const bad = 'aside, nav, [role="navigation"], [class*="sidebar"], ' +
            '[class*="sider"], header, footer, [class*="footer"]';
        const skipSelf = 'textarea, [contenteditable="true"], button, input, select';
        const noise = /内容由|AI生成|disclaimer|滑动查看|点击展开/i;
        const modelPill = /^(Qwen|千问|GPT|Claude|Kimi|DeepSeek|文心|豆包|通义|混元|Gemini|元宝)/i;
        const vis = (el) => {
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) return false;
            const s = getComputedStyle(el);
            return s.visibility !== 'hidden' && s.display !== 'none';
        };
        // 1) 锚定问题元素：取文本与问题一致的最深（最后出现）的元素
        let qEl = null;
        document.querySelectorAll('div, section, p, span, li').forEach(el => {
            if (el.closest(bad) || el.querySelector(skipSelf)) return;
            const t = (el.innerText || '').trim();
            if (!t) return;
            const norm = t.replace(/\\s+/g, ' ').trim();
            if (norm === questionNorm ||
                (questionNorm.length >= 8 &&
                 norm.startsWith(questionNorm.slice(0, 30)) &&
                 norm.length <= questionNorm.length + 8)) {
                qEl = el;
            }
        });
        if (!qEl) return '';
        const inputEl = document.querySelector('[contenteditable="true"], textarea');
        // 2) 从问题元素之后收集"叶子文本块"（可见、非侧栏、非控件）；
        //    走到输入框即停——输入框之后都是页面 chrome
        const blocks = [];
        const seen = new Set();
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let started = false;
        let node;
        while ((node = walker.nextNode())) {
            if (!started) {
                if (qEl.contains(node)) started = true;
                continue;
            }
            if (inputEl && inputEl.contains(node)) break;
            const el = node.parentElement;
            if (!el || el.closest(bad) || el.closest(skipSelf)) continue;
            if (el.querySelector && el.querySelector(skipSelf)) continue;
            let leaf = el;
            while (leaf.firstElementChild && (leaf.firstElementChild.innerText || '').trim()) {
                leaf = leaf.firstElementChild;
            }
            if (seen.has(leaf)) continue;
            seen.add(leaf);
            if (!vis(leaf)) continue;
            const text = (leaf.innerText || '').trim();
            if (text.length < 1 || noise.test(text)) continue;
            if (text.length <= 20 && modelPill.test(text)) continue;
            blocks.push(text);
            if (blocks.length > 300) break;
        }
        if (!blocks.length) return '';
        // 3) 回答 = 最后一个内容块（聊天布局里回答位于问题之后、页脚之前）
        return blocks[blocks.length - 1];
    }
    """

    def _extract_structural(self, page, question) -> str:
        question_normalized = self.normalize_text(question or "")
        if not question_normalized:
            return ""
        try:
            return str(page.evaluate(
                self._STRUCTURAL_EXTRACT_JS, question_normalized) or ""
            ).strip()
        except Exception:
            return ""

    _QUESTION_ROUND_EXTRACT_JS = r"""
    (questionNorm) => {
        if (!questionNorm) return '';
        const norm = value => (value || '').replace(/\s+/g, ' ').trim();
        const visible = el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return !!rect.width && !!rect.height &&
                style.visibility !== 'hidden' && style.display !== 'none';
        };
        const questionSelectors = [
            '[class*="wrapper-question"]', '[class*="chat-question"]',
            '[class*="user-message"]', '[class*="user_message"]',
            '[data-role="user"]', '[data-message-author-role="user"]'
        ].join(',');
        const answerSelectors = [
            '[class*="wrapper-answer"]', '[class*="chat-answer"]',
            '[class*="answer-content"]', '[class*="assistant"]',
            '[class*="bot-message"]', '[class*="model-message"]',
            '[data-role="assistant"]', '[data-message-author-role="assistant"]'
        ].join(',');
        const questions = Array.from(document.querySelectorAll(questionSelectors));
        let questionEl = null;
        for (const el of questions) {
            const text = norm(el.innerText);
            if (text === questionNorm ||
                (questionNorm.length >= 8 && text.includes(questionNorm))) {
                questionEl = el;
            }
        }
        if (!questionEl) return '';

        // Walk upward only until the smallest container holding both the
        // question and its answer is found. This avoids choosing a longer
        // answer from an older conversation round.
        let round = questionEl;
        for (let depth = 0; round && round !== document.body && depth < 10;
             depth++, round = round.parentElement) {
            const answers = Array.from(round.querySelectorAll(answerSelectors))
                .filter(el => visible(el) && !el.contains(questionEl));
            if (!answers.length) continue;
            for (let i = answers.length - 1; i >= 0; i--) {
                const text = (answers[i].innerText || '').trim();
                if (norm(text) && norm(text) !== questionNorm) return text;
            }
        }
        return '';
    }
    """

    def _extract_question_round(self, page, question) -> str:
        """Return the answer paired with this question, including fast replies."""
        question_normalized = self.normalize_text(question or "")
        if not question_normalized:
            return ""
        try:
            return str(page.evaluate(
                self._QUESTION_ROUND_EXTRACT_JS, question_normalized
            ) or "").strip()
        except Exception:
            return ""

    def _extract_answer_text(self, page, question=None) -> str:
        # 千问：回答正文固定在 #qk-markdown-react，优先走专用提取
        if self._is_qianwen():
            qw = self._extract_qianwen_answer(page)
            if qw:
                return qw
        paired = self._extract_question_round(page, question)
        if paired:
            return paired
        hints = json.dumps(self.REPLY_CLASS_HINTS, ensure_ascii=False)
        try:
            candidates = page.evaluate(
                """
                (hints) => {
                    const bad = 'aside, nav, [role="navigation"], ' +
                        '[class*="sidebar"], [class*="history"], header, footer';
                    const selectors = hints.map(h => `[class*="${h}"]`).join(',');
                    const out = [];
                    document.querySelectorAll(selectors).forEach(el => {
                        if (el.closest(bad)) return;
                        if (el.querySelector('textarea, [contenteditable="true"]')) return;
                        const text = (el.innerText || '').trim();
                        if (text.length < 2) return;
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 10) return;
                        out.push(text);
                    });
                    return out;
                }
                """, json.loads(hints))
        except Exception:
            candidates = []
        question_normalized = self.normalize_text(question or "")
        best = ""
        for text in candidates or []:
            normalized = self.normalize_text(str(text))
            if not normalized:
                continue
            # 跳过回显出来的问题本身
            if question_normalized and normalized == question_normalized:
                continue
            if len(normalized) >= len(self.normalize_text(best)):
                best = str(text)
        if best:
            return best.strip()
        # class 特征全部未命中：退化为以问题为锚点的结构提取
        return self._extract_structural(page, question)

    def extract_latest_answer(self, page, question):
        try:
            result = self._extract_answer_text(page, question)
            full_body = page.inner_text("body")
            if result:
                return (result, full_body)
            return ("未获取到有效AI回复", full_body)
        except Exception as exc:
            try:
                return ("提取失败", page.inner_text("body"))
            except Exception:
                return ("提取失败", "")

    # ========================================================
    # 文本规范化（仅用于文本匹配，不用于判断生成是否完成）
    # ========================================================

    @staticmethod
    def normalize_text(text: str) -> str:
        if not text:
            return ""
        for codepoint in (0x200B, 0x200C, 0x200D, 0xFEFF):
            text = text.replace(chr(codepoint), "")
        return " ".join(text.split())
