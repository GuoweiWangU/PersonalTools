from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .agent_store import ControlStore
from .policy import ExecutionPolicy, normalize_command


class ControlledTools:
    def __init__(self, roots: list[Path], ssh_hosts: set[str], db_path: Path,
                 task_id: str = "MCP", turn_id: str = "MCP"):
        self.policy = ExecutionPolicy(roots, ssh_hosts)
        self.store = ControlStore(db_path)
        self.task_id = task_id
        self.turn_id = turn_id
        self.runs_root = db_path.resolve().parent / "controlled-runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def schemas() -> list[dict]:
        command = {
            "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
            "description": "Shell command string or argv array",
        }
        cwd = {"type": "string", "description": "Absolute authorized working directory"}
        return [
            {"name": "controlled_exec", "description": "Execute a command and return real stdout, stderr and exit code",
             "inputSchema": {"type": "object", "properties": {
                 "command": command, "cwd": cwd,
                 "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
             }, "required": ["command"]}},
            {"name": "controlled_start", "description": "Start a long command, persist output and return run_id/PID",
             "inputSchema": {"type": "object", "properties": {"command": command, "cwd": cwd},
                             "required": ["command"]}},
            {"name": "controlled_run_status", "description": "Read a controlled run's recorded status and real output",
             "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}},
                             "required": ["run_id"]}},
            {"name": "controlled_read", "description": "Read a UTF-8 text file inside authorized roots",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                             "required": ["path"]}},
            {"name": "controlled_write", "description": "Write a UTF-8 text file inside authorized roots",
             "inputSchema": {"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"},
             }, "required": ["path", "content"]}},
        ]

    def call(self, name: str, args: dict) -> dict:
        if name == "controlled_exec":
            return self.execute(args)
        if name == "controlled_start":
            return self.start(args)
        if name == "controlled_run_status":
            return self.status(args["run_id"])
        if name == "controlled_read":
            path = self.policy.check_path(args["path"])
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}
        if name == "controlled_write":
            path = self.policy.check_path(args["path"], write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            return {"path": str(path), "bytes": path.stat().st_size, "verified": True}
        raise ValueError(f"Unknown controlled tool: {name}")

    def _command(self, args: dict) -> tuple[list[str], Path]:
        decision, reason = self.policy.command_decision(args, "Controlled MCP command")
        if decision != "allow":
            raise PermissionError(f"Command requires ACP approval or is denied: {reason}")
        return normalize_command(args), self.policy.check_cwd(args.get("cwd"))

    def execute(self, args: dict) -> dict:
        command, cwd = self._command(args)
        run_id = "RUN-" + uuid.uuid4().hex[:16]
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.store.record_external_run(run_id, self.task_id, self.turn_id, process.pid, command, cwd)
        try:
            stdout, stderr = process.communicate(
                timeout=min(int(args.get("timeout", 300)), 3600),
            )
            self.store.finish_external_run(run_id, process.returncode)
            return {"run_id": run_id, "pid": process.pid, "exit_code": process.returncode,
                    "stdout": stdout, "stderr": stderr}
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            self.store.finish_external_run(run_id, 124)
            return {"run_id": run_id, "pid": process.pid, "exit_code": 124,
                    "stdout": stdout or exc.stdout or "", "stderr": stderr or exc.stderr or "",
                    "error": "Command timed out"}

    def start(self, args: dict) -> dict:
        command, cwd = self._command(args)
        run_id = "RUN-" + uuid.uuid4().hex[:16]
        log_path = self.runs_root / f"{run_id}.log"
        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        self.store.record_external_run(run_id, self.task_id, self.turn_id, process.pid, command, cwd)

        def wait_for_exit() -> None:
            self.store.finish_external_run(run_id, process.wait())

        threading.Thread(target=wait_for_exit, daemon=True).start()
        return {"run_id": run_id, "pid": process.pid, "log_path": str(log_path), "status": "started"}

    def status(self, run_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM external_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        status, exit_code = row["status"], row["exit_code"]
        if status == "running":
            try:
                os.kill(row["pid"], 0)
                status = "running"
            except ProcessLookupError:
                status = "exit_unverified"
        log_path = self.runs_root / f"{run_id}.log"
        output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        return {"run_id": run_id, "status": status, "exit_code": exit_code,
                "pid": row["pid"], "output": output[-65536:]}


class MCPServer:
    def __init__(self, tools: ControlledTools):
        self.tools = tools

    def handle(self, request: dict) -> dict | None:
        method = request.get("method")
        if method and method.startswith("notifications/"):
            return None
        request_id = request.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "personal-tools-controlled", "version": "1.0.0"},
                    "instructions": "Use controlled_exec instead of Bash for visible, persistent command execution.",
                }
            elif method == "tools/list":
                result = {"tools": self.tools.schemas()}
            elif method == "tools/call":
                params = request.get("params", {})
                value = self.tools.call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                          "isError": False}
            elif method == "ping":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                }}
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}}

    def serve(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            response = self.handle(json.loads(line))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--ssh-host", action="append", default=[])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--task-id", default="MCP")
    parser.add_argument("--turn-id", default="MCP")
    args = parser.parse_args()
    MCPServer(ControlledTools(
        args.root, set(args.ssh_host), args.db, args.task_id, args.turn_id,
    )).serve()


if __name__ == "__main__":
    main()
