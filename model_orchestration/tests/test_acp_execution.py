import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.acp_worker import ACPClient
from orchestrator.agent_store import ControlStore
from orchestrator.policy import ExecutionPolicy


FAKE_EXEC_ACP = r'''#!/usr/bin/env python3
import json
import sys

next_request = 100

def call(method, params):
    global next_request
    next_request += 1
    request_id = next_request
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":request_id,"method":method,"params":params}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message["result"]

for line in sys.stdin:
    request = json.loads(line)
    method, request_id, params = request.get("method"), request.get("id"), request.get("params", {})
    if method == "initialize":
        result = {"protocolVersion":1,"agentCapabilities":{"loadSession":True}}
    elif method == "session/new":
        result = {"sessionId":"session-exec"}
    elif method == "session/prompt":
        permission = call("session/request_permission", {
            "sessionId":"session-exec",
            "toolCall":{"toolCallId":"call-1","kind":"execute","title":"Create verified output",
                        "rawInput":{"command":["python3","-c","pass"],"cwd":params.get("cwd")}},
            "options":[{"optionId":"allow","kind":"allow_once","name":"Allow"},
                       {"optionId":"reject","kind":"reject_once","name":"Reject"}]
        })
        terminal = call("terminal/create", {
            "sessionId":"session-exec","command":["python3","-c",
            "from pathlib import Path; Path('command-output.txt').write_text('command ran')"],
            "cwd":"__ROOT__","env":[],"outputByteLimit":10000
        })
        call("terminal/wait_for_exit", {"sessionId":"session-exec","terminalId":terminal["terminalId"]})
        call("terminal/release", {"sessionId":"session-exec","terminalId":terminal["terminalId"]})
        call("fs/write_text_file", {"sessionId":"session-exec","path":"__ROOT__/fs-output.txt","content":"fs wrote"})
        read = call("fs/read_text_file", {"sessionId":"session-exec","path":"__ROOT__/fs-output.txt"})
        worker_result = {"status":"succeeded","summary":read["content"],"artifacts":[],
                         "unknowns":[],"blocking_issues":[]}
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{
            "sessionId":"session-exec","update":{"sessionUpdate":"agent_message_chunk",
            "content":{"type":"text","text":json.dumps(worker_result)}}}}) + "\n")
        sys.stdout.flush()
        result = {"stopReason":"end_turn"}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":request_id,"result":result}) + "\n")
    sys.stdout.flush()
'''


class ACPExecutionTest(unittest.TestCase):
    def test_permission_terminal_and_filesystem_roundtrip(self):
        with tempfile.TemporaryDirectory(dir="model_orchestration") as tempdir:
            root = Path(tempdir).resolve()
            executable = root / "fake-kimi-exec"
            executable.write_text(FAKE_EXEC_ACP.replace("__ROOT__", str(root)), encoding="utf-8")
            executable.chmod(0o755)
            store = ControlStore(root / "control.db")
            task, turn = store.create_task("execute", "agent", root)
            store.claim_turn(turn["turn_id"])
            client = ACPClient(
                str(executable), root, 5, policy=ExecutionPolicy([root]),
                approval_handler=lambda request: False, store=store,
                task_id=task["task_id"], turn_id=turn["turn_id"],
            )
            try:
                client.initialize()
                session = client.new_session()
                content, stop = client.prompt(session, "execute")
            finally:
                client.close()
            self.assertEqual(json.loads(content)["summary"], "fs wrote")
            self.assertEqual(stop, "end_turn")
            self.assertEqual((root / "command-output.txt").read_text(), "command ran")
            self.assertEqual((root / "fs-output.txt").read_text(), "fs wrote")
            with store.connect() as db:
                run = db.execute("SELECT * FROM external_runs").fetchone()
            self.assertEqual(run["status"], "finished")
            self.assertEqual(run["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
