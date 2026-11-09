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


def _fill_editable_field(page) -> str | None:
    """Fill a genuinely editable field; read-only date widgets are not inputs to mutate."""
    candidates = page.locator(
        "input:not([type=button]):not([type=submit]):not([type=checkbox]):"
        "not([type=radio]):not([type=hidden]):not([disabled]):not([readonly]), "
        "textarea:not([disabled]):not([readonly])"
    )
    failures = []
    for index in range(candidates.count()):
        field = candidates.nth(index)
        try:
            if not field.is_visible() or not field.is_editable():
                continue
            if str(field.get_attribute("aria-readonly") or "").lower() == "true":
                continue
            field.fill("AgentRelay verification", timeout=3000)
            if field.input_value() != "AgentRelay verification":
                failures.append("输入框填写后内容不一致")
                continue
            return "输入框已真实填写并触发 input/change"
        except Exception as exc:
            failures.append(str(exc))
    if failures:
        raise ValueError("可编辑输入框填写失败：" + failures[0].splitlines()[0][:300])
    return None


def _check_page(page, output: Path, target_selector: str = "", expected: dict[str, Any] | None = None,
                screenshots_enabled: bool = True, validate_expected: bool = False
                ) -> tuple[list[str], list[str], dict[str, Any] | None, list[dict[str, Any]]]:
    errors: list[str] = []
    screenshots: list[str] = []
    console_errors: list[str] = []
    layout_errors: list[str] = []

    def browser_error(value: str) -> None:
        console_errors.append(value)
        errors.append(value)

    page.on("pageerror", lambda error: browser_error(f"pageerror: {error}"))
    page.on("console", lambda message: browser_error(f"console.{message.type}: {message.text}")
            if message.type == "error" else None)
    page.on("requestfailed", lambda request: browser_error(f"requestfailed: {request.url}"))
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
        layout_errors.append("page body is empty")
        errors.append("page body is empty")
    for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(700)
        if page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"):
            layout_errors.append(f"{name}: horizontal page overflow")
            errors.append(f"{name}: horizontal page overflow")
        if is_config and name == "desktop" and screenshots_enabled:
            page.locator('[data-theme-choice="light"]').click()
            page.wait_for_function("document.documentElement.dataset.theme === 'light'")
            page.wait_for_timeout(500)
            light = output / "config-light.png"
            page_verification.wait_for_visual_settle(page)
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
            page_verification.wait_for_visual_settle(page)
            page.screenshot(path=str(form), full_page=True)
            screenshots.append(str(form))
            page.locator('[data-tab="overview"]').click()
        if screenshots_enabled:
            target = output / f"config-{name}.png"
            page_verification.wait_for_visual_settle(page)
            page.screenshot(path=str(target), full_page=True)
            if not target.is_file() or target.stat().st_size < 1000:
                errors.append(f"{name}: screenshot missing or empty")
            screenshots.append(str(target))
    target_snapshot = None
    if target_selector:
        properties = list(expected or {"display": "block", "color": "", "width": "", "height": ""})
        target_snapshot = _capture_target(page, target_selector, properties)
        if validate_expected and expected:
            for prop, wanted in expected.items():
                actual = target_snapshot.get("styles", {}).get(prop)
                if str(actual) != str(wanted):
                    errors.append(f"target: {prop} 期望 {wanted}，实际 {actual}")
        target_image = output / "target-element.png"
        if screenshots_enabled:
            page.locator(target_selector).first.screenshot(path=str(target_image))
            screenshots.append(str(target_image))
    interactive = page.locator("button, a, input, select, textarea")
    interactive_count = interactive.count()
    interaction_details = []
    interaction_passed = True
    if interactive_count and not is_config:
        try:
            button = page.locator("button:not([disabled]), input[type=button]:not([disabled]), input[type=submit]:not([disabled])").first
            if button.count():
                clicked = button.evaluate("""element => {
                    let clicked = false;
                    const listener = () => { clicked = true; };
                    element.addEventListener('click', listener, { once: true });
                    element.click();
                    element.removeEventListener('click', listener);
                    return clicked;
                }""")
                if not clicked:
                    raise ValueError("button click did not dispatch")
                interaction_details.append("按钮点击事件已触发")
            field_detail = _fill_editable_field(page)
            if field_detail:
                interaction_details.append(field_detail)
            elif not interaction_details:
                focus_target = page.locator("a, select").first
                if focus_target.count():
                    focus_target.focus(timeout=1000)
                    interaction_details.append("链接/选择框已聚焦")
                else:
                    interaction_details.append("页面仅含只读或不可填写控件；未执行变更")
        except Exception as exc:
            interaction_passed = False
            errors.append(f"interaction: {exc}")
            interaction_details.append(f"交互失败：{exc}")
    if is_config:
        interaction_details.append("六导航、双主题和 Web 表单已真实操作")
    nonvisual_checks = [
        {
            "check": "dom",
            "passed": not layout_errors,
            "actual": (
                "body 可见，桌面与移动视口无横向溢出"
                if not layout_errors else "；".join(layout_errors)
            ),
        },
        {
            "check": "console",
            "passed": not console_errors,
            "actual": "0 console/pageerror/requestfailed 错误" if not console_errors else "；".join(console_errors),
        },
        {
            "check": "diff",
            "passed": True,
            "actual": "浏览器检查后的页面文件 SHA-256 由会话门禁绑定；此处不伪造 diff 结果",
        },
        {
            "check": "interaction",
            "passed": interaction_passed,
            "actual": "；".join(interaction_details) if interaction_details else "页面无交互控件；DOM、布局和控制台已检查",
        },
    ]
    return errors, screenshots, target_snapshot, nonvisual_checks


def _capture_target(page, selector: str, properties: list[str]) -> dict[str, Any]:
    locator = page.locator(selector).first
    locator.wait_for(state="visible")
    styles = locator.evaluate("""(element, properties) => Object.fromEntries(properties.map(
        property => [property, getComputedStyle(element)[property]]))""", properties)
    box = locator.bounding_box() or {}
    return {"selector": selector, "styles": styles,
            "rect": {key: box.get(key, 0) for key in ("x", "y", "width", "height")}}


def _load_visual_review():
    if (SCRIPT_DIR / "visual_review.py").is_file():
        spec = importlib.util.spec_from_file_location("agentrelay_visual_review", SCRIPT_DIR / "visual_review.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    from scripts import visual_review
    return visual_review


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
    parser.add_argument("--session", help="bind evidence to this Hook session (auto-detected when omitted)")
    parser.add_argument("--url", help="existing local page; default renders current config_web.py")
    parser.add_argument("--output", type=Path, help="evidence directory (session mode has a safe default)")
    parser.add_argument("--target-selector", default="", help="CSS selector of the element being changed")
    parser.add_argument("--expected", default="{}", help="JSON object of expected computed style values")
    parser.add_argument("--after", action="store_true", help="compare the current target with the saved before snapshot")
    parser.add_argument("--invariant", action="append", default=[], help="CSS selector of an outside element that must not change")
    parser.add_argument("--no-screenshots", action="store_true", help="nonvisual mode: real Chromium checks without images")
    args = parser.parse_args()
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
    if not args.session and not args.output:
        args.session = page_verification.resolve_current_session(
            codex_home,
            cwd=os.getcwd(),
            environ=os.environ,
        )
    if args.session:
        args.session = page_verification._valid_session_id(args.session)
        if not args.session:
            parser.error("--session must contain only letters, numbers, '-' or '_'")
    if args.session:
        output = args.output or codex_home / "agent_relay_tracker" / "page-verification" / args.session / "evidence"
    elif args.output:
        output = args.output
    else:
        parser.error(
            "cannot resolve a unique current Hook session; pass --session SESSION_ID "
            "or use standalone mode with --output"
        )
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    screenshots: list[str] = []
    nonvisual_checks: list[dict[str, Any]] = []
    target_snapshot = None
    browser_name = ""
    invariants = []
    previous = {}
    try:
        expected = json.loads(args.expected or "{}")
    except ValueError as exc:
        parser.error(f"--expected 必须是 JSON 对象：{exc}")
    if not isinstance(expected, dict):
        parser.error("--expected 必须是 JSON 对象")
    if args.session:
        previous = page_verification.read_state(codex_home, args.session)
        if args.after and previous.get("target_before"):
            saved_expected = dict(previous.get("target_expected") or {})
            if expected and expected != saved_expected:
                parser.error("--after 的 --expected 必须与修改前逐项一致，不能更换或放宽目标契约")
            expected = saved_expected
            saved_selector = str((previous.get("target_before") or {}).get("selector") or "")
            if args.target_selector and saved_selector and args.target_selector != saved_selector:
                parser.error("--after 的 --target-selector 必须与修改前一致，不能验收另一个元素")
        if args.after and previous.get("invariants_before") and not args.invariant:
            args.invariant = [item["selector"] for item in previous["invariants_before"]]
        elif args.after and args.invariant:
            expected_selectors = [item["selector"] for item in previous.get("invariants_before") or []]
            if set(args.invariant) != set(expected_selectors):
                parser.error("--after 的 --invariant 必须与修改前逐项一致，不能增删目标外检查")
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
                errors, screenshots, target_snapshot, nonvisual_checks = _check_page(
                    page, output, args.target_selector, expected,
                    screenshots_enabled=not args.no_screenshots,
                    validate_expected=False,
                )
                if args.invariant:
                    invariants = _load_visual_review().capture_invariants(page, args.invariant)
            finally:
                browser.close()
    except Exception as exc:  # The JSON failure is the deliverable; never claim a skipped check passed.
        errors.append(f"{type(exc).__name__}: {exc}")
    result = {"status": "failed" if errors else "passed", "browser": browser_name,
              "session_id": args.session or "",
              "screenshots": screenshots, "errors": errors, "checked_at": time.time(),
              "url": args.url or "", "visual_review_required": True}
    if args.session:
        state = page_verification.read_state(codex_home, args.session)
        state["evidence_root"] = str(output.expanduser().resolve())
        current_files = {}
        for value in state.get("files") or {}:
            path = Path(value)
            if path.is_file():
                current_files[value] = {"sha256": page_verification._source_digest(path), "checked_at": time.time()}
        state["files"] = current_files
        result["files"] = current_files
        state["verification"] = result
        if args.target_selector and not args.after and not errors:
            for key in ("target_review", "unchanged_results", "visual_review", "cleanup_completed"):
                state.pop(key, None)
            state["target_before"] = target_snapshot
            state["target_expected"] = expected
            state["invariants_before"] = invariants
        if args.after and not errors:
            before = state.get("target_before")
            if not before:
                errors.append("缺少修改前目标快照，不能证明目标修改成功")
            elif not target_snapshot:
                errors.append("缺少 --target-selector 或目标元素不可见，不能完成目标验收")
            else:
                visual_review = _load_visual_review()
                state["target_review"] = visual_review.record_target_verification(
                    codex_home, args.session, before=before, after=target_snapshot,
                    expected=expected or state.get("target_expected") or {},
                    unchanged_checks=args.invariant or ["目标外布局、兄弟元素和其他模块无回归"],
                )
                if not state["target_review"]["target_verified"]:
                    errors.append("目标元素未达到预期状态")
                if state.get("invariants_before"):
                    state["unchanged_results"] = visual_review.verify_invariants(
                        state["invariants_before"], invariants)
                    if any(not item["passed"] for item in state["unchanged_results"]):
                        errors.append("目标外元素位置或尺寸发生变化")
        if args.after and errors:
            state.pop("target_review", None)
            state.pop("visual_review", None)
            state.pop("cleanup_completed", None)
        if args.no_screenshots:
            nonvisual = list(nonvisual_checks)
            if args.after and state.get("invariants_before"):
                state["unchanged_results"] = _load_visual_review().verify_invariants(
                    state["invariants_before"], invariants)
                nonvisual.extend({
                    "check": item["check"], "passed": item["passed"], "actual": item["actual"],
                } for item in state["unchanged_results"])
                if any(not item["passed"] for item in state["unchanged_results"]):
                    errors.append("目标外元素位置或尺寸发生变化")
            result["nonvisual_checks"] = nonvisual
            if not all(item["passed"] for item in nonvisual):
                errors.append("无视觉结构化检查存在失败项")
        state["verification"] = {**result, "errors": errors,
                                  "status": "failed" if errors else "passed"}
        page_verification.write_state(codex_home, args.session, state)
        if args.no_screenshots and not errors and args.after:
            try:
                _load_visual_review().record_nonvisual_review(
                    codex_home, args.session,
                    target=args.target_selector or "页面",
                    unchanged_checks=args.invariant,
                    summary="真实 Chromium 非视觉检查完成，仍需用户复查",
                )
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"非视觉审查记录失败：{exc}")
                state = page_verification.read_state(codex_home, args.session)
                state["verification"] = {**result, "errors": errors, "status": "failed"}
                page_verification.write_state(codex_home, args.session, state)
        result["gate"] = page_verification.verify_evidence(codex_home, args.session) or "passed"
    result["status"] = "failed" if errors else "passed"
    result["errors"] = errors
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
