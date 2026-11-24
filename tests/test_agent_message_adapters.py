import http.server
import json
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import agent_message_adapters as adapters  # noqa: E402
import agent_message_bus as bus_module  # noqa: E402


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-message-adapters-")
        self.addCleanup(self.temp.cleanup)
        self.bus = bus_module.MessageBus(Path(self.temp.name) / "messages.sqlite3")
        self.bus.register("codex:sender", session_id="sender", name="sender")

    def message(self):
        return {
            "message_id": "msg-test",
            "task_id": "msg-test",
            "from": "codex:sender",
            "to": "claude:receiver",
            "kind": "message",
            "delivery": "safe_boundary",
            "context_mode": "current",
            "body": "hello from codex",
            "reply_to": "",
            "envelope": bus_module.render_envelope({
                "message_id": "msg-test",
                "task_id": "msg-test",
                "from": "codex:sender",
                "to": "claude:receiver",
                "kind": "message",
                "delivery": "safe_boundary",
                "context_mode": "current",
                "body": "hello from codex",
                "reply_to": "",
            }),
        }

    def test_claude_socket_uses_official_auth_and_user_records(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("Unix sockets unavailable")
        path = Path(self.temp.name) / "claude.sock"
        received = []
        ready = threading.Event()

        def serve():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                ready.set()
                connection, _ = server.accept()
                with connection:
                    data = b""
                    while data.count(b"\n") < 2:
                        data += connection.recv(65536)
                    received.extend(json.loads(line) for line in data.splitlines())

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))
        receipt = adapters.deliver(
            {"adapter": "claude_socket", "endpoint": {"socket_path": str(path), "token": "secret"}},
            self.message(),
        )
        thread.join(2)
        self.assertEqual(receipt["status"], "dispatched")
        self.assertEqual(received[0], {"type": "auth", "token": "secret"})
        self.assertEqual(received[1]["type"], "user")
        content = received[1]["message"]["content"]
        self.assertIn("hello from codex", content)
        self.assertIn("not_user_authorization", content)

    def test_opencode_prompt_uses_native_delivery_and_envelope(self):
        captured = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                captured.append((self.path, json.loads(self.rfile.read(length))))
                payload = json.dumps({"data": {"id": "msg-host", "sessionID": "ses-test"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        class Server(socketserver.TCPServer):
            allow_reuse_address = True

        with Server(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                message = self.message()
                message["to"] = "opencode:receiver"
                message["delivery"] = "when_idle"
                receipt = adapters.deliver(
                    {
                        "adapter": "opencode_http",
                        "endpoint": {
                            "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                            "session_id": "ses-test",
                        },
                    },
                    message,
                )
            finally:
                server.shutdown()
                thread.join(2)

        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(captured[0][0], "/api/session/ses-test/prompt")
        self.assertEqual(captured[0][1]["id"], "msg-test")
        self.assertEqual(captured[0][1]["prompt"]["text"], message["envelope"])
        self.assertEqual(captured[0][1]["delivery"], "queue")
        self.assertTrue(captured[0][1]["resume"])
        self.assertNotIn("text", captured[0][1])
        self.assertNotIn("metadata", captured[0][1])

    def test_pull_adapter_stays_queued_instead_of_claiming_delivery(self):
        self.bus.register("pi:receiver", session_id="receiver", adapter="pull")
        message = self.bus.send(
            "codex:sender",
            "pi:receiver",
            "queued for pi",
            review_action="new",
            review_reason="Verify queued adapter delivery semantics",
            push=True,
        )
        self.assertEqual(message["status"], "queued")
        self.assertEqual(message["transport"]["status"], "queued")

    def test_opencode_empty_success_is_not_a_delivery_receipt(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                message = self.message()
                message["to"] = "opencode:receiver"
                with self.assertRaisesRegex(adapters.DeliveryError, "receipt"):
                    adapters.deliver(
                        {
                            "adapter": "opencode_http",
                            "endpoint": {
                                "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                                "session_id": "ses-test",
                            },
                        },
                        message,
                    )
            finally:
                server.shutdown()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
