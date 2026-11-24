import base64
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agent_image  # noqa: E402
import config_manager  # noqa: E402

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _config(**overrides):
    config = config_manager.default_config()
    config.update(overrides)
    return config


def _web(pid, images=None, enabled=True):
    entry = {"id": pid, "name": pid, "enabled": enabled,
             "conversation": {"modes": ["hybrid"]}}
    if images is not None:
        entry["conversation"]["images"] = {"hybrid": images}
    return entry


class WebCandidateTests(unittest.TestCase):
    def test_default_provider_sorted_first(self):
        config = _config(web_providers=[
            _web("qianwen-web", images=True),
            _web("deepseek-web", images=True),
        ], default_provider="deepseek-web", web_assist={"enabled": True})
        candidates = agent_image.image_capable_web_providers(config)
        self.assertEqual([item["id"] for item in candidates],
                         ["deepseek-web", "qianwen-web"])

    def test_providers_without_image_capability_excluded(self):
        config = _config(web_providers=[
            _web("deepseek-web", images=False),
            _web("qianwen-web", images=True),
        ], web_assist={"enabled": True})
        candidates = agent_image.image_capable_web_providers(config)
        self.assertEqual([item["id"] for item in candidates], ["qianwen-web"])

    def test_disabled_providers_excluded(self):
        config = _config(
            web_providers=[_web("deepseek-web", images=True, enabled=False)],
            web_assist={"enabled": True},
        )
        self.assertEqual(agent_image.image_capable_web_providers(config), [])

    def test_beta_off_excludes_every_web_provider(self):
        config = _config(web_providers=[_web("deepseek-web", images=True)])
        self.assertEqual(agent_image.image_capable_web_providers(config), [])


class LocalVisionSelectionTests(unittest.TestCase):
    def test_only_vision_enabled_local_models_are_picked(self):
        config = _config(local_providers=[
            {"id": "txt", "endpoint": "http://127.0.0.1:11434/v1",
             "model": "llama3.2", "vision": False, "enabled": True},
            {"id": "vlm", "endpoint": "http://127.0.0.1:11434/v1",
             "model": "minicpm-v", "vision": True, "enabled": True},
        ])
        entries = agent_image.local_vision_providers(config)
        self.assertEqual([item["id"] for item in entries], ["vlm"])

    def test_local_model_without_vision_flag_is_not_picked(self):
        config = _config(local_providers=[
            {"id": "txt", "endpoint": "http://127.0.0.1:11434/v1",
             "model": "llama3.2", "enabled": True},
        ])
        self.assertEqual(agent_image.local_vision_providers(config), [])

    def test_requested_local_provider_must_match(self):
        config = _config(local_providers=[
            {"id": "vlm", "endpoint": "http://127.0.0.1:11434/v1",
             "model": "minicpm-v", "vision": True, "enabled": True},
        ])
        self.assertIsNone(agent_image.pick_local_provider(config, "other"))


class ApiVisionSelectionTests(unittest.TestCase):
    def test_only_enabled_api_models_with_explicit_vision_are_picked(self):
        config = _config(api_providers=[
            {"id": "text", "endpoint": "https://api.example/v1",
             "model": "text-model", "enabled": True},
            {"id": "disabled", "endpoint": "https://api.example/v1",
             "model": "vision-model", "vision": True, "enabled": False},
            {"id": "vision", "endpoint": "https://api.example/v1",
             "model": "vision-model", "vision": True, "enabled": True},
        ])
        entries = agent_image.api_vision_providers(config)
        self.assertEqual([item["id"] for item in entries], ["vision"])


class ActiveInvocationTests(unittest.TestCase):
    def test_missing_explicit_invocation_is_rejected_before_config_or_provider_access(self):
        output = io.StringIO()
        with mock.patch.object(agent_image, "load_config") as load, \
                mock.patch.object(agent_image, "run_web_provider_attempt") as web_call, \
                redirect_stdout(output):
            result = agent_image.main(["/not-read.png"])
        self.assertEqual(result, 2)
        self.assertIn("只能由用户当次显式调用", output.getvalue())
        load.assert_not_called()
        web_call.assert_not_called()


class ProviderOrderTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.image = self.directory / "vision.png"
        self.image.write_bytes(PNG_1PX)
        self.local = {
            "id": "vlm", "name": "Local VLM",
            "endpoint": "http://127.0.0.1:11434/v1",
            "model": "minicpm-v", "vision": True, "enabled": True,
        }
        self.web = _web("deepseek-web", images=True)
        self.api = {
            "id": "vision-api", "name": "Vision API",
            "endpoint": "https://api.example/v1", "model": "vision-model",
            "api_key": "test-key", "vision": True, "enabled": True,
        }

    @staticmethod
    def _args(path):
        return [str(path), "--no-cache", "--user-invoked"]

    def test_local_success_never_calls_beta_web(self):
        config = _config(
            local_providers=[self.local], web_providers=[self.web],
            web_assist={"enabled": True},
        )
        with mock.patch.object(agent_image, "load_config", return_value=config), \
                mock.patch.object(
                    agent_image, "run_local_provider_attempt",
                    return_value=(True, "本地描述", ""),
                ) as local_call, mock.patch.object(
                    agent_image, "run_web_provider_attempt",
                ) as web_call:
            result = agent_image.main(self._args(self.image))
        self.assertEqual(result, 0)
        local_call.assert_called_once()
        web_call.assert_not_called()

    def test_local_failure_skips_api_and_uses_beta_web_as_last_resort(self):
        config = _config(
            local_providers=[self.local], api_providers=[self.api],
            web_providers=[self.web],
            web_assist={"enabled": True},
        )
        output = "💬 AI回复:\n网页描述\n" + "=" * 60
        with mock.patch.object(agent_image, "load_config", return_value=config), \
                mock.patch.object(
                    agent_image, "run_local_provider_attempt",
                    return_value=(False, "", "offline"),
                ) as local_call, mock.patch.object(
                    agent_image, "run_api_provider_attempt",
                ) as api_call, mock.patch.object(
                    agent_image, "run_web_provider_attempt",
                    return_value=(True, output, ""),
                ) as web_call:
            result = agent_image.main(self._args(self.image))
        self.assertEqual(result, 0)
        local_call.assert_called_once()
        api_call.assert_not_called()
        web_call.assert_called_once()

    def test_api_is_used_when_no_local_visual_model_exists(self):
        config = _config(
            api_providers=[self.api], web_providers=[self.web],
            web_assist={"enabled": True},
        )
        with mock.patch.object(agent_image, "load_config", return_value=config), \
                mock.patch.object(
                    agent_image, "run_api_provider_attempt",
                    return_value=(True, "API 描述", ""),
                ) as api_call, mock.patch.object(
                    agent_image, "run_web_provider_attempt",
                ) as web_call:
            result = agent_image.main(self._args(self.image))
        self.assertEqual(result, 0)
        api_call.assert_called_once()
        web_call.assert_not_called()

    def test_api_failure_can_use_declared_beta_web_vision(self):
        config = _config(
            api_providers=[self.api], web_providers=[self.web],
            web_assist={"enabled": True},
        )
        output = "💬 AI回复:\n网页描述\n" + "=" * 60
        with mock.patch.object(agent_image, "load_config", return_value=config), \
                mock.patch.object(
                    agent_image, "run_api_provider_attempt",
                    return_value=(False, "", "offline"),
                ) as api_call, mock.patch.object(
                    agent_image, "run_web_provider_attempt",
                    return_value=(True, output, ""),
                ) as web_call:
            result = agent_image.main(self._args(self.image))
        self.assertEqual(result, 0)
        api_call.assert_called_once()
        web_call.assert_called_once()

    def test_web_only_config_does_nothing_while_beta_is_off(self):
        config = _config(web_providers=[self.web])
        output = io.StringIO()
        with mock.patch.object(agent_image, "load_config", return_value=config), \
                mock.patch.object(agent_image, "run_web_provider_attempt") as web_call, \
                redirect_stdout(output):
            result = agent_image.main(self._args(self.image))
        self.assertEqual(result, 0)
        self.assertIn("Beta 网页测试协助", output.getvalue())
        web_call.assert_not_called()


class ImageValidationTests(unittest.TestCase):
    def test_missing_file_and_bad_extension_are_reported(self):
        images, problems = agent_image.validate_images([
            "/definitely/not/here.png", str(Path(__file__)), "x.txt",
        ])
        self.assertEqual(images, [])
        self.assertEqual(len(problems), 3)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_store_and_load_roundtrip(self):
        agent_image.cache_store("abc", "deepseek", "红色圆形", home=self.home, now=1000.0)
        entry = agent_image.cache_load("abc", home=self.home, now=1000.0 + 60)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["description"], "红色圆形")
        self.assertEqual(entry["provider"], "deepseek")

    def test_expired_entry_is_removed(self):
        agent_image.cache_store("abc", "deepseek", "旧结果", home=self.home,
                                now=1000.0)
        self.assertIsNone(agent_image.cache_load(
            "abc", home=self.home, now=1000.0 + agent_image.CACHE_TTL_SECONDS + 1))

    def test_cleanup_enforces_ttl_and_max_entries(self):
        start = 1_000_000.0
        for index in range(agent_image.CACHE_MAX_ENTRIES + 5):
            agent_image.cache_store(f"id{index}", "p", f"d{index}",
                                    home=self.home, now=start + index)
        agent_image.cache_cleanup(home=self.home, now=start + 10)
        remaining = sorted(p.name for p in agent_image.cache_directory(self.home).glob("*.json"))
        self.assertEqual(len(remaining), agent_image.CACHE_MAX_ENTRIES)
        self.assertNotIn("id0.json", remaining)
        self.assertNotIn("id4.json", remaining)
        self.assertIn(f"id{agent_image.CACHE_MAX_ENTRIES + 4}.json", remaining)


class FailureClassificationTests(unittest.TestCase):
    def test_login_failure_detected(self):
        reason = agent_image._classify_relay_failure(
            "❌ 未检测到当前 Provider（deepseek）的登录状态。", False)
        self.assertIn("登录状态", reason)

    def test_timeout_detected(self):
        self.assertIn("超时", agent_image._classify_relay_failure("", True))

    def test_generic_failure_uses_last_line(self):
        reason = agent_image._classify_relay_failure("line1\nline2 风控拦截", False)
        self.assertTrue(reason)


class ExtractAnswerTests(unittest.TestCase):
    def test_answer_extracted_between_markers(self):
        output = ("header\n💬 AI回复:\n红色圆形\n"
                  + "=" * 60 + "\nAGENT_RELAY_RESULT={\"status\": \"success\"}\n")
        self.assertEqual(agent_image._extract_answer(output), "红色圆形")

    def test_missing_marker_returns_empty(self):
        self.assertEqual(agent_image._extract_answer("nothing"), "")


class LocalProviderCallTests(unittest.TestCase):
    def _image(self):
        directory = Path(tempfile.mkdtemp())
        path = directory / "t.png"
        path.write_bytes(PNG_1PX)
        return path

    def test_local_call_success(self):
        payload = {"choices": [{"message": {"content": "图1：一个圆"}}]}
        response = mock.Mock()
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__ = lambda s: response
        response.__exit__ = lambda s, *a: False
        entry = {"endpoint": "http://127.0.0.1:11434/v1", "model": "vlm",
                 "queue_timeout": 5}
        with mock.patch.object(agent_image, "urlopen", return_value=response):
            ok, answer, reason = agent_image.run_local_provider_attempt(
                entry, "描述", [self._image()])
        self.assertTrue(ok)
        self.assertEqual(answer, "图1：一个圆")
        self.assertEqual(reason, "")

    def test_local_call_failure_returns_reason(self):
        entry = {"endpoint": "http://127.0.0.1:11434/v1", "model": "vlm",
                 "queue_timeout": 5}
        with mock.patch.object(agent_image, "urlopen", side_effect=OSError("boom")):
            ok, answer, reason = agent_image.run_local_provider_attempt(
                entry, "描述", [self._image()])
        self.assertFalse(ok)
        self.assertEqual(answer, "")
        self.assertTrue(reason)


class ApiProviderCallTests(unittest.TestCase):
    def test_api_call_reuses_formatted_adapter_and_sends_image_parts(self):
        directory = Path(tempfile.mkdtemp())
        image = directory / "t.png"
        image.write_bytes(PNG_1PX)
        entry = {"endpoint": "https://api.example/v1", "model": "vision",
                 "format": "responses", "queue_timeout": 5}
        response = {"choices": [{"message": {"content": "API 视觉描述"}}]}
        with mock.patch("agent_fusion._call_formatted_model", return_value=response) as call:
            ok, answer, reason = agent_image.run_api_provider_attempt(
                entry, "描述", [image])
        self.assertTrue(ok)
        self.assertEqual(answer, "API 视觉描述")
        self.assertEqual(reason, "")
        messages = call.call_args.args[1]
        self.assertEqual(messages[0]["content"][0], {"type": "text", "text": "描述"})
        self.assertEqual(messages[0]["content"][1]["type"], "image_url")
        self.assertTrue(messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_api_empty_answer_is_a_failure(self):
        directory = Path(tempfile.mkdtemp())
        image = directory / "t.png"
        image.write_bytes(PNG_1PX)
        response = {"choices": [{"message": {"content": ""}}]}
        with mock.patch("agent_fusion._call_formatted_model", return_value=response):
            ok, answer, reason = agent_image.run_api_provider_attempt({}, "描述", [image])
        self.assertFalse(ok)
        self.assertEqual(answer, "")
        self.assertIn("未返回", reason)


class PrepareImageTests(unittest.TestCase):
    def setUp(self):
        self.has_pil = agent_image._pil_image_module() is not None

    def test_small_png_passes_through(self):
        directory = Path(tempfile.mkdtemp())
        path = directory / "small.png"
        path.write_bytes(PNG_1PX)
        send_path, compressed = agent_image.prepare_image(path)
        self.assertEqual(send_path, path)
        self.assertFalse(compressed)

    def test_oversized_image_is_compressed_when_pil_available(self):
        if not self.has_pil:
            self.skipTest("PIL 不可用")
        from PIL import Image
        directory = Path(tempfile.mkdtemp())
        path = directory / "big.png"
        Image.new("RGB", (3000, 2000), (200, 30, 30)).save(path)
        send_path, compressed = agent_image.prepare_image(path)
        self.assertTrue(compressed)
        with Image.open(send_path) as image:
            self.assertLessEqual(max(image.size), agent_image.MAX_IMAGE_DIMENSION)


class GptImageDefaultTests(unittest.TestCase):
    def test_gpt_defaults_to_no_images_without_explicit_check(self):
        conversation = config_manager.normalize_conversation(
            "gpt-web", {"modes": ["hybrid"], "supports_images": True}
        )
        self.assertFalse(conversation["images"]["hybrid"])
        self.assertFalse(conversation["supports_images"])

    def test_gpt_respects_explicit_images_map(self):
        conversation = config_manager.normalize_conversation(
            "gpt-web", {"modes": ["hybrid"], "images": {"hybrid": True}}
        )
        self.assertTrue(conversation["images"]["hybrid"])

    def test_deepseek_keeps_image_default(self):
        conversation = config_manager.normalize_conversation("deepseek-web", None)
        self.assertTrue(conversation["images"]["hybrid"])

    def test_qianwen_default_is_no_images(self):
        conversation = config_manager.normalize_conversation("qianwen-web", None)
        self.assertFalse(conversation["images"]["hybrid"])


if __name__ == "__main__":
    unittest.main()
