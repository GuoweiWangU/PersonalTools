import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.acp_worker import KimiCodeWorker, PersistentKimiAgent, parse_worker_json
from orchestrator.agent_store import ControlStore


FAKE_ACP = r'''#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method in {"session/new", "session/load", "session/prompt"}:
        with open("__LOG__", "a") as handle:
            handle.write(method + "\n")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentInfo": {"name": "fake-kimi", "version": "1"},
                  "agentCapabilities": {"loadSession": True}}
    elif method == "session/new":
        result = {"sessionId": "session-test"}
    elif method == "session/load":
        result = {}
    elif method == "session/prompt":
        worker_result = {
            "status": "succeeded", "summary": "Fake ACP task completed",
            "artifacts": [], "unknowns": [], "blocking_issues": []
        }
        notification = {
            "jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": request["params"]["sessionId"], "update": {
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
        self.log = self.root / "calls.log"
        self.executable = self.root / "fake-kimi"
        self.executable.write_text(FAKE_ACP.replace("__LOG__", str(self.log)), encoding="utf-8")
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

    def test_persistent_agent_loads_same_session(self):
        store = ControlStore(self.root / "control.db")
        task, first = store.create_task("first turn", "agent", self.root)
        agent = PersistentKimiAgent(store, str(self.executable), timeout=5)
        first_result = agent.run_turn(task, first, [self.root], set(), lambda request: False)
        store.complete_turn(first["turn_id"], first_result["status"], first_result)
        second = store.enqueue_turn(task["task_id"], "second turn")
        second_result = agent.run_turn(
            store.get_task(task["task_id"]), second, [self.root], set(), lambda request: False
        )
        self.assertEqual(first_result["status"], "succeeded")
        self.assertEqual(second_result["status"], "succeeded")
        self.assertEqual(store.get_task(task["task_id"])["session_id"], "session-test")
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls.count("session/new"), 1)
        self.assertEqual(calls.count("session/load"), 1)
        self.assertEqual(calls.count("session/prompt"), 2)


if __name__ == "__main__":
    unittest.main()
