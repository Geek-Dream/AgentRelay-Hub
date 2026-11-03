import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import agent_fusion  # noqa: E402
import config_manager  # noqa: E402
import config_web  # noqa: E402
import fusion_mode  # noqa: E402
import model_router  # noqa: E402
import provider_leases  # noqa: E402
import sensitive_redaction  # noqa: E402
import web_session_pool  # noqa: E402
from agent_relay_runtime import SessionRef  # noqa: E402


class SchedulerTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="agentrelay-scheduler-test-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.lease_dir = self.temp_dir / "leases"

    def manager(self, provider_id="api:test", **overrides):
        values = {
            "kind": "api",
            "limit": 1,
            "queue_timeout": 1,
            "lease_ttl": 5,
            "directory": self.lease_dir,
            "poll_interval": 0.01,
        }
        values.update(overrides)
        return provider_leases.ProviderLeaseManager(provider_id, **values)


class ProviderLimitDefaultsTests(SchedulerTestBase):
    def test_defaults_and_normalization_are_bounded(self):
        self.assertEqual(
            config_manager.provider_concurrency_defaults("web"),
            {"max_concurrency": 1, "queue_timeout": 30, "lease_ttl": 90},
        )
        self.assertEqual(
            config_manager.provider_concurrency_defaults("local"),
            {"max_concurrency": 1, "queue_timeout": 30, "lease_ttl": 90},
        )
        self.assertEqual(
            config_manager.provider_concurrency_defaults("api"),
            {"max_concurrency": 10, "queue_timeout": 30, "lease_ttl": 90},
        )
        normalized = config_manager.normalize_provider_limits({
            "max_concurrency": 999,
            "queue_timeout": 0,
            "lease_ttl": 1,
        }, "local")
        self.assertEqual(normalized["max_concurrency"], 100)
        self.assertEqual(normalized["queue_timeout"], 1)
        self.assertEqual(normalized["lease_ttl"], 10)

    def test_loading_old_provider_config_adds_defaults(self):
        config_manager.save_config({
            "web_providers": [{
                "id": "deepseek-web",
                "base_url": "https://chat.deepseek.com/",
            }],
            "local_providers": [{
                "id": "local-llm",
                "endpoint": "http://127.0.0.1:8080/v1",
                "model": "local-model",
            }],
            "api_providers": [{
                "id": "kimi-api",
                "endpoint": "https://api.example/v1",
                "model": "kimi",
            }],
        }, self.temp_dir)
        loaded = config_manager.load_config(self.temp_dir)
        for kind, field in (
            ("web", "web_providers"),
            ("local", "local_providers"),
            ("api", "api_providers"),
        ):
            item = loaded[field][0]
            defaults = config_manager.provider_concurrency_defaults(kind)
            self.assertEqual(item["max_concurrency"], defaults["max_concurrency"])
            self.assertEqual(item["queue_timeout"], 30)
            self.assertEqual(item["lease_ttl"], 90)

    def test_legacy_deepseek_migration_gets_pool_defaults(self):
        target = config_manager.unified_deepseek_state_path(self.temp_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"encrypted-placeholder")

        config, changed = config_manager.migrate_legacy_deepseek_config(self.temp_dir)

        self.assertTrue(changed)
        provider = config["web_providers"][0]
        self.assertEqual(provider["id"], "deepseek-web")
        self.assertEqual(provider["max_concurrency"], 1)
        self.assertEqual(provider["queue_timeout"], 30)
        self.assertEqual(provider["lease_ttl"], 90)


class ProviderLeaseTests(SchedulerTestBase):
    def test_fifo_waiters_are_granted_in_arrival_order(self):
        manager = self.manager(queue_timeout=2)
        held = manager.acquire()
        order = []
        failures = []
        first_acquired = threading.Event()
        second_acquired = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()

        def waiter(name, acquired, release):
            try:
                lease = manager.acquire(wait_timeout=2)
                order.append(name)
                acquired.set()
                release.wait(2)
                manager.release(lease)
            except Exception as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        first = threading.Thread(
            target=waiter,
            args=("first", first_acquired, release_first),
        )
        second = threading.Thread(
            target=waiter,
            args=("second", second_acquired, release_second),
        )
        first.start()
        self._wait_for(lambda: manager.status()["queued"] == 1)
        second.start()
        self._wait_for(lambda: manager.status()["queued"] == 2)

        manager.release(held)
        self.assertTrue(first_acquired.wait(2))
        self.assertEqual(order, ["first"])
        self.assertFalse(second_acquired.wait(0.08))

        release_first.set()
        self.assertTrue(second_acquired.wait(2))
        self.assertEqual(order, ["first", "second"])
        release_second.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])

    def test_queue_timeout_is_explicit(self):
        manager = self.manager(queue_timeout=0.05)
        held = manager.acquire()
        try:
            with self.assertRaises(provider_leases.ProviderLeaseTimeout) as raised:
                manager.acquire()
        finally:
            manager.release(held)
        self.assertEqual(raised.exception.provider_id, "api:test")
        self.assertGreaterEqual(raised.exception.status["queued"], 1)
        self.assertEqual(manager.status()["queued"], 0)

    def test_dead_pid_and_stale_heartbeat_are_recovered(self):
        manager = self.manager(lease_ttl=5)
        state = manager._empty_state()
        state["active"] = {
            "dead": {
                "slot": 1,
                "pid": 99999999,
                "created_at": provider_leases._now(),
                "heartbeat_at": provider_leases._now(),
            },
        }
        state["queue"] = [{
            "lease_id": "crashed-waiter",
            "ticket": 1,
            "pid": 99999999,
            "created_at": provider_leases._now(),
        }]
        provider_leases._atomic_write(manager.path, state)
        self.assertEqual(manager.status()["active"], 0)
        self.assertEqual(manager.status()["queued"], 0)

        state = manager._load()
        state["active"] = {
            "stale": {
                "slot": 1,
                "pid": os.getpid(),
                "created_at": provider_leases._now(),
                "heartbeat_at": "2020-01-01T00:00:00+00:00",
            },
        }
        provider_leases._atomic_write(manager.path, state)
        self.assertEqual(manager.status()["active"], 0)

    def test_concurrent_slots_are_numbered(self):
        manager = self.manager(limit=2)
        with manager.lease() as first, manager.lease() as second:
            self.assertEqual({first.slot, second.slot}, {1, 2})
            self.assertEqual(manager.status()["active_slots"], [1, 2])

    def _wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not met before timeout")


class WebSessionPoolTests(SchedulerTestBase):
    def pool(self, business="default", limit=2):
        return web_session_pool.WebSessionPool(
            "deepseek-web",
            limit=limit,
            queue_timeout=0.1,
            lease_ttl=5,
            home=self.temp_dir,
            business=business,
        )

    def test_numbered_slots_are_shared_globally(self):
        first_pool = self.pool()
        second_pool = self.pool()
        with first_pool.claim() as first, second_pool.claim() as second:
            self.assertEqual({first.slot, second.slot}, {1, 2})
            self.assertEqual(first.title, "AgentRelay-DeepSeek-1")
            self.assertEqual(second.title, "AgentRelay-DeepSeek-2")

    def test_global_limit_rejects_extra_session(self):
        first_pool = self.pool(limit=1)
        second_pool = self.pool(limit=1)
        with first_pool.claim():
            with self.assertRaises(provider_leases.ProviderLeaseTimeout):
                with second_pool.claim():
                    pass

    def test_business_owner_and_binding_scope_are_isolated(self):
        alpha = self.pool("alpha")
        beta = self.pool("beta")
        with alpha.claim() as slot:
            alpha.set_slot_owner(
                slot.slot,
                session_id="alpha-session",
                href="https://chat.deepseek.com/a/chat/s/alpha-session",
            )
            alpha_scope = slot.binding_scope
        with beta.claim() as slot:
            beta_scope = slot.binding_scope
            owner = beta.slot_owner_record(slot.slot)
        self.assertEqual(owner["business"], "alpha")
        self.assertEqual(owner["session_id"], "alpha-session")
        self.assertNotEqual(alpha_scope, beta_scope)
        self.assertIn(":alpha:", alpha_scope)
        self.assertIn(":beta:", beta_scope)

    def test_legacy_session_is_adopted_only_once(self):
        alpha = self.pool("alpha")
        beta = self.pool("beta")
        self.assertTrue(alpha.consume_legacy_adoption())
        self.assertFalse(beta.consume_legacy_adoption())

    def test_session_lookup_uses_exact_session_id_when_available(self):
        sessions = [
            SessionRef(
                provider="deepseek",
                session_id="old",
                title="AgentRelay-DeepSeek-1",
                href="https://chat.deepseek.com/a/chat/s/old",
            ),
            SessionRef(
                provider="deepseek",
                session_id="wanted",
                title="Renamed conversation",
                href="https://chat.deepseek.com/a/chat/s/wanted",
            ),
        ]
        found = web_session_pool.find_pool_session(
            sessions,
            "AgentRelay-DeepSeek-1",
            session_id="wanted",
        )
        self.assertEqual(found.session_id, "wanted")
        self.assertIsNone(web_session_pool.find_pool_session(
            sessions,
            "AgentRelay-DeepSeek-1",
            session_id="missing",
        ))


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.providers = {
            "web:deepseek-web": {
                "kind": "web",
                "enabled": True,
                "entry": {"conversation": {"images": {"expert": True}}},
            },
            "local:local-llm": {
                "kind": "local",
                "enabled": True,
                "entry": {"vision": True},
            },
            "api:kimi-api": {
                "kind": "api",
                "enabled": True,
                "entry": {},
            },
        }

    def test_local_execution_routes_api_before_local(self):
        route = model_router.route_provider_task(
            self.providers,
            preferred="web:deepseek-web",
            needs_local_execution=True,
        )
        self.assertEqual(route.provider_id, "api:kimi-api")
        self.assertTrue(route.executable)
        self.assertEqual(route.role, "executor")

    def test_local_execution_uses_local_when_api_is_absent(self):
        providers = dict(self.providers)
        providers.pop("api:kimi-api")
        route = model_router.route_provider_task(
            providers,
            needs_local_execution=True,
        )
        self.assertEqual(route.provider_id, "local:local-llm")
        self.assertIn("images", route.capabilities)

    def test_web_never_executes_local_tasks(self):
        providers = {"web:deepseek-web": self.providers["web:deepseek-web"]}
        self.assertIsNone(model_router.route_provider_task(
            providers,
            needs_local_execution=True,
        ))
        advisory = model_router.route_provider_task(providers)
        self.assertFalse(advisory.executable)
        self.assertTrue(advisory.advisory)
        self.assertIn("local_read_write", advisory.capability_loss)

    def test_fusion_web_call_does_not_take_a_second_lease(self):
        provider = {
            "id": "deepseek-web",
            "ref": "web:deepseek-web",
            "kind": "web",
            "entry": {},
        }
        response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        with mock.patch.object(
            config_manager,
            "apply_config_to_environment",
        ), mock.patch.object(
            agent_fusion,
            "_call_web_provider",
            return_value=response,
        ), mock.patch.object(
            fusion_mode,
            "provider_lease_manager",
        ) as lease_manager:
            result = agent_fusion._call_provider_model(
                provider,
                [{"role": "user", "content": "hello"}],
                [],
                5,
                Path("/tmp/test-home"),
                business="alpha",
            )
        self.assertEqual(result, response)
        lease_manager.assert_not_called()


class SensitiveRedactionTests(unittest.TestCase):
    def test_redaction_round_trip_keeps_mapping_local(self):
        text = (
            "联系 user@example.com 或 13812345678；"
            "api_key=secret-value；Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        )
        result = sensitive_redaction.redact_text(text)
        self.assertNotIn("user@example.com", result.text)
        self.assertNotIn("13812345678", result.text)
        self.assertNotIn("secret-value", result.text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result.text)
        self.assertEqual(result.restore(result.text), text)
        self.assertTrue(result.audit()["mapping_retained_locally"])

    def test_complete_private_key_is_redacted(self):
        key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "very-secret-material\n"
            "-----END PRIVATE KEY-----"
        )
        result = sensitive_redaction.redact_text(f"before\n{key}\nafter")
        self.assertIn("private_key", result.categories)
        self.assertNotIn("very-secret-material", result.text)
        self.assertEqual(result.restore(result.text), f"before\n{key}\nafter")

    def test_known_key_inside_assignment_is_restored_without_nested_fakes(self):
        key = "sk-" + ("a" * 24)
        text = f"api_key={key}"
        result = sensitive_redaction.redact_text(text)
        self.assertNotIn(key, result.text)
        self.assertEqual(result.restore(result.text), text)
        self.assertNotIn("AGENTRELAY_FAKE", result.restore(result.text))

    def test_incomplete_private_key_fails_closed(self):
        with self.assertRaises(sensitive_redaction.SensitiveDataError):
            sensitive_redaction.redact_text(
                "-----BEGIN PRIVATE KEY-----\nnot-finished"
            )


class SchedulerConfigSurfaceTests(unittest.TestCase):
    def test_all_provider_forms_include_three_limit_fields(self):
        page = config_web.build_config_page({})
        for kind in ("web", "local", "api"):
            self.assertIn(f"limitFields('{kind}',p)", page)
        self.assertIn('id="f-max-concurrency"', page)
        self.assertIn('id="f-queue-timeout"', page)
        self.assertIn('id="f-lease-ttl"', page)
        self.assertIn("不会静默改成网页执行", page)
        self.assertIn("AgentRelay-DeepSeek-1、-2、-3", page)
        self.assertIn("Hook 非常驻；可选 daemon", page)

    def test_installer_includes_new_scheduler_modules(self):
        spec = importlib.util.spec_from_file_location(
            "provider_scheduler_installer",
            PROJECT_ROOT / "install.py",
        )
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for filename in (
            "provider_leases.py",
            "web_session_pool.py",
            "sensitive_redaction.py",
        ):
            self.assertIn(filename, installer.SCRIPT_FILES)


if __name__ == "__main__":
    unittest.main()
