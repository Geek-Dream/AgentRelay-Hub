import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import page_verification
from scripts import skill_catalog as package_catalog_module
from scripts import verify_page
from scripts import visual_review


class PageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.page = self.workspace / "index.html"
        self.page.write_text("<h1>page</h1>", encoding="utf-8")
        self.session = "page-session"

    def edit(self, path=None):
        return page_verification.record_page_edit(
            self.home, self.session, str(self.workspace), "Edit",
            {"file_path": str(path or self.page), "old_string": "a", "new_string": "b"},
        )

    def pass_evidence(self):
        screenshot = self.home / "agent_relay_tracker/page-verification" / self.session / "evidence/desktop.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 900, "height": 600})
                page.set_content("""
                    <body style="margin:0;background:#0f172a;color:#f8fafc;font-family:sans-serif">
                      <header style="height:72px;background:#0f766e;padding:16px">页面标题</header>
                      <main style="padding:24px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
                        <section style="height:180px;background:#1d4ed8;padding:16px">主要内容</section>
                        <section style="height:180px;background:#b45309;padding:16px">目标外模块</section>
                        <section style="height:180px;background:#475569;padding:16px">底部内容 A</section>
                        <section style="height:180px;background:#7e22ce;padding:16px">底部内容 B</section>
                      </main>
                    </body>
                """)
                page.screenshot(path=str(screenshot))
            finally:
                browser.close()
        state = page_verification.read_state(self.home, self.session)
        state["verification"] = {"status": "passed", "browser": "chromium",
                                  "screenshots": [str(screenshot)], "files": state["files"],
                                  "url": "file:///tmp/page.html"}
        page_verification.write_state(self.home, self.session, state)
        visual_review.record_review(self.home, self.session, mode="native", screenshots=[str(screenshot)],
            target="页面", unchanged_checks=["其他模块"], passed=True, summary="已读取整页截图",
            self_reviewed=True,
            attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
            observations={"target": "页面标题和主要内容均可见，宽度 900px",
                          "其他模块": "before/after rect 和尺寸相同"})
        visual_review.cleanup_screenshots(self.home, self.session)

    def test_later_edit_keeps_before_baseline_but_invalidates_final_evidence(self):
        self.edit()
        state = page_verification.read_state(self.home, self.session)
        state.update(target_before={"selector": "#target"}, target_expected={"color": "green"},
                     invariants_before=[{"selector": "#other"}], target_review={"target_verified": True},
                     verification={"status": "passed"}, visual_review={"status": "passed"}, cleanup_completed=True)
        page_verification.write_state(self.home, self.session, state)
        self.page.write_text("new version", encoding="utf-8")
        self.edit()
        current = page_verification.read_state(self.home, self.session)
        self.assertIn("target_before", current)
        self.assertIn("invariants_before", current)
        for key in ("target_review", "verification", "visual_review", "cleanup_completed"):
            self.assertNotIn(key, current)

    def test_target_review_does_not_bypass_file_freshness_or_invariants(self):
        self.edit()
        self.pass_evidence()
        state = page_verification.read_state(self.home, self.session)
        from playwright.sync_api import sync_playwright
        image = str(self.home / "agent_relay_tracker/page-verification" / self.session / "evidence/target.png")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 900, "height": 600})
                page.set_content("""
                    <body style="margin:0;background:#172554;color:white">
                      <header style="height:80px;background:#0f766e;padding:16px">目标页面</header>
                      <main style="padding:24px;background:#1e40af;height:360px">目标内容和目标外区域</main>
                      <footer style="height:100px;background:#7e22ce;padding:16px">底部导航</footer>
                    </body>
                """)
                page.screenshot(path=image)
            finally:
                browser.close()
        state["verification"]["screenshots"] = [image]
        page_verification.write_state(self.home, self.session, state)
        visual_review.record_review(self.home, self.session, mode="native", screenshots=[image],
            target="目标外布局、兄弟元素和其他模块无回归", unchanged_checks=["其他模块"], passed=True,
            summary="已逐项读取截图",
            attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出", self_reviewed=True,
            observations={"target": "目标内容可见，边界与相邻模块间距 16px",
                          "其他模块": "before/after rect 和尺寸相同"})
        visual_review.cleanup_screenshots(self.home, self.session)
        before = {"selector": "#target", "styles": {"color": "red"},
                  "rect": {"x": 1, "y": 1, "width": 10, "height": 10}}
        after = {"selector": "#target", "styles": {"color": "green"},
                 "rect": {"x": 1, "y": 1, "width": 10, "height": 10}}
        state.update(target_before=before, target_review={
            "selector": "#target", "before": before, "after": after,
            "target_verified": True, "status": "passed", "checks": {"color": True},
            "expected_changes": {"color": "green"}, "unchanged_checks": ["目标外布局、兄弟元素和其他模块无回归"]},
            invariants_before=[{"selector": "#other", "before": {"rect": {"x": 1}}}],
            unchanged_results=[{"check": "目标外布局、兄弟元素和其他模块无回归", "passed": True, "actual": "unchanged"}])
        page_verification.write_state(self.home, self.session, state)
        self.assertIsNone(page_verification.verify_evidence(self.home, self.session))
        self.page.write_text("changed after target verification", encoding="utf-8")
        self.assertIn("又有修改", page_verification.verify_evidence(self.home, self.session))
        state = page_verification.read_state(self.home, self.session)
        state["files"][str(self.page)]["sha256"] = page_verification._source_digest(self.page)
        state["verification"]["files"] = state["files"]
        state.pop("unchanged_results")
        page_verification.write_state(self.home, self.session, state)
        self.assertIn("目标外", page_verification.verify_evidence(self.home, self.session))

    def test_target_review_requires_actual_target_snapshot_and_invariant_results(self):
        self.edit()
        state = page_verification.read_state(self.home, self.session)
        state.update(target_review={"target_verified": True},
                     verification={"status": "passed", "browser": "chromium", "screenshots": [],
                                   "files": state["files"], "url": self.page.as_uri()},
                     visual_review={"status": "passed", "screenshots_deleted": True}, cleanup_completed=True)
        page_verification.write_state(self.home, self.session, state)
        self.assertIn("目标", page_verification.verify_evidence(self.home, self.session))

    def test_status_passed_without_itemized_certificate_is_blocked(self):
        self.edit()
        state = page_verification.read_state(self.home, self.session)
        state.update(
            verification={"status": "passed", "browser": "chromium", "screenshots": [],
                          "files": state["files"], "url": self.page.as_uri()},
            visual_review={"status": "passed", "screenshots_deleted": True},
            cleanup_completed=True,
        )
        page_verification.write_state(self.home, self.session, state)
        self.assertIn("逐项目标视觉认证", page_verification.verify_evidence(self.home, self.session))

    def test_nonvisual_evidence_needs_no_screenshots_but_requires_user_review(self):
        self.edit()
        state = page_verification.read_state(self.home, self.session)
        state["verification"] = {"status": "passed", "browser": "chromium", "screenshots": [],
                                 "files": state["files"], "url": self.page.as_uri(),
                                 "nonvisual_checks": [
                                     {"check": name, "passed": True, "actual": "真实检查"}
                                     for name in ("dom", "console", "diff", "interaction")
                                 ]}
        page_verification.write_state(self.home, self.session, state)
        visual_review.record_nonvisual_review(self.home, self.session, target="页面",
                                              unchanged_checks=[], summary="请用户复查")
        self.assertIn("用户复查", page_verification.verify_evidence(self.home, self.session))

    def test_only_page_write_tools_are_recorded(self):
        self.assertEqual(self.edit(), [str(self.page)])
        self.assertEqual(page_verification.record_page_edit(
            self.home, self.session, str(self.workspace), "Read", {"file_path": str(self.page)}), [])
        other = self.workspace / "module.py"
        other.write_text("pass", encoding="utf-8")
        self.assertEqual(self.edit(other), [])

    def test_missing_stale_and_failed_evidence_are_blocked(self):
        self.edit()
        self.assertIn("没有成功的真实浏览器检查", page_verification.verify_evidence(self.home, self.session))
        state = page_verification.read_state(self.home, self.session)
        state["verification"] = {"status": "failed", "browser": "chromium"}
        page_verification.write_state(self.home, self.session, state)
        self.assertIsNotNone(page_verification.verify_evidence(self.home, self.session))
        self.pass_evidence()
        self.assertIsNone(page_verification.verify_evidence(self.home, self.session))
        self.page.write_text("changed after browser check", encoding="utf-8")
        self.assertIn("又有修改", page_verification.verify_evidence(self.home, self.session))

    def test_cleanup_allows_deleted_screenshots_only_after_visual_pass(self):
        self.edit()
        self.pass_evidence()
        state = page_verification.read_state(self.home, self.session)
        state["visual_review"]["screenshots_deleted"] = True
        page_verification.write_state(self.home, self.session, state)
        self.assertIsNone(page_verification.verify_evidence(self.home, self.session))
        state["visual_review"]["status"] = "user_review_required"
        page_verification.write_state(self.home, self.session, state)
        self.assertIn("用户复查", page_verification.verify_evidence(self.home, self.session))

    def test_screenshot_cleanup_is_required_before_delivery(self):
        self.edit()
        self.pass_evidence()
        state = page_verification.read_state(self.home, self.session)
        state.pop("cleanup_completed", None)
        page_verification.write_state(self.home, self.session, state)
        self.assertIn("cleanup", page_verification.verify_evidence(self.home, self.session))

    def test_current_session_resolution_is_conservative(self):
        self.assertEqual(
            page_verification.resolve_current_session(
                self.home,
                cwd=str(self.workspace),
                environ={"CODEX_THREAD_ID": "environment-session"},
            ),
            "environment-session",
        )
        self.assertEqual(
            page_verification.resolve_current_session(
                self.home,
                cwd=str(self.workspace),
                environ={
                    "CODEX_THREAD_ID": "codex-session",
                    "CLAUDE_SESSION_ID": "claude-session",
                },
            ),
            "",
        )

    def test_workspace_binding_resolves_one_session_and_rejects_ambiguity(self):
        page_verification.bind_workspace_session(
            self.home,
            "workspace-session-one",
            str(self.workspace),
            environ={},
        )
        self.assertEqual(
            page_verification.resolve_current_session(
                self.home,
                cwd=str(self.workspace),
                environ={},
            ),
            "workspace-session-one",
        )

        page_verification.bind_workspace_session(
            self.home,
            "workspace-session-two",
            str(self.workspace),
            environ={},
        )
        self.assertEqual(
            page_verification.resolve_current_session(
                self.home,
                cwd=str(self.workspace),
                environ={},
            ),
            "",
        )

    def test_browser_pass_points_to_visual_review_without_rerunning_chromium(self):
        self.edit()
        screenshot = (
            self.home / "agent_relay_tracker/page-verification"
            / self.session / "evidence/desktop.png"
        )
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"placeholder evidence")
        state = page_verification.read_state(self.home, self.session)
        state["verification"] = {
            "status": "passed",
            "browser": "chromium",
            "screenshots": [str(screenshot)],
            "files": state["files"],
            "url": self.page.as_uri(),
        }
        page_verification.write_state(self.home, self.session, state)

        detail = page_verification.verification_preflight(
            self.home, self.session, "页面已完成，验证通过。",
        )
        self.assertFalse(detail["ok"])
        self.assertEqual(detail["code"], "visual_review_missing")
        self.assertEqual(detail["phase"], "visual")
        self.assertIn("visual_review.py record", detail["next_command"])
        self.assertNotIn("verify_page.py", detail["next_command"])

        state = page_verification.read_state(self.home, self.session)
        state["visual_review"] = {"status": "passed"}
        page_verification.write_state(self.home, self.session, state)
        detail = page_verification.verification_preflight(
            self.home, self.session, "页面已完成，验证通过。",
        )
        self.assertFalse(detail["ok"])
        self.assertEqual(detail["code"], "screenshot_cleanup_missing")
        self.assertEqual(detail["phase"], "cleanup")
        self.assertIn("visual_review.py cleanup", detail["next_command"])

    def test_verify_page_auto_resolves_current_host_session(self):
        self.page.write_text("<button>查询</button><input value=''>", encoding="utf-8")
        env = {**os.environ, "CODEX_HOME": str(self.home)}
        for key in page_verification.SESSION_ENV_KEYS:
            env.pop(key, None)
        env["CODEX_THREAD_ID"] = "auto-session"
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "verify_page.py"),
                "--url", self.page.as_uri(),
                "--no-screenshots",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["session_id"], "auto-session")
        state = page_verification.read_state(self.home, "auto-session")
        self.assertEqual(state["verification"]["status"], "passed")

    def test_non_page_and_honest_unverified_messages_do_not_claim_completion(self):
        self.assertFalse(page_verification.completion_claim("后端已完成，测试通过"))
        self.assertFalse(page_verification.completion_claim("页面已修改；未验证"))
        self.assertFalse(page_verification.completion_claim(
            "20 个相关测试通过；我会再跑完整视觉/页面门禁测试。"
            "安装副本已同步，相关 40 个测试全部通过，语法检查也通过。"
        ))
        self.assertFalse(page_verification.completion_claim(
            "页面验证计划将在确认后执行。"
        ))
        self.assertFalse(page_verification.completion_claim(
            "这个门禁用于判断页面是否已完成、浏览器检查是否通过。"
        ))
        self.assertFalse(page_verification.completion_claim(
            "页面完成声明为什么要等浏览器检查通过？"
        ))
        self.assertFalse(page_verification.completion_claim(
            "已修正为：“页面已完成，浏览器检查通过”仍会触发门禁。"
        ))
        self.assertTrue(page_verification.completion_claim("配置中心页面已完成，检查通过"))
        self.assertTrue(page_verification.completion_claim("页面已修改，浏览器检查通过。"))
        self.assertIsNone(page_verification.verify_evidence(self.home, self.session))

    def test_verify_page_skips_readonly_date_inputs_during_interaction_check(self):
        self.page.write_text(
            """
            <button>查询</button>
            <input id="readonly-date" type="date" readonly value="2026-11-05">
            <input id="aria-date" type="date" aria-readonly="true" value="2026-11-05">
            <input id="editable" value="">
            """,
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "verify_page.py"),
                "--session", self.session,
                "--url", self.page.as_uri(),
                "--no-screenshots",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={**__import__("os").environ, "CODEX_HOME": str(self.home)},
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        interaction = next(item for item in result["nonvisual_checks"] if item["check"] == "interaction")
        self.assertTrue(interaction["passed"])
        self.assertIn("输入框已真实填写", interaction["actual"])

    def test_verify_page_accepts_readonly_only_form_without_mutation(self):
        self.page.write_text(
            """
            <label>只读表单</label>
            <input id="readonly-date" type="date" readonly value="2026-11-05">
            <textarea id="readonly-note" readonly>fixed</textarea>
            """,
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "verify_page.py"),
                "--session", self.session,
                "--url", self.page.as_uri(),
                "--no-screenshots",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={**__import__("os").environ, "CODEX_HOME": str(self.home)},
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        interaction = next(item for item in result["nonvisual_checks"] if item["check"] == "interaction")
        self.assertTrue(interaction["passed"])
        self.assertIn("只读或不可填写控件", interaction["actual"])

    def test_visual_settle_waits_for_finite_animations_before_screenshot(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    """
                    <style>
                      @keyframes fade-in { from { opacity: 0 } to { opacity: 1 } }
                      #target { opacity: 0; animation: fade-in 180ms linear both }
                    </style>
                    <div id="target">ready</div>
                    """
                )
                page_verification.wait_for_visual_settle(page)
                self.assertEqual(
                    page.locator("#target").evaluate(
                        "element => getComputedStyle(element).opacity"
                    ),
                    "1",
                )
            finally:
                browser.close()

    def test_bash_page_change_uses_worktree_delta(self):
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(["git", "add", "index.html"], cwd=self.workspace, check=True)
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "initial"], cwd=self.workspace, check=True)
        page_verification.record_command_start(self.home, self.session, str(self.workspace),
                                               "Bash", {"command": "python formatter.py"})
        self.page.write_text("<h1>changed</h1>", encoding="utf-8")
        changed = page_verification.record_page_edit(self.home, self.session, str(self.workspace),
                                                    "Bash", {"command": "python formatter.py"}, {})
        self.assertEqual(changed, [str(self.page)])
        page_verification.record_command_start(self.home, self.session, str(self.workspace),
                                               "Bash", {"command": "npm test"})
        unchanged = page_verification.record_page_edit(self.home, self.session, str(self.workspace),
                                                      "Bash", {"command": "npm test"}, {})
        self.assertEqual(unchanged, [])

    def test_other_page_cannot_use_config_page_evidence(self):
        self.edit()
        self.pass_evidence()
        state = page_verification.read_state(self.home, self.session)
        state["verification"]["url"] = ""
        page_verification.write_state(self.home, self.session, state)
        self.assertIn("--url", page_verification.verify_evidence(self.home, self.session))

    def test_config_page_template_counts_as_page_source(self):
        source = self.workspace / "config_web.py"
        source.write_text("PAGE = '<html></html>'", encoding="utf-8")
        self.assertEqual(self.edit(source), [str(source)])

    def test_explicit_url_wins_over_config_template_session_fallback(self):
        source = self.workspace / "config_web.py"
        source.write_text("PAGE = '<html></html>'", encoding="utf-8")
        self.edit(source)
        self.page.write_text(
            '<main id="explicit-url-marker"><button>查询</button><input value=""></main>',
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "verify_page.py"),
                "--session", self.session,
                "--url", self.page.as_uri(),
                "--no-screenshots",
                "--target-selector", "#explicit-url-marker",
                "--expected", '{"display":"block"}',
            ],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "CODEX_HOME": str(self.home)},
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["url"], self.page.as_uri())
        self.assertEqual(result["status"], "passed")

    def test_default_config_page_check_renders_catalog_skills(self):
        source = self.workspace / "config_web.py"
        source.write_text("PAGE = '<html></html>'", encoding="utf-8")
        self.edit(source)
        config = verify_page._sample_config()
        page = verify_page._config_page()
        self.assertEqual(
            config["skill_catalog"]["defaults"],
            list(package_catalog_module.PREFERRED_DEFAULT_SKILL_IDS),
        )
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                rendered = browser.new_page()
                rendered.set_content(page, wait_until="domcontentloaded")
                rendered.wait_for_selector(
                    '.skill-box:has(.skill-check[value="browser-automatic"])'
                )
                default_ids = rendered.eval_on_selector_all(
                    ".skill-check:checked",
                    "nodes => nodes.map(node => node.value)",
                )
            finally:
                browser.close()

        self.assertEqual(
            set(default_ids),
            set(package_catalog_module.PREFERRED_DEFAULT_SKILL_IDS),
        )
        self.assertEqual(
            len(default_ids),
            len(package_catalog_module.PREFERRED_DEFAULT_SKILL_IDS),
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "verify_page.py"),
                "--session", self.session,
                "--no-screenshots",
                "--target-selector",
                '.skill-box:has(.skill-check[value="browser-automatic"])',
                "--invariant", ".mast",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "CODEX_HOME": str(self.home)},
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["browser"], "chromium")
        self.assertTrue(result["nonvisual_checks"])

    def test_concurrent_page_writes_keep_every_file_without_temp_collisions(self):
        pages = []
        for index in range(24):
            path = self.workspace / f"concurrent-{index}.html"
            path.write_text(f"<h1>{index}</h1>", encoding="utf-8")
            pages.append(path)

        def record(path):
            return page_verification.record_page_edit(
                self.home, self.session, str(self.workspace), "Edit",
                {"file_path": str(path)},
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(record, pages))
        self.assertTrue(all(result == [str(path)] for result, path in zip(results, pages)))
        state = page_verification.read_state(self.home, self.session)
        self.assertEqual(set(state["files"]), {str(path) for path in pages})
        state_dir = page_verification._state_file(self.home, self.session).parent
        self.assertEqual(list(state_dir.glob(".*.tmp")), [])

    def test_cross_process_page_writes_keep_every_file(self):
        pages = []
        for index in range(12):
            path = self.workspace / f"process-{index}.html"
            path.write_text(f"<h1>process {index}</h1>", encoding="utf-8")
            pages.append(path)

        worker = (
            "import sys\n"
            "from pathlib import Path\n"
            "from scripts import page_verification\n"
            "page_verification.record_page_edit("
            "Path(sys.argv[1]), sys.argv[2], sys.argv[3], 'Edit', {'file_path': sys.argv[4]})\n"
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable, "-c", worker, str(self.home), self.session,
                    str(self.workspace), str(path),
                ],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for path in pages
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)

        state = page_verification.read_state(self.home, self.session)
        self.assertEqual(set(state["files"]), {str(path) for path in pages})
        state_dir = page_verification._state_file(self.home, self.session).parent
        self.assertEqual(list(state_dir.glob(".*.tmp")), [])


class HookPageGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.page = self.workspace / "page.html"
        self.page.write_text("<h1>page</h1>", encoding="utf-8")
        self.session = "hook-page"

    def run_hook(self, event, **values):
        payload = {"hook_event_name": event, "session_id": self.session,
                   "cwd": str(self.workspace), **values}
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "hooks" / "agent_relay_hook.py")],
                                input=json.dumps(payload), text=True, capture_output=True,
                                env={**__import__("os").environ, "CODEX_HOME": str(self.home),
                                     "AGENTRELAY_TARGET": "claude"}, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def test_after_reuses_before_invariants_and_no_screenshot_mode(self):
        env = {**__import__("os").environ, "CODEX_HOME": str(self.home)}
        self.page.write_text('<button id="target" style="color:rgb(255, 0, 0)">目标</button><input id="input" style="width:100px">', encoding="utf-8")
        common = [sys.executable, str(PROJECT_ROOT / "scripts/verify_page.py"), "--session", self.session,
                  "--url", self.page.as_uri(), "--target-selector", "#target", "--expected", '{"color":"rgb(0, 128, 0)"}',
                  "--invariant", "#input"]
        subprocess.run(common, check=True, capture_output=True, env=env)
        self.page.write_text(self.page.read_text(encoding="utf-8").replace("rgb(255, 0, 0)", "rgb(0, 128, 0)").replace("width:100px", "width:300px"), encoding="utf-8")
        after = subprocess.run(common + ["--after"], capture_output=True, text=True, check=False, env=env)
        self.assertEqual(after.returncode, 1, after.stdout)
        self.assertIn("目标外", after.stdout)
        state = page_verification.read_state(self.home, self.session)
        self.assertFalse(state["unchanged_results"][0]["passed"])

        other_home = self.home / "nonvisual-home"
        self.page.write_text(self.page.read_text(encoding="utf-8").replace("width:300px", "width:100px"), encoding="utf-8")
        env["CODEX_HOME"] = str(other_home)
        no_images = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/verify_page.py"), "--session", self.session,
                                    "--url", self.page.as_uri(), "--no-screenshots", "--target-selector", "#target",
                                    "--expected", '{"color":"rgb(0, 128, 0)"}', "--after"],
                                   capture_output=True, text=True, check=False, env=env)
        self.assertEqual(no_images.returncode, 1, no_images.stdout + no_images.stderr)
        self.assertIn("缺少修改前目标快照", no_images.stdout)
        self.page.write_text(
            self.page.read_text(encoding="utf-8").replace(
                "rgb(0, 128, 0)", "rgb(255, 0, 0)"
            ),
            encoding="utf-8",
        )
        before = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/verify_page.py"), "--session", self.session,
                                 "--url", self.page.as_uri(), "--no-screenshots", "--target-selector", "#target",
                                 "--expected", '{"color":"rgb(0, 128, 0)"}', "--invariant", "#input"],
                                capture_output=True, text=True, check=False, env=env)
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        self.page.write_text(
            self.page.read_text(encoding="utf-8")
            .replace("rgb(255, 0, 0)", "rgb(0, 128, 0)")
            .replace("width:100px", "width:300px"),
            encoding="utf-8",
        )
        no_images = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/verify_page.py"), "--session", self.session,
                                    "--url", self.page.as_uri(), "--no-screenshots", "--target-selector", "#target",
                                    "--expected", '{"color":"rgb(0, 128, 0)"}', "--after"],
                                   capture_output=True, text=True, check=False, env=env)
        self.assertEqual(no_images.returncode, 1, no_images.stdout + no_images.stderr)
        self.assertIn("目标外", no_images.stdout)
        self.page.write_text(self.page.read_text(encoding="utf-8").replace("width:300px", "width:100px"), encoding="utf-8")
        no_images = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/verify_page.py"), "--session", self.session,
                                    "--url", self.page.as_uri(), "--no-screenshots", "--target-selector", "#target",
                                    "--expected", '{"color":"rgb(0, 128, 0)"}', "--after"],
                                   capture_output=True, text=True, check=False, env=env)
        self.assertEqual(no_images.returncode, 0, no_images.stdout + no_images.stderr)
        result = json.loads(no_images.stdout)
        self.assertEqual(result["screenshots"], [])
        checks = {item["check"]: item["passed"] for item in result["nonvisual_checks"]}
        for name in ("dom", "console", "diff", "interaction"):
            self.assertTrue(checks[name])
        self.assertFalse((other_home / "agent_relay_tracker/page-verification" / self.session / "evidence" / "config-desktop.png").exists())

    def test_after_cannot_replace_expected_or_target_selector(self):
        env = {**__import__("os").environ, "CODEX_HOME": str(self.home)}
        self.page.write_text(
            '<button id="target" style="color:rgb(255, 0, 0)">目标</button>'
            '<input id="input" style="width:100px">',
            encoding="utf-8",
        )
        base = [
            sys.executable, str(PROJECT_ROOT / "scripts/verify_page.py"),
            "--session", self.session, "--url", self.page.as_uri(),
            "--no-screenshots",
        ]
        before = subprocess.run(
            base + [
                "--target-selector", "#target",
                "--expected", '{"color":"rgb(0, 128, 0)"}',
                "--invariant", "#input",
            ],
            capture_output=True, text=True, check=False, env=env,
        )
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)

        changed_expected = subprocess.run(
            base + [
                "--target-selector", "#target",
                "--expected", '{"color":"rgb(0, 0, 0)"}',
                "--invariant", "#input", "--after",
            ],
            capture_output=True, text=True, check=False, env=env,
        )
        self.assertEqual(changed_expected.returncode, 2, changed_expected.stdout + changed_expected.stderr)
        self.assertIn("--expected 必须与修改前逐项一致", changed_expected.stderr)

        changed_selector = subprocess.run(
            base + [
                "--target-selector", "#input",
                "--expected", '{"color":"rgb(0, 128, 0)"}',
                "--invariant", "#input", "--after",
            ],
            capture_output=True, text=True, check=False, env=env,
        )
        self.assertEqual(changed_selector.returncode, 2, changed_selector.stdout + changed_selector.stderr)
        self.assertIn("--target-selector 必须与修改前一致", changed_selector.stderr)

    def test_hook_blocks_false_page_completion_then_accepts_current_evidence(self):
        self.run_hook("PostToolUse", tool_name="Edit", tool_use_id="edit-1",
                      tool_input={"file_path": str(self.page)}, tool_response={"success": True})
        denied = self.run_hook("Stop", last_assistant_message="页面已完成，验证通过。")
        self.assertEqual(denied["decision"], "block")
        self.assertIn("页面验证门禁", denied["reason"])
        self.assertIn(
            "下一步：python3 scripts/verify_page.py --session hook-page",
            denied["reason"],
        )
        self.assertNotIn("控制台、主要导航、主题和桌面/移动截图", denied["reason"])
        PageEvidenceTests.pass_evidence(self)
        allowed = self.run_hook("Stop", last_assistant_message="页面已完成，验证通过。")
        self.assertNotEqual(allowed.get("reason", ""), denied["reason"])
        self.assertNotIn("页面验证门禁", allowed.get("reason", ""))

    def test_hook_event_persists_workspace_session_binding(self):
        self.run_hook("SessionStart")
        bindings = json.loads(
            page_verification.session_bindings_path(self.home).read_text(
                encoding="utf-8",
            )
        )["bindings"]
        self.assertTrue(any(
            item["session_id"] == self.session
            and item["workspace"] == str(self.workspace.resolve())
            for item in bindings
        ))

    def test_stop_normal_allow_is_silent(self):
        result = self.run_hook("Stop", last_assistant_message="本轮没有新的写任务。")
        self.assertEqual(result, {})

    def test_hook_blocks_original_one_quadrant_claim_from_becoming_full_visual_pass(self):
        self.run_hook("PostToolUse", tool_name="Edit", tool_use_id="edit-quadrants",
                      tool_input={"file_path": str(self.page)}, tool_response={"success": True})
        PageEvidenceTests.pass_evidence(self)
        message = (
            "是的，我亲自检查过。使用真实 Chromium 打开 5174，操作了弹框的取消、确定，"
            "以及 1–4 象限的放大、还原。截取并实际查看了第 1 象限放大后的截图。"
            "补充准确范围：截图只人工查看了第 1 象限；另外三张通过浏览器操作和尺寸测量验证。"
        )
        denied = self.run_hook("Stop", last_assistant_message=message)
        self.assertEqual(denied["decision"], "block")
        self.assertIn("视觉认证", denied["reason"])
        self.assertIn("4 个目标", denied["reason"])

    def test_installed_hook_layout_resolves_page_verifier(self):
        import hooks.agent_relay_tracker as tracker
        skill = self.home / "skills" / "agent-relay" / "scripts"
        skill.mkdir(parents=True)
        for name in ("page_verification.py", "verification_state.py"):
            (skill / name).write_text(
                (PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8")
        old_home = tracker.CODEX_DIR
        self.addCleanup(setattr, tracker, "CODEX_DIR", old_home)
        tracker.CODEX_DIR = self.home
        self.run_hook("PostToolUse", tool_name="Edit", tool_use_id="installed-edit",
                      tool_input={"file_path": str(self.page)}, tool_response={"success": True})
        self.assertTrue(page_verification.read_state(self.home, self.session)["files"])

    def test_non_page_task_is_not_blocked(self):
        source = self.workspace / "main.py"
        source.write_text("print('ok')", encoding="utf-8")
        self.run_hook("PostToolUse", tool_name="Edit", tool_use_id="edit-2",
                      tool_input={"file_path": str(source)}, tool_response={"success": True})
        result = self.run_hook("Stop", last_assistant_message="后端已完成，测试通过。")
        self.assertNotIn("页面验证门禁", result.get("reason", ""))

    def test_hook_blocks_visual_claim_without_page_evidence(self):
        result = self.run_hook(
            "Stop",
            last_assistant_message="我亲自检查了截图，页面布局正常，没有大块空白。",
        )
        self.assertEqual(result.get("decision"), "block")
        self.assertIn("视觉验收", result.get("reason", ""))
        self.assertIn("没有页面文件修改记录", result.get("reason", ""))

    def test_hook_allows_honest_visual_rejection_without_evidence(self):
        result = self.run_hook(
            "Stop",
            last_assistant_message=(
                "我刚才做的是像素统计，不是对页面进行逐项目标视觉检查；"
                "当前会话没有视觉认证证书，需用户复查。"
            ),
        )
        self.assertNotIn("视觉验收", result.get("reason", ""))

    def test_hook_allows_requirement_plan_with_future_visual_check(self):
        result = self.run_hook(
            "Stop",
            last_assistant_message=(
                "确认卡：订单空加载状态与指标列可读性。"
                "验证将覆盖骨架加载、无数据、完整指标文字、固定列与排序，并截图检查。"
                "请确认后修改。方案已列出，尚未修改，等待你确认后实施。"
            ),
        )
        self.assertNotIn("视觉验收", result.get("reason", ""))
        self.assertNotIn("交付审计", result.get("reason", ""))

    def test_hook_allows_gate_rule_examples_without_page_evidence(self):
        result = self.run_hook(
            "Stop",
            last_assistant_message=(
                "旧规则只识别到“截图检查”，没有识别计划语境。"
                "已修正为：“已亲自检查、已逐项核对、验证通过”等肯定完成声明仍然触发。"
            ),
        )
        self.assertNotIn("视觉验收", result.get("reason", ""))

    def test_hook_allows_visual_gate_explanation_with_quoted_examples(self):
        result = self.run_hook(
            "Stop",
            last_assistant_message=(
                "误判已复现并修正：正向“我亲自检查了截图”仍返回 True。"
                "20 个相关测试通过；我会同步安装副本并再跑完整视觉/页面门禁测试。"
                "安装副本已同步，相关 40 个测试全部通过，语法检查和 git diff --check 也通过。"
            ),
        )
        self.assertNotIn("视觉验收", result.get("reason", ""))
        self.assertNotIn("交付审计", result.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
