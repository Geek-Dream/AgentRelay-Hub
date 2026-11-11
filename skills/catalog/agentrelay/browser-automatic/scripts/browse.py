#!/usr/bin/env python3
"""Small Playwright Chromium CLI for bounded browser automation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("URL must start with http:// or https://")
    return value


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def _load_steps(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read steps file: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("steps file must contain a non-empty JSON array")
    for index, step in enumerate(payload, 1):
        if not isinstance(step, dict) or not isinstance(step.get("action"), str):
            raise ValueError(f"step {index} must be an object with an action")
    return payload


def _require(step: dict[str, Any], key: str) -> Any:
    value = step.get(key)
    if value is None or value == "":
        raise ValueError(f"{step['action']} requires {key!r}")
    return value


def _check_response(response, url: str) -> None:
    if response is not None and response.status >= 400:
        raise RuntimeError(f"{url} returned HTTP {response.status}")


def _navigate(page, step: dict[str, Any], wait_until: str, timeout: int) -> str:
    url = _url(str(_require(step, "url")))
    response = page.goto(url, wait_until=wait_until, timeout=timeout)
    _check_response(response, url)
    return page.url


def _extract(page, step: dict[str, Any]) -> Any:
    selector = str(_require(step, "selector"))
    mode = str(step.get("mode") or "text").lower()
    attribute = str(step.get("attribute") or "")
    locator = page.locator(selector)
    count = locator.count()
    values: list[Any] = []
    for index in range(count):
        item = locator.nth(index)
        if mode == "text":
            values.append(item.inner_text())
        elif mode == "html":
            values.append(item.inner_html())
        elif mode == "value":
            values.append(item.input_value())
        elif mode == "attribute" and attribute:
            values.append(item.get_attribute(attribute))
        else:
            raise ValueError(
                "extract mode must be text, html, value, or attribute with attribute"
            )
    return values


def _run_step(page, step: dict[str, Any], wait_until: str, timeout: int) -> Any:
    action = str(step["action"]).lower()
    if action == "goto":
        return {"url": _navigate(page, step, wait_until, timeout)}
    if action == "wait":
        milliseconds = int(step.get("milliseconds", step.get("seconds", 1) * 1000))
        if milliseconds < 0 or milliseconds > timeout:
            raise ValueError("wait milliseconds must be between 0 and the timeout")
        page.wait_for_timeout(milliseconds)
        return {"waited_ms": milliseconds}
    if action == "wait_for":
        selector = str(_require(step, "selector"))
        page.locator(selector).first.wait_for(
            state=str(step.get("state") or "visible"),
            timeout=timeout,
        )
        return {"selector": selector, "state": step.get("state") or "visible"}
    if action == "click":
        selector = str(_require(step, "selector"))
        page.locator(selector).first.click(timeout=timeout)
        return {"selector": selector}
    if action in {"fill", "type"}:
        selector = str(_require(step, "selector"))
        value = str(_require(step, "value"))
        locator = page.locator(selector).first
        if action == "fill":
            locator.fill(value, timeout=timeout)
        else:
            locator.press_sequentially(value, timeout=timeout)
        return {"selector": selector, "value": value}
    if action == "press":
        key = str(_require(step, "key"))
        selector = str(step.get("selector") or "")
        if selector:
            page.locator(selector).first.press(key, timeout=timeout)
        else:
            page.keyboard.press(key)
        return {"selector": selector, "key": key}
    if action == "select":
        selector = str(_require(step, "selector"))
        value = _require(step, "value")
        selected = page.locator(selector).first.select_option(
            value=str(value),
            timeout=timeout,
        )
        return {"selector": selector, "selected": selected}
    if action in {"check", "uncheck"}:
        selector = str(_require(step, "selector"))
        locator = page.locator(selector).first
        if action == "check":
            locator.check(timeout=timeout)
        else:
            locator.uncheck(timeout=timeout)
        return {"selector": selector}
    if action == "screenshot":
        path = Path(str(_require(step, "path"))).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=bool(step.get("full_page", False)))
        return {"path": str(path.resolve())}
    if action == "extract":
        return {"values": _extract(page, step)}
    if action == "eval":
        return {"value": page.evaluate(str(_require(step, "expression")))}
    if action == "title":
        return {"value": page.title()}
    if action == "url":
        return {"value": page.url}
    if action == "assert_text":
        selector = str(_require(step, "selector"))
        contains = str(_require(step, "contains"))
        actual = page.locator(selector).first.inner_text(timeout=timeout)
        if contains not in actual:
            raise AssertionError(f"text {contains!r} not found in {selector}")
        return {"selector": selector, "contains": contains}
    if action == "assert_url":
        contains = str(_require(step, "contains"))
        if contains not in page.url:
            raise AssertionError(f"URL does not contain {contains!r}: {page.url}")
        return {"url": page.url, "contains": contains}
    raise ValueError(f"unsupported action: {action}")


def _launch(playwright, headed: bool):
    return playwright.chromium.launch(headless=not headed)


def cmd_screenshot(playwright, args) -> int:
    browser = _launch(playwright, args.headed)
    try:
        page = browser.new_page(
            viewport={"width": args.width, "height": args.height}
        )
        response = page.goto(
            args.url,
            wait_until=args.wait_until,
            timeout=args.timeout,
        )
        _check_response(response, args.url)
        if args.wait:
            page.wait_for_timeout(args.wait)
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output), full_page=args.full_page)
        print(
            json.dumps(
                {
                    "path": str(output.resolve()),
                    "title": page.title(),
                    "url": page.url,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        browser.close()


def cmd_scrape(playwright, args) -> int:
    browser = _launch(playwright, args.headed)
    try:
        page = browser.new_page()
        response = page.goto(
            args.url,
            wait_until=args.wait_until,
            timeout=args.timeout,
        )
        _check_response(response, args.url)
        if args.wait:
            page.wait_for_timeout(args.wait)
        if args.selector:
            locator = page.locator(args.selector)
            values = []
            for index in range(locator.count()):
                item = locator.nth(index)
                values.append(item.inner_html() if args.html else item.inner_text())
            print("\n".join(values))
        else:
            print(page.content() if args.html else page.locator("body").inner_text())
        return 0
    finally:
        browser.close()


def cmd_run(playwright, args) -> int:
    steps = _load_steps(Path(args.script).expanduser())
    browser = _launch(playwright, args.headed)
    try:
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height}
        )
        try:
            page = context.new_page()
            for index, step in enumerate(steps, 1):
                result = _run_step(page, step, args.wait_until, args.timeout)
                print(
                    json.dumps(
                        {
                            "step": index,
                            "action": step["action"],
                            "result": result,
                        },
                        ensure_ascii=False,
                    )
                )
            print(json.dumps({"final_url": page.url, "title": page.title()}))
        finally:
            context.close()
        return 0
    finally:
        browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Playwright browser automation for AgentRelay",
    )
    parser.add_argument("--headed", action="store_true", help="show Chromium")
    parser.add_argument(
        "--timeout",
        type=_positive_int,
        default=30_000,
        help="operation timeout in milliseconds (default: 30000)",
    )
    parser.add_argument(
        "--wait-until",
        choices=("commit", "domcontentloaded", "load", "networkidle"),
        default="domcontentloaded",
    )
    parser.add_argument("--width", type=_positive_int, default=1280)
    parser.add_argument("--height", type=_positive_int, default=800)
    commands = parser.add_subparsers(dest="command", required=True)

    screenshot = commands.add_parser("screenshot", help="capture a page")
    screenshot.add_argument("url", type=_url)
    screenshot.add_argument("-o", "--output", default="screenshot.png")
    screenshot.add_argument("--full-page", action="store_true")
    screenshot.add_argument("--wait", type=int, default=0)
    screenshot.set_defaults(handler=cmd_screenshot)

    scrape = commands.add_parser("scrape", help="extract page content")
    scrape.add_argument("url", type=_url)
    scrape.add_argument("-s", "--selector")
    scrape.add_argument("--html", action="store_true")
    scrape.add_argument("--wait", type=int, default=0)
    scrape.set_defaults(handler=cmd_scrape)

    run = commands.add_parser("run", help="run a JSON workflow")
    run.add_argument("script")
    run.set_defaults(handler=cmd_run)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "wait", 0) < 0:
        parser.error("--wait cannot be negative")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error(
            "Playwright is unavailable; run this script with the AgentRelay "
            "Python environment"
        )
    try:
        with sync_playwright() as playwright:
            return int(args.handler(playwright, args))
    except Exception as exc:
        print(f"browser-automatic: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
