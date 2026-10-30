import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.agent_store import ControlStore
from orchestrator.runtime import AgentRuntime


class FakeAgent:
    def run_turn(self, task, turn, roots, ssh_hosts, approval_handler, log_handler):
        log_handler("fake worker heartbeat")
        path = Path(task["cwd"]) / f"{turn['turn_id']}.json"
        path.write_text('{"verified":true}\n')
        return {
            "status": "succeeded", "summary": "Turn completed",
            "artifacts": [{"path": str(path), "kind": "result"}],
            "unknowns": [], "blocking_issues": [], "_stop_reason": "end_turn",
        }


class AgentRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        self.root = Path(self.tempdir.name)
        self.store = ControlStore(self.root / "control.db")
        self.notifications = []
        self.runtime = AgentRuntime(
            self.store, FakeAgent(), [self.root], set(),
            notifier=lambda event, data: self.notifications.append((event, data)), max_workers=2,
        )

    def tearDown(self):
        self.runtime.shutdown()
        self.tempdir.cleanup()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for runtime state")

    def test_task_continue_and_verified_artifact(self):
        task, turn = self.runtime.create_task("prepare data")
        self.wait_for(lambda: self.store.get_turn(turn["turn_id"])["status"] == "succeeded")
        second = self.runtime.continue_task(task["task_id"], "evaluate data")
        self.wait_for(lambda: self.store.get_turn(second["turn_id"])["status"] == "succeeded")
        current = self.store.get_task(task["task_id"])
        artifacts = self.store.artifacts(task["task_id"])
        self.assertEqual(current["status"], "waiting_input")
        self.assertEqual(len(self.store.task_turns(task["task_id"])), 2)
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(all(item["valid"] and len(item["sha256"]) == 64 for item in artifacts))

    def test_goal_schedules_dependent_step_and_checks_expected_artifact(self):
        first_turn_box = {}
        original_create = self.runtime.create_task

        def capture(*args, **kwargs):
            task, turn = original_create(*args, **kwargs)
            first_turn_box.setdefault("first", turn)
            return task, turn
        self.runtime.create_task = capture
        goal = self.runtime.create_goal("prepare then evaluate", [
            {"id": "prepare", "instructions": "prepare", "depends_on": []},
            {"id": "evaluate", "instructions": "evaluate", "depends_on": ["prepare"]},
        ])
        self.wait_for(lambda: self.store.get_goal(goal["goal_id"])["status"] == "succeeded")
        current = self.store.get_goal(goal["goal_id"])
        self.assertTrue(all(step["status"] == "succeeded" for step in current["steps"]), current)
        self.assertIsNotNone(current["steps"][1]["task_id"])

    def test_missing_expected_artifact_blocks_goal(self):
        class MissingArtifactAgent:
            def run_turn(self, task, turn, roots, ssh_hosts, approval_handler, log_handler):
                return {"status": "succeeded", "summary": "Claims completion", "artifacts": [],
                        "unknowns": [], "blocking_issues": []}
        self.runtime.agent = MissingArtifactAgent()
        goal = self.runtime.create_goal("must verify artifact", [
            {"id": "prepare", "instructions": "prepare",
             "expected_artifacts": [str(self.root / "required.json")]},
        ])
        self.wait_for(lambda: self.store.get_goal(goal["goal_id"])["status"] == "blocked")
        current = self.store.get_goal(goal["goal_id"])
        self.assertIn("Expected artifacts missing", current["steps"][0]["error"])

    def test_restart_blocks_running_turn_without_changing_session(self):
        task, turn = self.store.create_task("long task", "agent", self.root)
        store_claim, turn_claim = self.store.claim_turn(turn["turn_id"])
        self.store.save_session(task["task_id"], "session-preserved")
        recovered = self.store.recover_after_restart()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(self.store.get_turn(turn["turn_id"])["status"], "blocked")
        self.assertEqual(self.store.get_task(task["task_id"])["session_id"], "session-preserved")


if __name__ == "__main__":
    unittest.main()
