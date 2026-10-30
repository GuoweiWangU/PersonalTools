import tempfile
import unittest
from pathlib import Path

from orchestrator.policy import ExecutionPolicy


class ExecutionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        self.root = Path(self.tempdir.name)
        self.policy = ExecutionPolicy([self.root], {"h20-dev1"})

    def tearDown(self):
        self.tempdir.cleanup()

    def test_read_and_safe_command_are_allowed(self):
        self.assertEqual(self.policy.permission_decision({"kind": "read"})[0], "allow")
        decision = self.policy.command_decision({
            "command": ["python3", "train.py"], "cwd": str(self.root),
        })
        self.assertEqual(decision[0], "allow")

    def test_write_outside_root_is_denied(self):
        decision = self.policy.permission_decision({
            "kind": "edit", "rawInput": {"path": "/etc/passwd"},
        })
        self.assertEqual(decision[0], "deny")

    def test_ssh_allowlist_is_enforced(self):
        allowed = self.policy.command_decision({"command": "ssh h20-dev1 nvidia-smi"})
        denied = self.policy.command_decision({"command": "ssh unknown-host nvidia-smi"})
        self.assertEqual(allowed[0], "allow")
        self.assertEqual(denied[0], "deny")

    def test_high_risk_command_requires_explicit_approval(self):
        decision = self.policy.command_decision({"command": "git push origin main"})
        self.assertEqual(decision[0], "approve")

    def test_parent_traversal_is_denied(self):
        decision = self.policy.command_decision({"command": "python3 ../outside.py"})
        self.assertEqual(decision[0], "deny")


if __name__ == "__main__":
    unittest.main()
