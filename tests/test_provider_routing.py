import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from agent_relay import DeepSeekAdapter  # noqa: E402
from agent_relay_runtime import SessionRef  # noqa: E402


class DeepSeekRoutingTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DeepSeekAdapter()

    def test_hybrid_mode_uses_single_unified_conversation(self):
        # 混合模式 = 一套统一会话，同时处理极速和专家提问
        self.assertEqual(self.adapter.session_scope("default"), "hybrid")
        self.assertEqual(self.adapter.session_scope("expert"), "hybrid")
        self.assertEqual(
            self.adapter.canonical_session_title("default"),
            "AgentRelay-DeepSeek",
        )
        self.assertEqual(
            self.adapter.canonical_session_title("expert"),
            "AgentRelay-DeepSeek",
        )

    def test_session_is_parsed_from_route_not_date_text(self):
        session = self.adapter._session_from_link(
            "专家思考模式对话",
            "/a/chat/s/abc-123",
        )
        self.assertEqual(session.session_id, "abc-123")
        self.assertEqual(
            session.href,
            "https://chat.deepseek.com/a/chat/s/abc-123",
        )

    def test_external_session_link_is_rejected(self):
        self.assertIsNone(
            self.adapter._session_from_link(
                "fake",
                "https://example.com/a/chat/s/abc-123",
            )
        )

    def test_hybrid_discovery_uses_unified_session(self):
        sessions = [
            SessionRef(
                provider="deepseek",
                session_id="unified",
                title="AgentRelay-DeepSeek",
                href="https://chat.deepseek.com/a/chat/s/unified",
            ),
            SessionRef(
                provider="deepseek",
                session_id="legacy",
                title="AgentRelay-DeepSeek-Flash",
                href="https://chat.deepseek.com/a/chat/s/legacy",
            ),
        ]
        default_target = self.adapter.find_target_session(
            sessions,
            mode="default",
        )
        expert_target = self.adapter.find_target_session(
            sessions,
            mode="expert",
        )
        self.assertEqual(default_target.session_id, "unified")
        self.assertEqual(expert_target.session_id, "unified")


if __name__ == "__main__":
    unittest.main()
