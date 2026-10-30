import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.agent_store import ControlStore
from orchestrator.core import Orchestrator
from orchestrator.lark_bridge import LarkRelayService
from orchestrator.relay import RelayBridge
from orchestrator.runtime import AgentRuntime
from orchestrator.workers import MockWorker


class FakeCLI:
    def __init__(self):
        self.replies = []

    def reply(self, message_id, text, idempotency_key):
        self.replies.append((message_id, text, idempotency_key))
        return {"ok": True}


class FakeAgent:
    def run_turn(self, task, turn, roots, ssh_hosts, approval_handler, log_handler):
        return {"status": "succeeded", "summary": turn["instructions"],
                "artifacts": [], "unknowns": [], "blocking_issues": []}


class AgentServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        self.root = Path(self.tempdir.name)
        controller = Orchestrator(self.root / "relay.db", self.root / "legacy")
        bridge = RelayBridge("cloud", controller, {"mock": MockWorker()})
        self.cli = FakeCLI()
        self.service = LarkRelayService(bridge, self.cli, {"oc-1"}, {"ou-1"})
        self.runtime = AgentRuntime(
            ControlStore(self.root / "relay.db"), FakeAgent(), [self.root], set(),
            notifier=self.service.notify,
        )
        self.service.runtime = self.runtime

    def tearDown(self):
        self.runtime.shutdown(wait=True)
        self.tempdir.cleanup()

    def event(self, event_id, content):
        return {"event_id": event_id, "message_id": "om-" + event_id,
                "chat_id": "oc-1", "sender_id": "ou-1", "message_type": "text", "content": content}

    def wait_turn(self, turn_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.runtime.store.get_turn(turn_id)["status"] == "succeeded":
                return
            time.sleep(0.02)
        self.fail("Turn did not finish")

    def test_agent_continue_and_async_notifications(self):
        accepted = self.service.process_event(self.event("one", "/agent first instruction"))
        self.wait_turn(accepted["turn_id"])
        continued = self.service.process_event(self.event(
            "two", f"/continue {accepted['task_id']} second instruction",
        ))
        self.wait_turn(continued["turn_id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            texts = [item[1] for item in self.cli.replies]
            if any("second instruction" in text and "执行成功" in text for text in texts):
                break
            time.sleep(0.02)
        texts = [item[1] for item in self.cli.replies]
        self.assertTrue(any("已创建长期任务" in text for text in texts))
        self.assertTrue(any("second instruction" in text and "执行成功" in text for text in texts))
        self.assertEqual(len(self.runtime.store.task_turns(accepted["task_id"])), 2)

    def test_duplicate_agent_event_is_idempotent(self):
        event = self.event("duplicate", "/agent only once")
        first = self.service.process_event(event)
        second = self.service.process_event(event)
        self.wait_turn(first["turn_id"])
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.runtime.store.list_tasks()), 1)

    def test_management_commands(self):
        accepted = self.service.process_event(self.event("manage", "/agent inspect"))
        self.wait_turn(accepted["turn_id"])
        self.service.process_event(self.event("status", f"/status {accepted['task_id']}"))
        self.service.process_event(self.event("logs", f"/logs {accepted['task_id']}"))
        self.service.process_event(self.event("tasks", "/tasks"))
        texts = "\n".join(item[1] for item in self.cli.replies)
        self.assertIn("Session：", texts)
        self.assertIn("最近日志", texts)
        self.assertIn("最近任务", texts)


if __name__ == "__main__":
    unittest.main()
