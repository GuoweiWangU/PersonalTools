import tempfile
import unittest
from pathlib import Path

from orchestrator.acp_worker import KimiCodeWorker, parse_worker_json


FAKE_ACP = r'''#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentInfo": {"name": "fake-kimi", "version": "1"}}
    elif method == "session/new":
        result = {"sessionId": "session-test"}
    elif method == "session/prompt":
        worker_result = {
            "status": "succeeded", "summary": "Fake ACP task completed",
            "artifacts": [], "unknowns": [], "blocking_issues": []
        }
        notification = {
            "jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "session-test", "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": json.dumps(worker_result)}
            }}
        }
        sys.stdout.write(json.dumps(notification) + "\n")
        sys.stdout.flush()
        result = {"stopReason": "end_turn"}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()
'''


class KimiCodeWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        self.root = Path(self.tempdir.name)
        self.executable = self.root / "fake-kimi"
        self.executable.write_text(FAKE_ACP, encoding="utf-8")
        self.executable.chmod(0o755)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_fake_acp_roundtrip(self):
        worker = KimiCodeWorker(self.root, str(self.executable), timeout=5)
        result = worker.run({
            "task_id": "TASK-acp-test", "task_type": "generic", "role": "tester",
            "input_ref": "none", "instructions": "Return the test result"
        })
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["summary"], "Fake ACP task completed")

    def test_json_code_fence_is_accepted(self):
        result = parse_worker_json('```json\n{"status":"succeeded","summary":"ok",'
                                   '"artifacts":[],"unknowns":[],"blocking_issues":[]}\n```')
        self.assertEqual(result["summary"], "ok")


if __name__ == "__main__":
    unittest.main()
