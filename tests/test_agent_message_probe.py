import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_message_bus as bus_module  # noqa: E402
try:
    import agent_message_probe as probe_module  # noqa: E402
except ImportError:
    probe_module = None


class MessageProbeTests(unittest.TestCase):
    def setUp(self):
        if probe_module is None:
            self.fail("agent_message_probe module is not implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="agent-message-probe-")
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.bus = bus_module.MessageBus(
            Path(self.temp.name) / "messages.sqlite3",
            now=lambda: self.now,
        )
        self.register("codex:one", name="Codex primary")
        self.register("claude:two", name="Claude reviewer")
        self.register("pi:three", name="Pi researcher")

    def register(self, address, **kwargs):
        host, session_id = address.split(":", 1)
        return self.bus.register(
            address,
            session_id=session_id,
            name=kwargs.pop("name", session_id),
            workspace=str(PROJECT_ROOT),
            **kwargs,
        )

    def test_lists_only_live_registered_sessions_without_private_endpoint(self):
        self.register(
            "opencode:four",
            name="OpenCode worker",
            adapter="opencode_http",
            endpoint={"base_url": "http://127.0.0.1:4096", "token": "secret"},
        )
        self.register("hermes:offline", state="offline")
        self.register("hermes:stale")
        self.now += bus_module.STALE_AFTER_SECONDS + 1
        self.bus.heartbeat("codex:one")
        self.bus.heartbeat("claude:two")
        self.bus.heartbeat("pi:three")

        peers = probe_module.list_peers(self.bus)

        self.assertEqual(
            {peer["address"] for peer in peers},
            {"codex:one", "claude:two", "pi:three"},
        )
        self.assertNotIn("endpoint", peers[0])
        self.assertNotIn("secret", str(peers))

    def test_probe_fans_out_and_only_correlated_replies_are_connected(self):
        result = probe_module.start_probe(
            self.bus, "codex:one", ["claude:two", "pi:three"]
        )

        self.assertEqual(len(result["probes"]), 2)
        self.assertTrue(all(item["status"] == "queued" for item in result["probes"]))
        self.assertTrue(all(not item["connected"] for item in result["probes"]))
        self.assertEqual(len({item["task_id"] for item in result["probes"]}), 1)

        first = result["probes"][0]
        second = result["probes"][1]
        self.bus.inbox(first["to"])
        delivered = probe_module.probe_status(
            self.bus, [first["message_id"], second["message_id"]]
        )
        self.assertEqual(delivered["probes"][0]["status"], "delivered")
        self.assertFalse(delivered["probes"][0]["connected"])

        self.bus.send(
            sender=first["to"],
            reply_to=first["message_id"],
            body="PROBE_OK",
            push=False,
        )
        status = probe_module.probe_status(
            self.bus, [first["message_id"], second["message_id"]]
        )
        self.assertTrue(status["probes"][0]["connected"])
        self.assertEqual(status["probes"][0]["status"], "replied")
        self.assertFalse(status["probes"][1]["connected"])
        self.assertEqual(status["summary"]["connected"], 1)
        self.assertEqual(status["summary"]["pending"], 1)

    def test_probe_rejects_invalid_selection_before_sending(self):
        self.register("hermes:offline", state="offline")
        cases = (
            ("codex:one", [], "between 1 and 8"),
            ("codex:one", ["claude:two"] * 2, "Duplicate"),
            ("codex:one", ["codex:one"], "itself"),
            ("codex:one", ["hermes:offline"], "not live"),
            ("codex:one", ["missing:four"], "not live"),
            ("missing:sender", ["claude:two"], "not live"),
            ("codex:one", [f"pi:target-{index}" for index in range(9)], "between 1 and 8"),
        )
        for sender, targets, message in cases:
            with self.subTest(message=message, targets=targets):
                with self.assertRaisesRegex(bus_module.MessageBusError, message):
                    probe_module.start_probe(self.bus, sender, targets)
        self.assertEqual(self.bus.inbox("claude:two", peek=True), [])

    def test_probe_rejects_stale_participants(self):
        self.now += bus_module.STALE_AFTER_SECONDS + 1
        with self.assertRaisesRegex(bus_module.MessageBusError, "not live"):
            probe_module.start_probe(self.bus, "codex:one", ["claude:two"])

    def test_status_rejects_unknown_and_malformed_message_ids(self):
        for message_ids in ([], ["msg_missing"], ["not-a-message"], "msg_one"):
            with self.subTest(message_ids=message_ids):
                with self.assertRaises(bus_module.MessageBusError):
                    probe_module.probe_status(self.bus, message_ids)


class MessageProbeHttpTests(unittest.TestCase):
    def test_config_server_lists_peers_and_returns_correlated_reply_status(self):
        import install

        with tempfile.TemporaryDirectory(prefix="agent-message-probe-http-") as directory:
            home = Path(directory)
            database = home / "messages.sqlite3"
            bus = bus_module.MessageBus(database)
            for address in ("codex:http-one", "claude:http-two", "pi:http-three"):
                host, session_id = address.split(":", 1)
                bus.register(
                    address,
                    session_id=session_id,
                    name=address,
                    workspace=str(PROJECT_ROOT),
                )

            opened = threading.Event()
            location = []
            failures = []

            def run_server():
                try:
                    install.run_web_configurator()
                except Exception as exc:  # pragma: no cover - surfaced by assertion
                    failures.append(exc)

            def capture(url):
                location.append(url)
                opened.set()

            base_url = ""
            thread = threading.Thread(target=run_server, daemon=True)
            with patch.object(install, "CODEX_HOME", home), \
                    patch.object(install.webbrowser, "open", side_effect=capture), \
                    patch.dict("os.environ", {
                        "CODEX_HOME": str(home),
                        "AGENTRELAY_MESSAGE_DB": str(database),
                    }):
                thread.start()
                self.assertTrue(opened.wait(5), "config server did not start")
                base_url = location[0]
                try:
                    with urlopen(base_url + "/api/messages/peers", timeout=5) as response:
                        peers = json.load(response)["agents"]
                    self.assertEqual(len(peers), 3)
                    self.assertNotIn("endpoint", peers[0])

                    request = Request(
                        base_url + "/api/messages/probe",
                        data=json.dumps({
                            "sender": "codex:http-one",
                            "targets": ["claude:http-two", "pi:http-three"],
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(request, timeout=5) as response:
                        started = json.load(response)
                    self.assertEqual(started["summary"]["total"], 2)
                    self.assertEqual(started["summary"]["connected"], 0)

                    first = started["probes"][0]
                    bus.inbox(first["to"])
                    bus.send(
                        sender=first["to"],
                        reply_to=first["message_id"],
                        body="PROBE_OK",
                        push=False,
                    )
                    ids = ",".join(item["message_id"] for item in started["probes"])
                    with urlopen(base_url + "/api/messages/probe?ids=" + ids, timeout=5) as response:
                        status = json.load(response)
                    self.assertEqual(status["summary"]["connected"], 1)
                    self.assertEqual(status["summary"]["pending"], 1)
                    self.assertFalse(status["probes"][1]["connected"])
                finally:
                    close = Request(base_url + "/api/close", data=b"{}", method="POST")
                    urlopen(close, timeout=5).close()
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
