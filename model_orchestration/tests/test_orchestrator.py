import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.core import Orchestrator
from orchestrator.workers import MockWorker


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        root = Path(self.tempdir.name)
        self.controller = Orchestrator(root / "tasks.db", root / "artifacts")

    def tearDown(self):
        self.tempdir.cleanup()

    def create(self):
        return self.controller.create_task(
            "data_audit", "dataset_auditor", "controlled-reference",
            "Validate manifests and report blockers", "mock",
        )

    def test_idempotent_create(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.controller.list_tasks()), 1)

    def test_mock_task_roundtrip(self):
        task = self.create()
        result = self.controller.run_task(task["task_id"], MockWorker())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempts"], 1)
        result_path = Path(self.controller.artifacts_root) / task["task_id"] / "result.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "succeeded")
        self.assertIn("worker_observation", payload)

    def test_invalid_worker_result_becomes_failed(self):
        class BadWorker:
            def run(self, payload):
                return {"summary": "missing fields"}

        task = self.create()
        result = self.controller.run_task(task["task_id"], BadWorker())
        self.assertEqual(result["status"], "failed")
        self.assertIn("missing fields", result["error"])

    def test_task_type_allowlist(self):
        with self.assertRaises(ValueError):
            self.controller.create_task("shell", "worker", "x", "y", "mock")


if __name__ == "__main__":
    unittest.main()
