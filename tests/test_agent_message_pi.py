import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import agent_message_bus as bus_module  # noqa: E402
import agent_targets as targets  # noqa: E402


@unittest.skipUnless(shutil.which("node"), "Node.js is required for Pi extension tests")
class PiMessageExtensionTests(unittest.TestCase):
    def test_installed_main_extension_loads_polling_and_reply_tools(self):
        with tempfile.TemporaryDirectory(prefix="agent-message-pi-") as temporary:
            root = Path(temporary)
            extension = root / "extensions" / "agent-relay.ts"
            targets.write_pi_extension(
                extension,
                Path(sys.executable),
                PROJECT_ROOT / "scripts" / "agentrelay_hook_shim.py",
            )
            database = root / "messages.sqlite3"
            bus = bus_module.MessageBus(database)
            bus.register("codex:sender", session_id="sender", name="sender")
            bus.register("pi:installed-pi", session_id="installed-pi", name="pi")
            original = bus.send(
                "codex:sender",
                "pi:installed-pi",
                "PING-INSTALLED-PI",
                review_action="new",
                review_reason="Verify the installed Pi messaging extension",
                push=False,
            )

            typebox = root / "node_modules" / "typebox"
            typebox.mkdir(parents=True)
            (typebox / "package.json").write_text(
                json.dumps({"name": "typebox", "type": "module", "main": "index.js"}),
                encoding="utf-8",
            )
            (typebox / "index.js").write_text(
                "const schema = () => ({});\n"
                "export const Type = {\n"
                "  Object: schema, String: schema, Boolean: schema, Number: schema,\n"
                "  Optional: schema, Union: schema, Literal: schema,\n"
                "};\n",
                encoding="utf-8",
            )
            driver = root / "driver.mjs"
            driver.write_text(
                "import agentRelay from './extensions/agent-relay.ts';\n"
                "const handlers = new Map();\n"
                "const tools = new Map();\n"
                "const sent = [];\n"
                "const pi = {\n"
                "  registerTool(tool) { tools.set(tool.name, tool); },\n"
                "  registerCommand() {},\n"
                "  on(name, handler) {\n"
                "    const items = handlers.get(name) || [];\n"
                "    items.push(handler); handlers.set(name, items);\n"
                "  },\n"
                "  getActiveTools() { return []; }, setActiveTools() {},\n"
                "  sendMessage(message, options) { sent.push({ message, options }); },\n"
                "};\n"
                "const ctx = {\n"
                "  cwd: process.cwd(), isIdle() { return true; },\n"
                "  sessionManager: { getSessionId() { return 'installed-pi'; } },\n"
                "};\n"
                "async function emit(name, event = {}) {\n"
                "  for (const handler of handlers.get(name) || []) await handler(event, ctx);\n"
                "}\n"
                "await agentRelay(pi);\n"
                "await emit('session_start');\n"
                "for (let index = 0; index < 50 && sent.length === 0; index += 1) {\n"
                "  await new Promise((resolve) => setTimeout(resolve, 100));\n"
                "}\n"
                "if (sent.length !== 1) throw new Error('installed Pi extension did not poll');\n"
                "const listed = await tools.get('agentrelay_list_agents').execute('list', {});\n"
                "const history = await tools.get('agentrelay_task_history').execute('history', {});\n"
                "const reply = await tools.get('agentrelay_send_message').execute(\n"
                "  'reply',\n"
                "  { reply_to: " + json.dumps(original["message_id"]) + ", body: 'PONG-INSTALLED-PI' },\n"
                "  undefined, undefined, ctx,\n"
                ");\n"
                "await emit('session_shutdown');\n"
                "console.log(JSON.stringify({\n"
                "  sent, listed, history, reply,\n"
                "  hasAckTool: tools.has('agentrelay_ack_message'),\n"
                "}));\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": str(root),
                "CODEX_HOME": str(root / ".codex"),
                "AGENTRELAY_MESSAGE_DB": str(database),
                "AGENTRELAY_MESSAGE_BUS": str(PROJECT_ROOT / "scripts" / "agent_message_bus.py"),
                "AGENTRELAY_PYTHON": sys.executable,
            }
            completed = subprocess.run(
                [shutil.which("node"), str(driver)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertIn("PING-INSTALLED-PI", evidence["sent"][0]["message"]["content"])
            self.assertEqual(evidence["sent"][0]["options"]["deliverAs"], "followUp")
            self.assertTrue(evidence["sent"][0]["options"]["triggerTurn"])
            self.assertTrue(evidence["hasAckTool"])
            self.assertIn("[]", evidence["history"]["content"][0]["text"])
            self.assertIn("codex:sender", evidence["listed"]["content"][0]["text"])
            self.assertEqual(bus.get(original["message_id"])["status"], "replied")
            replies = bus.inbox("codex:sender")
            self.assertEqual([item["body"] for item in replies], ["PONG-INSTALLED-PI"])
            self.assertEqual(replies[0]["task_id"], original["task_id"])


if __name__ == "__main__":
    unittest.main()
