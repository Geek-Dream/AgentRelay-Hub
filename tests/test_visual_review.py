import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import visual_review as review
from scripts import page_verification as page


class VisualReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.session = "visual-test"
        self.evidence = self.home / "agent_relay_tracker" / "page-verification" / self.session / "evidence"
        self.evidence.mkdir(parents=True)
        self.image = self.evidence / "desktop.png"
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                browser_page = browser.new_page(viewport={"width": 800, "height": 600})
                browser_page.set_content("""
                    <body style="margin:0;background:#16324f;color:#f8fafc;font-family:sans-serif">
                      <header style="height:70px;background:#0f766e;padding:16px">AgentRelay 页面</header>
                      <main style="display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:24px">
                        <section style="height:180px;background:#1d4ed8;padding:16px">目标按钮和表格内容</section>
                        <section style="height:180px;background:#b45309;padding:16px">目标外检查区域</section>
                        <section style="height:180px;background:#475569;padding:16px">底部内容 A</section>
                        <section style="height:180px;background:#7e22ce;padding:16px">底部内容 B</section>
                      </main>
                    </body>
                """)
                browser_page.screenshot(path=str(self.image))
            finally:
                browser.close()
        page.write_state(self.home, self.session, {
            "files": {"index.html": {"sha256": "abc"}},
            "verification": {"status": "passed", "browser": "chromium", "screenshots": [str(self.image)]},
        })

    def new_screenshot(self, name, color):
        path = self.evidence / name
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 320, "height": 180})
                page.set_content(f'<body style="margin:0;background:{color}"></body>')
                page.screenshot(path=str(path))
            finally:
                browser.close()
        return path

    def test_native_review_requires_target_and_unchanged_checks(self):
        with self.assertRaisesRegex(ValueError, "非目标"):
            review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image)],
                                 target="左上角按钮", unchanged_checks=[], passed=True, summary="ok")
        result = review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image)],
                                     target="左上角按钮", unchanged_checks=["导出按钮不移位"],
                                     passed=True, summary="整页和目标外均正常",
                                     self_reviewed=True,
                                     attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                                     observations={
                                         "target": "按钮显示在左上角，宽度 120px，颜色 rgb(15, 118, 110)",
                                         "导出按钮不移位": "before/after rect 相同",
                                     })
        self.assertEqual(result["status"], "passed")
        self.assertIsNone(review.completion_block(self.home, self.session))

    def test_unavailable_cannot_claim_visual_pass(self):
        with self.assertRaisesRegex(ValueError, "无视觉"):
            review.record_review(self.home, self.session, mode="unavailable", screenshots=[str(self.image)],
                                 target="按钮", unchanged_checks=["其他模块"], passed=True, summary="假通过")
        result = review.record_review(self.home, self.session, mode="unavailable", screenshots=[str(self.image)],
                                     target="按钮", unchanged_checks=["其他模块"], passed=False,
                                     summary="无视觉，已先检查 DOM/布局/diff，需用户复查")
        self.assertEqual(result["status"], "user_review_required")
        self.assertIn("用户复查", review.completion_block(self.home, self.session))

    def test_completion_block_reports_browser_failure_instead_of_missing_review(self):
        state = page.read_state(self.home, self.session)
        state.pop("visual_review", None)
        state["verification"] = {
            "status": "failed",
            "screenshots": [],
            "errors": [
                "interaction: Locator.fill: Timeout 30000ms exceeded; element is not editable"
            ],
        }
        page.write_state(self.home, self.session, state)

        reason = review.completion_block(self.home, self.session, "已查看截图，页面完成")
        self.assertIn("真实浏览器检查失败", reason)
        self.assertIn("element is not editable", reason)
        self.assertIn("verify_page.py", reason)
        self.assertNotIn("缺少真实截图的视觉审查记录", reason)

    def test_nonvisual_still_requires_real_nonvisual_checks(self):
        with self.assertRaisesRegex(ValueError, "结构化"):
            review.record_nonvisual_review(self.home, self.session, target="按钮",
                                           unchanged_checks=["其他模块"], summary="已检查")
        state = page.read_state(self.home, self.session)
        state["verification"] = {
            "status": "passed", "browser": "chromium", "screenshots": [],
            "nonvisual_checks": [
                {"check": "dom", "passed": True, "actual": "#export computed style 与 rect 已读取"},
                {"check": "console", "passed": True, "actual": "0 errors"},
                {"check": "diff", "passed": True, "actual": "仅 page.html 变更"},
                {"check": "interaction", "passed": True, "actual": "按钮可点击，表单可输入"},
                {"check": "其他模块", "passed": True, "actual": "before/after rect 相同"},
            ],
        }
        page.write_state(self.home, self.session, state)
        result = review.record_nonvisual_review(self.home, self.session, target="按钮",
                                               unchanged_checks=["其他模块"], summary="非视觉检查完成，请用户看页面")
        self.assertEqual(result["status"], "user_review_required")
        self.assertIn("用户复查", review.completion_block(self.home, self.session))

    def test_cleanup_deletes_only_agentrelay_evidence(self):
        review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image)],
                             target="按钮", unchanged_checks=["其他模块"], passed=True, summary="ok",
                             self_reviewed=True,
                             attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                             observations={"target": "按钮显示在左上角，宽度 120px，颜色与预期一致",
                                           "其他模块": "before/after rect 相同"})
        outside = self.home / "keep.png"
        outside.write_bytes(b"keep")
        removed = review.cleanup_screenshots(self.home, self.session)
        self.assertEqual(removed, [str(self.image)])
        self.assertFalse(self.image.exists())
        self.assertTrue(outside.exists())
        state = review.read_state(self.home, self.session)
        self.assertTrue(state["cleanup_completed"])
        self.assertTrue(state["visual_review"]["screenshots_deleted"])

    def test_cleanup_cli_accepts_only_explicit_disposable_temp_screenshots(self):
        temp = tempfile.TemporaryDirectory(prefix="agentrelay-review-")
        self.addCleanup(temp.cleanup)
        temp_root = Path(temp.name).resolve()
        screenshot = temp_root / "dcm-skeleton-review-final.png"
        target_dir = temp_root / "dcm-analysis-browser"
        target_dir.mkdir()
        target_screenshot = target_dir / "config-desktop.png"
        ordinary = temp_root / "important.png"
        user_input = temp_root / "orca-paste-123.png"
        for path in (screenshot, target_screenshot, ordinary, user_input):
            path.write_bytes(b"\x89PNG\r\n\x1a\n")

        for path in (ordinary, user_input):
            with self.assertRaisesRegex(ValueError, "只能清理"):
                review.cleanup_screenshots(self.home, self.session, [str(path)])
            self.assertTrue(path.exists())

        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "visual_review.py"),
                "cleanup",
                "--session", self.session,
                "--home", str(self.home),
                "--screenshot", str(screenshot),
                "--screenshot", str(target_screenshot),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIn(str(screenshot), result["deleted"])
        self.assertIn(str(target_screenshot), result["deleted"])
        self.assertIn(str(self.image), result["deleted"])
        self.assertTrue(result["cleanup_completed"])
        self.assertFalse(screenshot.exists())
        self.assertFalse(target_screenshot.exists())
        self.assertFalse(self.image.exists())
        self.assertTrue(ordinary.exists())
        self.assertTrue(user_input.exists())

    def test_visual_cli_returns_actionable_json_instead_of_traceback(self):
        unregistered = self.evidence / "unregistered.png"
        unregistered.write_bytes(self.image.read_bytes())
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "visual_review.py"),
                "record",
                "--session", self.session,
                "--home", str(self.home),
                "--target", "按钮",
                "--unchanged", "其他模块",
                "--screenshot", str(unregistered),
                "--summary", "视觉检查",
                "--observation", "target=按钮位于左上角，宽度 120px，颜色 rgb(15, 118, 110)",
                "--observation", "其他模块=before/after rect 相同",
                "--self-reviewed",
                "--attestation", "已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                "--passed",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        error = json.loads(completed.stderr)
        self.assertEqual(error["status"], "error")
        self.assertIn("verify_page.py", error["hint"])
        self.assertIn("verification.screenshots", error["hint"])

    def test_invariants_compare_real_positions_and_dimensions(self):
        before = [{"selector": "#other", "before": {"rect": {"x": 200, "y": 20, "width": 80, "height": 30}}}]
        same = [{"selector": "#other", "before": {"rect": {"x": 200, "y": 20, "width": 80, "height": 30}}}]
        self.assertTrue(review.verify_invariants(before, same)[0]["passed"])
        moved = [{"selector": "#other", "before": {"rect": {"x": 40, "y": 20, "width": 80, "height": 30}}}]
        self.assertFalse(review.verify_invariants(before, moved)[0]["passed"])

    def test_invariants_ignore_viewport_scroll_but_not_document_movement(self):
        before = [{
            "selector": "#other",
            "before": {
                "rect": {"x": 200, "y": 20, "width": 80, "height": 30},
                "document_rect": {"x": 200, "y": 120, "width": 80, "height": 30},
            },
        }]
        scrolled = [{
            "selector": "#other",
            "before": {
                "rect": {"x": 200, "y": 60, "width": 80, "height": 30},
                "document_rect": {"x": 200, "y": 120, "width": 80, "height": 30},
            },
        }]
        moved = [{
            "selector": "#other",
            "before": {
                "rect": {"x": 200, "y": 100, "width": 80, "height": 30},
                "document_rect": {"x": 200, "y": 160, "width": 80, "height": 30},
            },
        }]
        self.assertTrue(review.verify_invariants(before, scrolled)[0]["passed"])
        self.assertFalse(review.verify_invariants(before, moved)[0]["passed"])

    def test_target_verification_requires_expected_style_change(self):
        before = {"selector": "#export", "styles": {"color": "rgb(255, 0, 0)", "width": "120px"}}
        after = {"selector": "#export", "styles": {"color": "rgb(0, 128, 0)", "width": "120px"}}
        result = review.record_target_verification(self.home, self.session, before=before,
            after=after, expected={"color": "rgb(0, 128, 0)"}, unchanged_checks=["导出按钮不移位"])
        self.assertTrue(result["target_verified"])
        wrong = review.record_target_verification(self.home, self.session, before=before,
            after=before, expected={"color": "rgb(0, 128, 0)"}, unchanged_checks=["导出按钮不移位"])
        self.assertFalse(wrong["target_verified"])
        self.assertIn("没有变成预期", review.completion_block(self.home, self.session))
        unchanged = review.record_target_verification(
            self.home, self.session,
            before={"selector": "#export", "styles": {"color": "rgb(0, 128, 0)"}},
            after={"selector": "#export", "styles": {"color": "rgb(0, 128, 0)"}},
            expected={"color": "rgb(0, 128, 0)"},
            unchanged_checks=["导出按钮不移位"],
        )
        self.assertFalse(unchanged["observed_change"])
        self.assertFalse(unchanged["target_verified"])

    def test_visual_review_rejects_symlinked_screenshots(self):
        linked = self.evidence / "linked.png"
        linked.symlink_to(self.image)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            review.record_review(
                self.home, self.session, mode="native", screenshots=[str(linked)],
                target="按钮", unchanged_checks=["其他模块"], passed=True,
                summary="符号链接不能作为证据", self_reviewed=True,
                attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                observations={"target": "按钮显示在左上角，宽度 120px",
                              "其他模块": "before/after rect 相同"},
            )
        state = page.read_state(self.home, self.session)
        state["verification"]["screenshots"] = [str(linked)]
        page.write_state(self.home, self.session, state)
        with self.assertRaisesRegex(ValueError, "登记截图不能是符号链接"):
            review.cleanup_screenshots(self.home, self.session)

    def test_visual_review_rejects_screenshot_replaced_inside_state_lock(self):
        replacement = self.new_screenshot("replacement-race.png", "#111827")
        original_mutate = review._verification_state.mutate_state

        def replace_before_update(home, session_id, update):
            self.image.write_bytes(replacement.read_bytes())
            return original_mutate(home, session_id, update)

        with mock.patch.object(
            review._verification_state,
            "mutate_state",
            side_effect=replace_before_update,
        ):
            with self.assertRaisesRegex(ValueError, "写入前发生变化"):
                review.record_review(
                    self.home, self.session, mode="native", screenshots=[str(self.image)],
                    target="按钮", unchanged_checks=["其他模块"], passed=True,
                    summary="锁内截图变化", self_reviewed=True,
                    attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                    observations={"target": "按钮显示在左上角，宽度 120px",
                                  "其他模块": "before/after rect 相同"},
                )

    def test_cleanup_rejects_screenshot_replaced_after_review(self):
        review.record_review(
            self.home, self.session, mode="native", screenshots=[str(self.image)],
            target="按钮", unchanged_checks=["其他模块"], passed=True, summary="真实图片",
            self_reviewed=True,
            attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
            observations={"target": "按钮显示在左上角，宽度 120px",
                          "其他模块": "before/after rect 相同"},
        )
        replacement = self.new_screenshot("replacement-cleanup.png", "#111827")
        self.image.write_bytes(replacement.read_bytes())
        with self.assertRaisesRegex(ValueError, "视觉审查后发生变化"):
            review.cleanup_screenshots(self.home, self.session)
        self.assertFalse(review.read_state(self.home, self.session).get("cleanup_completed"))

    def test_target_changes_can_require_difference_without_exact_color(self):
        before = {"selector": "#button", "styles": {"color": "red"}}
        after = {"selector": "#button", "styles": {"color": "green"}}
        self.assertEqual(review.target_changes(before, after, {"color": "!=red"}), {"color": True})
        self.assertEqual(review.target_changes(before, before, {"color": "!=red"}), {"color": False})

    def test_native_requires_real_images_and_itemized_observations(self):
        fake = self.evidence / "fake.png"
        fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not-an-image")
        state = page.read_state(self.home, self.session)
        state["verification"]["screenshots"] = [str(fake)]
        page.write_state(self.home, self.session, state)
        with self.assertRaisesRegex(ValueError, "图片内容"):
            review.record_review(self.home, self.session, mode="native", screenshots=[str(fake)],
                                 target="按钮", unchanged_checks=["其他模块"], passed=True, summary="假图",
                                 observations={"target": "无法判断", "其他模块": "无法判断"})
        state["verification"]["screenshots"] = [str(self.image)]
        page.write_state(self.home, self.session, state)
        with self.assertRaisesRegex(ValueError, "逐项观察"):
            review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image)],
                                 target="按钮", unchanged_checks=["其他模块"], passed=True, summary="未逐项检查")

    def test_visual_record_and_cleanup_resolve_symlinked_temp_paths(self):
        relative = Path(str(self.home).replace("/private/var/", "/var/", 1))
        result = review.record_review(relative, self.session, mode="native", screenshots=[str(self.image)],
                                     target="按钮", unchanged_checks=["其他模块"], passed=True, summary="真实图片",
                                     self_reviewed=True,
                                     attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                                     observations={"target": "按钮显示在左上角，宽度 120px",
                                                   "其他模块": "before/after rect 不变"})
        self.assertEqual(result["status"], "passed")
        removed = review.cleanup_screenshots(relative, self.session)
        self.assertEqual(len(removed), 1)
        self.assertFalse(self.image.exists())

    def test_visual_certificate_requires_signature_and_distinct_multi_target_images(self):
        with self.assertRaisesRegex(ValueError, "自评审"):
            review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image)],
                                 target="按钮", unchanged_checks=["其他模块"], passed=True, summary="无签名",
                                 observations={"target": "按钮显示在左上角，宽度 120px",
                                               "其他模块": "before/after rect 相同"})
        with self.assertRaisesRegex(ValueError, "自评审"):
            review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image)],
                                 target="按钮", unchanged_checks=["其他模块"], passed=True, summary="仅写签名文字",
                                 attestation="已逐项读取截图并对照页面",
                                 observations={"target": "按钮显示在左上角，宽度 120px",
                                               "其他模块": "before/after rect 相同"})
        with self.assertRaisesRegex(ValueError, "逐项绑定截图"):
            review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image)],
                                 target="", target_items=["按钮A", "按钮B"], unchanged_checks=["其他模块"],
                                 passed=True, summary="多目标", self_reviewed=True,
                                 attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                                 observations={"按钮A": "按钮显示在顶部，宽度 120px",
                                               "按钮B": "按钮显示在底部，宽度 120px",
                                               "其他模块": "before/after rect 相同"})
        second = self.evidence / "second.png"
        second.write_bytes(self.image.read_bytes())
        state = page.read_state(self.home, self.session)
        state["verification"]["screenshots"].append(str(second))
        page.write_state(self.home, self.session, state)
        with self.assertRaisesRegex(ValueError, "复用同一张截图"):
            review.record_review(self.home, self.session, mode="native", screenshots=[str(self.image), str(second)],
                                 target="", target_items=["按钮A", "按钮B"], unchanged_checks=["其他模块"],
                                 item_screenshots={"按钮A": str(self.image), "按钮B": str(second)},
                                 passed=True, summary="多目标", self_reviewed=True,
                                 attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                                 observations={"按钮A": "按钮显示在顶部，宽度 120px",
                                               "按钮B": "按钮显示在底部，宽度 120px",
                                               "其他模块": "before/after rect 相同"})

    def test_visual_duplicate_detection_uses_decoded_pixels_not_file_bytes(self):
        from PIL import Image, PngImagePlugin

        second = self.evidence / "same-pixels.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("review-copy", "metadata-only variation")
        with Image.open(self.image) as source:
            source.save(second, pnginfo=metadata)
        self.assertNotEqual(
            hashlib.sha256(self.image.read_bytes()).hexdigest(),
            hashlib.sha256(second.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            review._screenshot_digest(str(self.image)),
            review._screenshot_digest(str(second)),
        )
        state = page.read_state(self.home, self.session)
        state["verification"]["screenshots"].append(str(second))
        page.write_state(self.home, self.session, state)
        with self.assertRaisesRegex(ValueError, "复用同一张截图"):
            review.record_review(
                self.home, self.session, mode="native",
                screenshots=[str(self.image), str(second)],
                target="", target_items=["按钮A", "按钮B"],
                item_screenshots={"按钮A": str(self.image), "按钮B": str(second)},
                unchanged_checks=["其他模块"], passed=True, summary="元数据变体",
                self_reviewed=True,
                attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                observations={"按钮A": "按钮显示在顶部，宽度 120px",
                              "按钮B": "按钮显示在底部，宽度 120px",
                              "其他模块": "before/after rect 相同"},
            )

    def test_visual_claim_detects_original_scope_with_honest_limitation(self):
        message = (
            "是的，我亲自检查过。使用真实 Chromium 打开 5174，操作了弹框的取消、确定，"
            "以及 1–4 象限的放大、还原。截取并实际查看了第 1 象限放大后的截图。"
            "补充准确范围：截图只人工查看了第 1 象限；另外三张通过浏览器操作和尺寸测量验证。"
        )
        self.assertTrue(review.visual_claim(message))
        self.assertEqual(review._claimed_scope_count(message), 4)
        self.assertFalse(review.visual_claim("没有检查截图，无法判断页面。"))
        self.assertFalse(review.visual_claim(
            "我刚才做的是像素统计，不是对页面进行逐项目标视觉检查；"
            "当前会话没有视觉认证证书，需用户复查。"
        ))
        self.assertFalse(review.visual_claim(
            "本次未完成视觉验收，不能登记为通过。"
        ))
        self.assertFalse(review.visual_claim(
            "左右留白调整截图已实际查看，但 AgentRelay 未记录有效的视觉验收结果，"
            "因此不标记其验收通过。"
        ))
        self.assertFalse(review.visual_claim(
            "已停止，不再声明完成或视觉验收通过，等待后续指示。"
        ))
        self.assertFalse(review.visual_claim(
            "我已亲自查看截图，但没有登记视觉验收证书。"
        ))
        self.assertTrue(review.visual_claim(
            "没有发现问题，我已亲自查看截图，页面布局正常。"
        ))
        self.assertTrue(review.visual_claim(
            "我未逐项检查，但已亲自查看截图，页面正常。"
        ))
        self.assertTrue(review.visual_claim(
            "我未逐项检查但已亲自查看截图"
        ))
        self.assertTrue(review.visual_claim(
            "没有发现问题但已亲自查看截图"
        ))
        self.assertFalse(review.visual_claim(
            "我已亲自查看截图但没有登记视觉验收证书"
        ))
        self.assertFalse(review.visual_claim(
            "没有发现问题但没有查看截图"
        ))
        self.assertFalse(review.visual_claim(
            "验证将覆盖骨架加载、无数据、完整指标文字、固定列与排序，并截图检查。"
            "请确认后修改。"
        ))
        self.assertFalse(review.visual_claim(
            "方案已列出，尚未修改；后续完成后再截图检查。"
        ))
        self.assertFalse(review.visual_claim(
            "旧规则只识别到“截图检查”，没有识别前面的计划语境。"
        ))
        self.assertFalse(review.visual_claim(
            "已修正为：“已亲自检查、已逐项核对、验证通过”等肯定完成声明仍然触发。"
        ))
        self.assertFalse(review.visual_claim(
            "误判已复现并修正：正向“我亲自检查了截图”仍返回 True。"
        ))
        self.assertFalse(review.visual_claim(
            "安装副本已同步，相关 40 个测试全部通过，语法检查和 git diff --check 也通过。"
        ))
        self.assertFalse(review.visual_claim(
            "这个门禁用于判断页面是否已经被检查过，以及截图检查是否通过。"
        ))
        self.assertFalse(review.visual_claim(
            "截图检查通过了吗？"
        ))
        self.assertTrue(review.visual_claim(
            "我检查后门禁仍触发；我已亲自查看截图，页面布局正常。"
        ))

    def test_scope_claim_cannot_exceed_itemized_certificate(self):
        review.record_review(
            self.home, self.session, mode="native", screenshots=[str(self.image)],
            target="第1象限", unchanged_checks=["其他模块"], passed=True, summary="只认证一个目标",
            self_reviewed=True, attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
            observations={"第1象限": "表格显示 4 行，列宽对齐，底部无大块空白",
                          "其他模块": "before/after rect 相同"},
        )
        reason = review.completion_block(
            self.home, self.session,
            "操作了 1–4 象限的放大、还原；截图只人工查看了第 1 象限。",
        )
        self.assertIn("4 个目标", reason)

    def test_four_item_certificate_requires_and_accepts_four_distinct_screenshots(self):
        items = ["第1象限", "第2象限", "第3象限", "第4象限"]
        images = [
            self.image,
            self.new_screenshot("q2.png", "#e53935"),
            self.new_screenshot("q3.png", "#43a047"),
            self.new_screenshot("q4.png", "#1e88e5"),
        ]
        state = page.read_state(self.home, self.session)
        state["verification"]["screenshots"].extend(str(path) for path in images[1:])
        page.write_state(self.home, self.session, state)
        result = review.record_review(
            self.home, self.session, mode="native", screenshots=[str(path) for path in images],
            target="", target_items=items,
            item_screenshots={item: str(path) for item, path in zip(items, images)},
            unchanged_checks=["弹框取消", "弹框确定"], passed=True, summary="四象限逐项查看",
            self_reviewed=True, attestation="已逐项读取四张截图，核对可见内容、布局间距、对齐和空白溢出",
            observations={
                **{item: f"{item}显示表格 4 行，列宽对齐，底部无大块空白" for item in items},
                "弹框取消": "关闭后弹框不可见，before/after 表格 rect 相同",
                "弹框确定": "确认后数据刷新，表格仍为 4 行且列间距 16px",
            },
        )
        self.assertEqual(result["certification"]["coverage"]["targets"], 4)
        self.assertEqual(len(set(result["certification"]["item_screenshot_sha256"].values())), 4)
        self.assertIsNone(review.completion_block(
            self.home, self.session,
            "操作了 1–4 象限的放大、还原，并逐项查看四张截图。",
        ))

    def test_agent_image_success_requires_itemized_answer(self):
        script = self.home / "skills/agent-image/scripts/agent_image.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('target: 通过: 按钮显示在左上角，宽度 120px；其他模块: 通过: before/after rect 相同')\n",
                          encoding="utf-8")
        with mock.patch("subprocess.run") as call:
            call.return_value = subprocess.CompletedProcess(
                [], 0,
                "target: 通过: 按钮显示在左上角，宽度 120px；其他模块: 通过: before/after rect 相同",
                "",
            )
            result = review.review_with_agent_image(self.home, self.session, screenshots=[str(self.image)],
                                                   target="target", unchanged_checks=["其他模块"])
        self.assertEqual(result["status"], "passed")
        call.return_value = subprocess.CompletedProcess([], 0, "看起来没问题", "")
        result = review.review_with_agent_image(self.home, self.session, screenshots=[str(self.image)],
                                               target="target", unchanged_checks=["其他模块"])
        self.assertEqual(result["status"], "user_review_required")

    def test_agent_image_missing_script_degrades_to_user_review(self):
        result = review.review_with_agent_image(self.home, self.session, screenshots=[str(self.image)],
                                               target="按钮", unchanged_checks=["其他模块"])
        self.assertEqual(result["status"], "user_review_required")
        self.assertIn("无视觉", result["summary"])

    def test_generic_observation_cannot_become_visual_pass(self):
        with self.assertRaisesRegex(ValueError, "观察过于笼统"):
            review.record_review(
                self.home, self.session, mode="native", screenshots=[str(self.image)],
                target="按钮", unchanged_checks=["其他模块"], passed=True,
                summary="泛泛描述", self_reviewed=True,
                attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                observations={"target": "符合", "其他模块": "无变化"},
            )
        with self.assertRaisesRegex(ValueError, "具体证据"):
            review.record_review(
                self.home, self.session, mode="native", screenshots=[str(self.image)],
                target="按钮", unchanged_checks=["其他模块"], passed=True,
                summary="只写元素状态", self_reviewed=True,
                attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                observations={"target": "按钮符合", "其他模块": "位置尺寸正常"},
            )

    def test_large_blank_screenshot_cannot_become_visual_pass(self):
        blank = self.new_screenshot("blank.png", "#ffffff")
        state = page.read_state(self.home, self.session)
        state["verification"]["screenshots"].append(str(blank))
        page.write_state(self.home, self.session, state)
        with self.assertRaisesRegex(ValueError, "大块空白"):
            review.record_review(
                self.home, self.session, mode="native", screenshots=[str(blank)],
                target="结果表", unchanged_checks=["其他模块"], passed=True,
                summary="空白页不能通过", self_reviewed=True,
                attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                observations={"target": "表格显示 2 行，底部空白符合", "其他模块": "rect 相同"},
            )

    def test_centered_blank_region_is_detected_with_colored_sidebars(self):
        blank = self.evidence / "centered-blank.png"
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                browser_page = browser.new_page(viewport={"width": 800, "height": 600})
                browser_page.set_content("""
                    <body style="margin:0;background:#1e293b">
                      <div style="width:15%;height:600px;float:left;background:#0f766e"></div>
                      <div style="width:65%;height:600px;float:left;background:#ffffff"></div>
                      <div style="width:20%;height:600px;float:left;background:#b45309"></div>
                    </body>
                """)
                browser_page.screenshot(path=str(blank))
            finally:
                browser.close()
        state = page.read_state(self.home, self.session)
        state["verification"]["screenshots"].append(str(blank))
        page.write_state(self.home, self.session, state)
        with self.assertRaisesRegex(ValueError, "大块空白"):
            review.record_review(
                self.home, self.session, mode="native", screenshots=[str(blank)],
                target="结果表", unchanged_checks=["其他模块"], passed=True,
                summary="中间空白页不能通过", self_reviewed=True,
                attestation="已逐项读取截图，核对可见内容、布局间距、对齐和空白溢出",
                observations={"target": "表格显示 2 行，底部空白符合", "其他模块": "rect 相同"},
            )


if __name__ == "__main__":
    unittest.main()
