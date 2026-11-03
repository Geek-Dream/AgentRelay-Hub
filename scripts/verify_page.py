#!/usr/bin/env python3
"""Really open AgentRelay's config page in Chromium and save visual evidence."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
if (SCRIPT_DIR / "page_verification.py").is_file():
    _spec = importlib.util.spec_from_file_location("agentrelay_page_verification", SCRIPT_DIR / "page_verification.py")
    page_verification = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(page_verification)
else:
    sys.path.insert(0, str(PROJECT_ROOT))
    from scripts import page_verification


def _sample_config() -> dict[str, Any]:
    return {
        "default_provider": "demo-web",
        "web_providers": [{"id": "demo-web", "name": "演示网页模型", "base_url": "https://example.invalid",
                           "enabled": True, "logged_in": False, "adapter": "generic"}],
        "local_providers": [], "api_providers": [], "agents": {},
        "install_targets": ["codex"], "fusion": {"enabled": True},
        "commander": {"enabled": False},
    }


def _check_page(page, output: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    screenshots: list[str] = []
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.on("console", lambda message: errors.append(f"console.{message.type}: {message.text}")
            if message.type == "error" else None)
    page.on("requestfailed", lambda request: errors.append(f"requestfailed: {request.url}"))
    page.wait_for_selector("body", state="visible")
    is_config = bool(page.locator("#overview.view.active").count())
    if is_config:
        page.wait_for_selector("#overview.view.active")
        for tab in ("commander", "web", "local", "api", "security", "overview"):
            page.locator(f'[data-tab="{tab}"]').click()
            page.wait_for_selector(f"#{tab}.view.active")
        page.locator('[data-theme-choice="light"]').click()
        page.wait_for_function("document.documentElement.dataset.theme === 'light'")
        page.locator('[data-theme-choice="dark"]').click()
        page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        page.locator('[data-tab="overview"]').click()
    if not (page.locator("body").inner_text() or "").strip():
        errors.append("page body is empty")
    for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(700)
        if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
            errors.append(f"{name}: horizontal page overflow")
        if is_config and name == "desktop":
            page.locator('[data-theme-choice="light"]').click()
            page.wait_for_function("document.documentElement.dataset.theme === 'light'")
            page.wait_for_timeout(500)
            light = output / "config-light.png"
            page.screenshot(path=str(light), full_page=True)
            screenshots.append(str(light))
            page.locator('[data-tab="web"]').click()
            page.locator('#web .head .btn.primary').click()
            page.wait_for_selector("#web-form #f-id", state="visible")
            page.locator("#f-id").fill("demo-web")
            page.locator("#f-name").fill("演示网页模型")
            page.locator('[data-theme-choice="dark"]').click()
            page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
            page.wait_for_timeout(700)
            form = output / "config-web-form.png"
            page.screenshot(path=str(form), full_page=True)
            screenshots.append(str(form))
            page.locator('[data-tab="overview"]').click()
        target = output / f"config-{name}.png"
        page.screenshot(path=str(target), full_page=True)
        if not target.is_file() or target.stat().st_size < 1000:
            errors.append(f"{name}: screenshot missing or empty")
        screenshots.append(str(target))
    return errors, screenshots


def _config_page() -> str:
    try:
        from scripts.config_web import build_config_page
    except ImportError:
        spec = importlib.util.spec_from_file_location("agentrelay_config_web", SCRIPT_DIR / "config_web.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        build_config_page = module.build_config_page
    return build_config_page(_sample_config())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="bind evidence to this Hook session")
    parser.add_argument("--url", help="existing local page; default renders current config_web.py")
    parser.add_argument("--output", type=Path, help="evidence directory (session mode has a safe default)")
    args = parser.parse_args()
    if args.session:
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        output = args.output or codex_home / "agent_relay_tracker" / "page-verification" / args.session / "evidence"
    elif args.output:
        output = args.output
    else:
        parser.error("standalone checks require --output")
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    screenshots: list[str] = []
    browser_name = ""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser_name = browser.browser_type.name
            try:
                content = None if args.url else _config_page()
                if args.session:
                    files = page_verification.read_state(codex_home, args.session).get("files") or {}
                    if any(Path(value).name == "config_web.py" for value in files):
                        content = _config_page()
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                if content is not None:
                    page.set_content(content, wait_until="load")
                else:
                    if not args.url.startswith(("http://127.0.0.1", "http://localhost", "file://")):
                        raise ValueError("only local pages can be checked")
                    page.goto(args.url, wait_until="networkidle")
                errors, screenshots = _check_page(page, output)
            finally:
                browser.close()
    except Exception as exc:  # The JSON failure is the deliverable; never claim a skipped check passed.
        errors.append(f"{type(exc).__name__}: {exc}")
    result = {"status": "failed" if errors else "passed", "browser": browser_name,
              "screenshots": screenshots, "errors": errors, "checked_at": time.time(),
              "url": args.url or ""}
    if args.session:
        state = page_verification.read_state(codex_home, args.session)
        result["files"] = state.get("files") or {}
        state["verification"] = result
        page_verification.write_state(codex_home, args.session, state)
        result["gate"] = page_verification.verify_evidence(codex_home, args.session) or "passed"
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
