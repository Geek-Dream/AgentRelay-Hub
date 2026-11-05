import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import config_manager  # noqa: E402
from agent_relay import DeepSeekAdapter, ProviderRegistry, SiteDetector  # noqa: E402
from generic_web_adapter import GenericWebAdapter  # noqa: E402


def fake_page(url: str):
    return SimpleNamespace(url=url)


class GenericAdapterTests(unittest.TestCase):
    def test_marked_as_fallback(self):
        self.assertTrue(GenericWebAdapter().fallback)
        self.assertFalse(DeepSeekAdapter().fallback)

    def test_registry_falls_back_to_generic_for_unknown_provider(self):
        registry = ProviderRegistry()
        adapter = registry.create("qianwen")
        self.assertIsInstance(adapter, GenericWebAdapter)
        self.assertEqual(adapter.name, "qianwen")
        self.assertTrue(adapter.fallback)

    def test_registry_keeps_specific_adapter_for_deepseek(self):
        adapter = ProviderRegistry().create("deepseek")
        self.assertIsInstance(adapter, DeepSeekAdapter)

    def test_registry_placeholder_not_in_create_all(self):
        names = [a.name for a in ProviderRegistry().create_all()]
        self.assertIn("deepseek", names)
        self.assertNotIn("generic", names)

    def test_registry_rejects_invalid_provider_name(self):
        with self.assertRaises(ValueError):
            ProviderRegistry().create("Bad Name!")

    def test_is_match_host_rules(self):
        adapter = GenericWebAdapter(
            provider_name="qianwen", base_url="https://qianwen.com/"
        )
        self.assertTrue(adapter.is_match(fake_page("https://www.qianwen.com/chat/x")))
        self.assertTrue(adapter.is_match(fake_page("https://qianwen.com/")))
        self.assertFalse(adapter.is_match(fake_page("https://deepseek.com/")))
        self.assertFalse(adapter.is_match(fake_page("https://evil-qianwen.com/")))
        self.assertFalse(adapter.is_match(fake_page("about:blank")))

    def test_unconfigured_adapter_matches_nothing(self):
        adapter = GenericWebAdapter()
        self.assertFalse(adapter.is_match(fake_page("https://www.qianwen.com/")))

    def test_wait_for_page_without_url_raises(self):
        adapter = GenericWebAdapter()
        with self.assertRaises(RuntimeError):
            adapter.wait_for_page(None)

    def test_session_from_link(self):
        adapter = GenericWebAdapter(
            provider_name="qianwen", base_url="https://www.qianwen.com/"
        )
        session = adapter._session_from_link("  我的\n会话 ", "/chat/abc123")
        self.assertIsNotNone(session)
        self.assertEqual(session.session_id, "abc123")
        self.assertEqual(session.title, "我的 会话")
        self.assertEqual(session.provider, "qianwen")
        session = adapter._session_from_link("t", "/c/xyz")
        self.assertIsNotNone(session)
        self.assertEqual(session.session_id, "xyz")
        self.assertIsNone(adapter._session_from_link("t", "/settings"))
        self.assertIsNone(adapter._session_from_link("t", "javascript:void(0)"))
        self.assertIsNone(
            adapter._session_from_link("t", "https://other.com/chat/xyz")
        )

    def test_session_scope(self):
        # 未启用混合时：极速/专家各用各的 scope
        with mock.patch.object(config_manager, "load_config", return_value={}):
            adapter = GenericWebAdapter(provider_name="qianwen")
            self.assertEqual(adapter.session_scope("expert"), "expert")
            self.assertEqual(adapter.session_scope("flash"), "flash")

    def test_canonical_session_title_defaults(self):
        with mock.patch.object(config_manager, "load_config", return_value={}):
            adapter = GenericWebAdapter(provider_name="qianwen")
            self.assertEqual(
                adapter.canonical_session_title("flash"),
                "AgentRelay-Qianwen-Flash",
            )
            self.assertEqual(
                adapter.canonical_session_title("expert"),
                "AgentRelay-Qianwen-Expert",
            )
            adapter = GenericWebAdapter(provider_name="my-site")
            self.assertEqual(
                adapter.canonical_session_title("flash"),
                "AgentRelay-My-Site-Flash",
            )

    def test_canonical_session_title_from_config(self):
        config = {
            "web_providers": [
                {
                    "id": "qianwen",
                    "conversation": {
                        "supports_hybrid": True,
                        "titles": {
                            "hybrid": "AgentRelay-Qianwen",
                        },
                    },
                }
            ]
        }
        with mock.patch.object(config_manager, "load_config", return_value=config):
            adapter = GenericWebAdapter(provider_name="qianwen")
            # 混合模式：极速和专家提问都使用统一会话名称
            self.assertEqual(
                adapter.canonical_session_title("expert"),
                "AgentRelay-Qianwen",
            )
            self.assertEqual(
                adapter.canonical_session_title("flash"),
                "AgentRelay-Qianwen",
            )
            self.assertEqual(adapter.session_scope("expert"), "hybrid")
            self.assertEqual(adapter.session_scope("flash"), "hybrid")

    def test_detector_prefers_specific_adapter(self):
        registry = ProviderRegistry()
        detector = SiteDetector(registry, provider_name="deepseek")
        detected = detector.detect(fake_page("https://chat.deepseek.com/a/chat/s/123"))
        self.assertIsInstance(detected, DeepSeekAdapter)

    def test_detector_falls_back_to_named_generic(self):
        config = {
            "web_providers": [
                {"id": "qianwen", "base_url": "https://www.qianwen.com/"}
            ]
        }
        with mock.patch.object(config_manager, "load_config", return_value=config):
            registry = ProviderRegistry()
            detector = SiteDetector(registry, provider_name="qianwen")
            detected = detector.detect(fake_page("https://www.qianwen.com/chat/abc"))
        self.assertIsInstance(detected, GenericWebAdapter)
        self.assertEqual(detected.name, "qianwen")

    def test_detector_generic_is_last(self):
        registry = ProviderRegistry()
        detector = SiteDetector(registry, provider_name="qianwen")
        self.assertFalse(detector.adapters[-1].fallback is False)
        self.assertFalse(any(a.fallback for a in detector.adapters[:-1]))

    def test_normalize_text_removes_zero_width(self):
        self.assertEqual(
            GenericWebAdapter.normalize_text("a\u200bb\u200cc\u200dd\ufeff e\nf"),
            "abcd e f",
        )

    def test_session_from_query_param(self):
        adapter = GenericWebAdapter(
            provider_name="qianwen", base_url="https://www.qianwen.com/"
        )
        session = adapter._session_from_link("t", "/chat?chatId=abc123")
        self.assertIsNotNone(session)
        self.assertEqual(session.session_id, "abc123")

    def test_spa_session_reference_roundtrip(self):
        from agent_relay_runtime import SessionRef
        adapter = GenericWebAdapter(
            provider_name="qianwen", base_url="https://www.qianwen.com/"
        )
        session = SessionRef(
            provider="qianwen",
            session_id="f8921870c48042c9af1eb8a47490fa3c",
            title="",
            href="https://www.qianwen.com/#f8921870c48042c9af1eb8a47490fa3c",
        )
        self.assertTrue(GenericWebAdapter._is_spa_ref(session))
        self.assertTrue(adapter.is_session_reference_valid(session))

    def test_headful_defaults_and_env_override(self):
        import os
        old = os.environ.pop("AGENT_RELAY_WEB_HEADFUL", None)
        try:
            self.assertTrue(GenericWebAdapter()._headed)
            os.environ["AGENT_RELAY_WEB_HEADFUL"] = "0"
            self.assertFalse(GenericWebAdapter()._headed)
            os.environ["AGENT_RELAY_WEB_HEADFUL"] = "1"
            self.assertTrue(GenericWebAdapter()._headed)
        finally:
            if old is None:
                os.environ.pop("AGENT_RELAY_WEB_HEADFUL", None)
            else:
                os.environ["AGENT_RELAY_WEB_HEADFUL"] = old

    def test_spa_discovery_and_binding_flow(self):
        from unittest import mock as _mock
        from agent_relay_runtime import SessionBindingStore
        import tempfile
        adapter = GenericWebAdapter(
            provider_name="qianwen", base_url="https://www.qianwen.com/"
        )
        # 模拟 list_sessions 的 SPA 发现结果结构
        session = adapter.list_sessions(fake_page("https://www.qianwen.com/"))  # 无 DOM，返回空
        self.assertEqual(session, [])
        store = SessionBindingStore(Path(tempfile.mkdtemp()) / "bindings.json")
        ref = adapter.current_session(fake_page("https://www.qianwen.com/chat/abc123"), "标题")
        self.assertIsNotNone(ref)
        store.put("flash", ref)
        self.assertEqual(store.get("qianwen", "flash").session_id, "abc123")


if __name__ == "__main__":
    unittest.main()
