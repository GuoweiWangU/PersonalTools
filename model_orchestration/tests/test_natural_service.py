import tempfile
import unittest
from pathlib import Path

from orchestrator.core import Orchestrator
from orchestrator.lark_bridge import LarkRelayService
from orchestrator.relay import RelayBridge
from orchestrator.workers import MockWorker


class FakeCLI:
    def __init__(self):
        self.replies = []

    def reply(self, message_id, text, idempotency_key):
        self.replies.append((message_id, text, idempotency_key))
        return {"ok": True}


class NaturalServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        root = Path(self.tempdir.name)
        controller = Orchestrator(root / "tasks.db", root / "artifacts")
        bridge = RelayBridge("cloud-desktop-1", controller, {"mock": MockWorker()})
        self.cli = FakeCLI()
        self.service = LarkRelayService(bridge, self.cli, {"oc-1"}, {"ou-1"})

    def tearDown(self):
        self.tempdir.cleanup()

    def event(self, content):
        return {
            "event_id": "evt-natural-1", "message_id": "om-natural-1",
            "chat_id": "oc-1", "sender_id": "ou-1",
            "message_type": "text", "content": content,
        }

    def test_mock_command_executes_and_returns_readable_text(self):
        response = self.service.process_event(self.event("@机器人 /mock 测试自然语言"))
        self.assertEqual(response["status"], "succeeded")
        reply = self.cli.replies[0][1]
        self.assertIn("执行成功", reply)
        self.assertIn(response["task_id"], reply)
        self.assertNotIn('"protocol"', reply)

    def test_help_does_not_execute_worker(self):
        response = self.service.process_event(self.event("/help"))
        self.assertEqual(response["kind"], "help")
        self.assertIn("/glm", self.cli.replies[0][1])


if __name__ == "__main__":
    unittest.main()
