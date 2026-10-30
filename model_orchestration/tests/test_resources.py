import tempfile
import unittest
from pathlib import Path

from orchestrator.agent_store import ControlStore


class ResourceAndPlanTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        self.store = ControlStore(Path(self.tempdir.name) / "control.db")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_resource_lock_excludes_other_task_and_releases(self):
        self.assertIsNone(self.store.acquire_resources("TASK-1", "TURN-1", ["gpu:h20:0"]))
        self.assertEqual(
            self.store.acquire_resources("TASK-2", "TURN-2", ["gpu:h20:0"]), "gpu:h20:0"
        )
        self.store.release_resources("TASK-1")
        self.assertIsNone(self.store.acquire_resources("TASK-2", "TURN-2", ["gpu:h20:0"]))

    def test_dependency_cycle_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.store.create_goal("cyclic", [
                {"id": "A", "instructions": "a", "depends_on": ["B"]},
                {"id": "B", "instructions": "b", "depends_on": ["A"]},
            ])

    def test_goal_resources_are_persisted(self):
        goal = self.store.create_goal("training", [
            {"id": "train", "instructions": "train", "resources": ["gpu:h20:0"]},
        ])
        self.assertEqual(self.store.step_resources(goal["steps"][0]["step_id"]), ["gpu:h20:0"])


if __name__ == "__main__":
    unittest.main()
