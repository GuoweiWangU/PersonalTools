import tempfile
import threading
import time
import unittest
from pathlib import Path

from orchestrator.agent_store import ControlStore
from orchestrator.runtime import AgentRuntime


class ApprovalAgent:
    def run_turn(self, task, turn, roots, ssh_hosts, approval_handler, log_handler):
        approved = approval_handler({"kind": "execute", "title": "git push", "raw_input": {"command": "git push"}})
        return {"status": "succeeded" if approved else "blocked", "summary": "approval tested",
                "artifacts": [], "unknowns": [], "blocking_issues": [] if approved else ["denied"]}


class BlockingAgent:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def run_turn(self, task, turn, roots, ssh_hosts, approval_handler, log_handler):
        self.started.set()
        if not self.cancelled.wait(5):
            raise RuntimeError("Cancellation test timed out")
        raise RuntimeError("Task cancellation requested")

    def cancel_turn(self, turn_id):
        self.cancelled.set()


class ImmediateAgent:
    def run_turn(self, task, turn, roots, ssh_hosts, approval_handler, log_handler):
        return {"status": "succeeded", "summary": "done", "artifacts": [],
                "unknowns": [], "blocking_issues": []}


class ControlFlowTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        self.root = Path(self.tempdir.name)
        self.store = ControlStore(self.root / "control.db")

    def tearDown(self):
        self.tempdir.cleanup()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("State did not change in time")

    def test_explicit_approval_resumes_waiting_turn(self):
        runtime = AgentRuntime(self.store, ApprovalAgent(), [self.root], set())
        try:
            task, turn = runtime.create_task("publish change")
            self.wait_for(lambda: self.store.pending_approval(task["task_id"]) is not None)
            self.assertEqual(self.store.get_turn(turn["turn_id"])["status"], "waiting_approval")
            runtime.decide_approval(task["task_id"], True)
            self.wait_for(lambda: self.store.get_turn(turn["turn_id"])["status"] == "succeeded")
        finally:
            runtime.shutdown(wait=True)

    def test_cancel_stays_cancelled(self):
        agent = BlockingAgent()
        runtime = AgentRuntime(self.store, agent, [self.root], set())
        try:
            task, turn = runtime.create_task("long task")
            self.assertTrue(agent.started.wait(5))
            runtime.cancel(task["task_id"])
            self.wait_for(lambda: self.store.get_turn(turn["turn_id"])["status"] == "cancelled")
            self.assertEqual(self.store.get_task(task["task_id"])["status"], "cancelled")
        finally:
            runtime.shutdown(wait=True)

    def test_queued_turn_is_resumed_after_restart(self):
        task, turn = self.store.create_task("queued before restart", "agent", self.root)
        runtime = AgentRuntime(self.store, ImmediateAgent(), [self.root], set())
        try:
            self.wait_for(lambda: self.store.get_turn(turn["turn_id"])["status"] == "succeeded")
        finally:
            runtime.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
