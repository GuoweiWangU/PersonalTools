import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.core import Orchestrator
from orchestrator.relay import PROTOCOL, RelayBridge, encode_relay_message
from orchestrator.workers import MockWorker


class RelayBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        root = Path(self.tempdir.name)
        self.controller = Orchestrator(root / "tasks.db", root / "artifacts")
        self.bridge = RelayBridge(
            "cloud-desktop-1", self.controller,
            {"worker_a": MockWorker(), "worker_b": MockWorker()},
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def message(self, **overrides):
        envelope = {
            "protocol": PROTOCOL,
            "kind": "task",
            "task_id": "TASK-relay-001",
            "idempotency_key": "audit-bus-i-v1",
            "target_bridge": "cloud-desktop-1",
            "assigned_worker": "worker_a",
            "task_type": "data_audit",
            "role": "dataset_auditor",
            "input_ref": "controlled-reference",
            "instructions": "Validate the manifest and report blockers",
        }
        envelope.update(overrides)
        return encode_relay_message(envelope)

    def test_task_message_routes_to_result_message(self):
        response = self.bridge.handle_message(
            self.message(), event_id="evt-001", reply_to_message_id="om-001"
        )
        self.assertEqual(response["kind"], "result")
        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(response["task_id"], "TASK-relay-001")
        self.assertEqual(response["bridge_id"], "cloud-desktop-1")
        self.assertEqual(response["worker_id"], "worker_a")
        self.assertEqual(response["reply_to_message_id"], "om-001")
        self.assertEqual(self.controller.get_task("TASK-relay-001")["attempts"], 1)

    def test_other_bridge_and_unrelated_messages_are_ignored(self):
        self.assertIsNone(self.bridge.handle_message(self.message(target_bridge="cloud-desktop-2")))
        self.assertIsNone(self.bridge.handle_message("plain group chat"))
        self.assertIsNone(self.bridge.handle_message(json.dumps({
            "protocol": PROTOCOL, "kind": "result", "target_bridge": "cloud-desktop-1"
        })))

    def test_duplicate_event_does_not_execute_twice(self):
        first = self.bridge.handle_message(self.message(), event_id="evt-001")
        second = self.bridge.handle_message(self.message(), event_id="evt-001")
        self.assertEqual(first, second)
        self.assertEqual(self.controller.get_task("TASK-relay-001")["attempts"], 1)

    def test_duplicate_task_with_new_event_does_not_execute_twice(self):
        first = self.bridge.handle_message(self.message(), event_id="evt-001")
        second = self.bridge.handle_message(self.message(), event_id="evt-002")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(self.controller.get_task("TASK-relay-001")["attempts"], 1)

    def test_unknown_worker_returns_structured_failure(self):
        response = self.bridge.handle_message(self.message(assigned_worker="missing"))
        self.assertEqual(response["status"], "failed")
        self.assertIn("Worker is not registered", response["error"])
        self.assertEqual(response["blocking_issues"], [response["error"]])

    def test_malformed_targeted_task_returns_structured_failure(self):
        response = self.bridge.handle_message(self.message(instructions=""))
        self.assertEqual(response["status"], "failed")
        self.assertIn("instructions", response["error"])

    def test_workers_receive_only_their_own_payloads(self):
        class RecordingWorker(MockWorker):
            def __init__(self):
                self.payloads = []

            def run(self, payload):
                self.payloads.append(payload)
                return super().run(payload)

        worker_a = RecordingWorker()
        worker_b = RecordingWorker()
        bridge = RelayBridge(
            "cloud-desktop-1", self.controller, {"worker_a": worker_a, "worker_b": worker_b}
        )
        bridge.handle_message(self.message(task_id="TASK-a", idempotency_key="key-a"))
        bridge.handle_message(self.message(
            task_id="TASK-b", idempotency_key="key-b", assigned_worker="worker_b",
            input_ref="worker-b-private-reference",
        ))
        self.assertEqual(len(worker_a.payloads), 1)
        self.assertEqual(len(worker_b.payloads), 1)
        self.assertNotIn("worker-b-private-reference", json.dumps(worker_a.payloads))


if __name__ == "__main__":
    unittest.main()
