import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.controlled_mcp import ControlledTools, MCPServer


class ControlledMCPTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir="model_orchestration")
        self.root = Path(self.tempdir.name).resolve()
        self.tools = ControlledTools([self.root], set(), self.root / "control.db")
        self.server = MCPServer(self.tools)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_initialize_and_list_tools(self):
        initialized = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "personal-tools-controlled")
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, {
            "controlled_exec", "controlled_start", "controlled_run_status",
            "controlled_read", "controlled_write",
        })

    def test_exec_returns_real_stdout_and_exit_code(self):
        result = self.tools.call("controlled_exec", {
            "command": [sys.executable, "-c", "print('real stdout')"], "cwd": str(self.root),
        })
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "real stdout\n")
        self.assertGreater(result["pid"], 0)
        with self.tools.store.connect() as db:
            row = db.execute("SELECT pid,status,exit_code FROM external_runs WHERE run_id=?",
                             (result["run_id"],)).fetchone()
        self.assertEqual(row["pid"], result["pid"])
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["exit_code"], 0)

    def test_write_and_read_persist_file(self):
        path = self.root / "nested" / "evidence.txt"
        written = self.tools.call("controlled_write", {"path": str(path), "content": "verified\n"})
        self.assertTrue(written["verified"])
        self.assertEqual(path.read_text(encoding="utf-8"), "verified\n")
        read = self.tools.call("controlled_read", {"path": str(path)})
        self.assertEqual(read["content"], "verified\n")

    def test_outside_path_is_rejected(self):
        outside = self.root.parent / "outside-controlled-mcp.txt"
        with self.assertRaises(PermissionError):
            self.tools.call("controlled_write", {"path": str(outside), "content": "denied"})
        self.assertFalse(outside.exists())

    def test_long_run_reports_pid_output_and_finished_status(self):
        started = self.tools.call("controlled_start", {
            "command": [sys.executable, "-c", "print('long run output')"], "cwd": str(self.root),
        })
        self.assertGreater(started["pid"], 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.tools.call("controlled_run_status", {"run_id": started["run_id"]})
            if status["status"] == "finished":
                break
            time.sleep(0.02)
        self.assertEqual(status["status"], "finished")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["pid"], started["pid"])
        self.assertIn("long run output", status["output"])
        self.assertNotEqual(Path(started["log_path"]).parent, self.root)

    def test_timeout_kills_process_and_records_exit(self):
        result = self.tools.call("controlled_exec", {
            "command": [sys.executable, "-c", "import time; time.sleep(30)"],
            "cwd": str(self.root), "timeout": 1,
        })
        self.assertEqual(result["exit_code"], 124)
        self.assertIn("timed out", result["error"])
        status = self.tools.call("controlled_run_status", {"run_id": result["run_id"]})
        self.assertEqual(status["status"], "finished")
        self.assertEqual(status["exit_code"], 124)

    def test_tools_call_returns_json_content(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "controlled_exec", "arguments": {
                "command": [sys.executable, "-c", "print('mcp')"], "cwd": str(self.root),
            }},
        })
        self.assertFalse(response["result"]["isError"])
        value = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(value["stdout"], "mcp\n")


if __name__ == "__main__":
    unittest.main()
