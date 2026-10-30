import tempfile
import unittest
from pathlib import Path

from orchestrator.core import Orchestrator
from orchestrator.lark_bridge import LarkRelayService
from orchestrator.relay import PROTOCOL, RelayBridge, encode_relay_message
from orchestrator.workers import MockWorker


class FakeCLI:
    def __init__(self):
        self.replies = []

    def reply(self, message_id, text, idempotency_key):
        self.replies.append((message_id, text, idempotency_key))
        return {"ok": True}


class LarkRelayServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        root = Path(self.tempdir.name)
        controller = Orchestrator(root / "tasks.db", root / "artifacts")
        bridge = RelayBridge("cloud-desktop-1", controller, {"worker_a": MockWorker()})
        self.cli = FakeCLI()
        self.service = LarkRelayService(
            bridge, self.cli, {"oc-allowed"}, {"ou-controller"}
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def event(self, **overrides):
        event = {
            "event_id": "evt-lark-001",
            "message_id": "om-task-001",
            "chat_id": "oc-allowed",
            "sender_id": "ou-controller",
            "message_type": "text",
            "content": encode_relay_message({
                "protocol": PROTOCOL,
                "kind": "task",
                "task_id": "TASK-lark-001",
                "idempotency_key": "lark-smoke-v1",
                "target_bridge": "cloud-desktop-1",
                "assigned_worker": "worker_a",
                "task_type": "data_audit",
                "role": "dataset_auditor",
                "input_ref": "controlled-reference",
                "instructions": "Run the mock smoke test",
            }),
        }
        event.update(overrides)
        return event

    def test_allowed_task_is_executed_and_replied(self):
        response = self.service.process_event(self.event())
        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(len(self.cli.replies), 1)
        message_id, text, _ = self.cli.replies[0]
        self.assertEqual(message_id, "om-task-001")
        self.assertEqual(response["task_id"], "TASK-lark-001")
        self.assertIn('"kind":"result"', text)

    def test_wrong_chat_or_sender_is_not_executed(self):
        self.assertIsNone(self.service.process_event(self.event(chat_id="oc-other")))
        self.assertIsNone(self.service.process_event(self.event(sender_id="ou-other")))
        self.assertEqual(self.cli.replies, [])

    def test_non_text_message_is_ignored(self):
        self.assertIsNone(self.service.process_event(self.event(message_type="image")))
        self.assertEqual(self.cli.replies, [])

    def test_repeated_event_uses_same_reply_idempotency_key(self):
        self.service.process_event(self.event())
        self.service.process_event(self.event())
        self.assertEqual(len(self.cli.replies), 2)
        self.assertEqual(self.cli.replies[0][2], self.cli.replies[1][2])
        self.assertLessEqual(len(self.cli.replies[0][2]), 50)


if __name__ == "__main__":
    unittest.main()
