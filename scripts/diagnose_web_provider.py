#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""五次探针：全量定位回答容器（排除侧栏，取全部匹配）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_relay import (  # noqa: E402
    CODEX_HOME,
    create_browser_context,
    provider_state_file,
    decrypt_provider_state,
)
from config_manager import apply_config_to_environment  # noqa: E402

FIND_ALL_OUTSIDE_SIDEBAR = """
(keyword) => {
    const results = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        const text = node.textContent || '';
        if (!text.includes(keyword)) continue;
        let el = node.parentElement;
        if (el && el.closest('aside, [class*="sidebar"], [class*="sider"], nav')) continue;
        const chain = [];
        let cur = el;
        while (cur && cur !== document.body && chain.length < 8) {
            chain.push(cur.tagName.toLowerCase() + ' .' + String(cur.getAttribute('class') || '').slice(0, 85));
            cur = cur.parentElement;
        }
        results.push({text: text.trim().slice(0, 60), chain});
        if (results.length >= 6) break;
    }
    return results;
}
"""

MESSAGE_ROUNDS = """
() => {
    const rounds = document.querySelectorAll('[class*="chat-round"]');
    const out = [];
    rounds.forEach(r => {
        const cls = String(r.getAttribute('class') || '').slice(0, 70);
        const text = (r.innerText || '').trim();
        out.push({cls, len: text.length, sample: text.slice(0, 80)});
    });
    return {count: rounds.length, rounds: out.slice(-4)};
}
"""


def main() -> int:
    apply_config_to_environment()
    from playwright.sync_api import sync_playwright

    state_file = provider_state_file("qianwen", CODEX_HOME)
    temporary = decrypt_provider_state(state_file, CODEX_HOME)

    with sync_playwright() as p:
        browser, context = create_browser_context(p, temporary)
        page = context.new_page()
        page.goto("https://www.qianwen.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # 用问题中不出现的词作为答案探测词
        keyword = "猕猴桃"
        question = "请说出一种水果名称，只回复名称本身"
        box = page.locator('[contenteditable="true"]:visible').first
        box.click()
        page.keyboard.insert_text(question)
        box.press("Enter")
        print("已发送:", question, "探测词:", keyword)

        found = False
        for _ in range(45):
            page.wait_for_timeout(1000)
            results = page.evaluate(FIND_ALL_OUTSIDE_SIDEBAR, keyword)
            # 侧栏标题也会包含探测词，要求非侧栏匹配至少 1 个
            if results:
                found = True
                break
        print(f"\n===== 非侧栏匹配（found={found}） =====")
        print(json.dumps(page.evaluate(FIND_ALL_OUTSIDE_SIDEBAR, keyword), ensure_ascii=False, indent=1))
        print("\n===== chat-round 结构 =====")
        print(json.dumps(page.evaluate(MESSAGE_ROUNDS), ensure_ascii=False, indent=1))
        print("\nURL:", page.url)

        browser.close()
    if temporary != state_file:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
