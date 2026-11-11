#!/usr/bin/env python3
"""Emit GitHub Actions annotations for failed JUnit test cases."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _escape(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def annotate(path: Path) -> int:
    root = ET.parse(path).getroot()
    failures = 0
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        failures += 1
        class_name = case.get("classname", "")
        test_name = case.get("name", "")
        details = (problem.get("message") or problem.text or "test failed").strip()
        first_line = details.splitlines()[0]
        title = _escape(f"{class_name}::{test_name}" if class_name else test_name)
        message = _escape(first_line)
        print(f"::error title={title}::{message}")
    return failures


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: junit_annotations.py REPORT.xml", file=sys.stderr)
        return 2
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    annotate(Path(arguments[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
