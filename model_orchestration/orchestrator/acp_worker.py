from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from .agent_store import ControlStore
from .policy import ExecutionPolicy, normalize_command
from .workers import parse_worker_json, validate_result


class KimiCodeWorker:
    """Run one isolated, read-only task through Kimi Code's stdio ACP server."""

    def __init__(self, cwd: Path, command: str = "kimi", timeout: int = 600):
        self.cwd = cwd.resolve()
        self.command = command
        self.timeout = timeout

    def run(self, payload: dict) -> dict:
        prompt = (
            "You are an isolated task worker. Use only the information in the task payload. "
            "Do not execute commands, modify files, or access external resources. "
            "Return only one JSON object with fields: status (succeeded|blocked|failed), "
            "summary (string), artifacts (array), unknowns (array), blocking_issues (array).\n"
            "Task payload:\n" + json.dumps(payload, ensure_ascii=False)
        )
        client = ACPClient(self.command, self.cwd, self.timeout)
        try:
            client.initialize()
            session_id = client.new_session()
            content, _ = client.prompt(session_id, prompt)
        finally:
            client.close()
        return validate_result(parse_worker_json(content))


class PersistentKimiAgent:
    """One task maps to one durable ACP session; each invocation resumes that session."""

    def __init__(self, store: ControlStore, command: str = "kimi", timeout: int = 7200):
        self.store = store
        self.command = command
        self.timeout = timeout
        self.clients: dict[str, ACPClient] = {}
        self.client_lock = threading.Lock()

    def cancel_turn(self, turn_id: str) -> None:
        with self.client_lock:
            client = self.clients.get(turn_id)
        if client:
            client.cancel()

    def mcp_servers(self, roots: list[Path], ssh_hosts: set[str]) -> list[dict]:
        package_root = Path(__file__).resolve().parent.parent
        args = ["-m", "orchestrator.controlled_mcp", "--db", str(self.store.db_path)]
        for root in roots:
            args.extend(["--root", str(root.resolve())])
        for host in sorted(ssh_hosts):
            args.extend(["--ssh-host", host])
        return [{
            "name": "personal-tools-controlled", "command": sys.executable, "args": args,
            "env": [{"name": "PYTHONPATH", "value": str(package_root)}],
        }]

    def run_turn(self, task: dict, turn: dict, roots: list[Path], ssh_hosts: set[str],
                 approval_handler: Callable[[dict], bool],
                 log_handler: Callable[[str], None] | None = None) -> dict:
        policy = ExecutionPolicy(roots, ssh_hosts)
        mcp_servers = self.mcp_servers(roots, ssh_hosts)
        client = ACPClient(
            self.command, Path(task["cwd"]), self.timeout, policy=policy,
            approval_handler=approval_handler, log_handler=log_handler,
            store=self.store, task_id=task["task_id"], turn_id=turn["turn_id"],
        )
        with self.client_lock:
            self.clients[turn["turn_id"]] = client
        try:
            capabilities = client.initialize()
            session_id = task.get("session_id")
            if session_id:
                agent_caps = capabilities.get("agentCapabilities", {})
                if not agent_caps.get("loadSession", False):
                    raise RuntimeError("Kimi Code does not advertise ACP loadSession; refusing to replace session")
                client.load_session(session_id, mcp_servers)
            else:
                session_id = client.new_session(mcp_servers)
                self.store.save_session(task["task_id"], session_id)
            prompt = (
                "You are a controlled long-running task worker. Continue the existing task context. "
                "Perform the requested work using available tools within the authorized workspace. "
                "For every shell command, use the MCP tool controlled_exec instead of the built-in Bash tool; "
                "controlled_exec returns real stdout/stderr and persists side effects. Use controlled_start "
                "and controlled_run_status for long jobs. Use controlled_read/controlled_write when file "
                "verification is needed. Do not use the built-in Bash tool because its ACP output is unreliable. "
                "Do not claim an action or artifact unless you verified it. For long external jobs, "
                "record their PID/run identifier and actual output path. Finish this turn with only one "
                "JSON object containing status (succeeded|blocked|failed), summary (string), artifacts "
                "(array of absolute file paths or {path,kind} objects), unknowns (array), and "
                "blocking_issues (array). Use blocked when human input or permission is needed.\n"
                f"Task: {task['task_id']}\nTurn: {turn['turn_id']}\nInstruction:\n{turn['instructions']}"
            )
            content, stop_reason = client.prompt(session_id, prompt)
            result = validate_result(parse_worker_json(content))
            result["_stop_reason"] = stop_reason
            return result
        finally:
            with self.client_lock:
                self.clients.pop(turn["turn_id"], None)
            client.close()


class ACPTerminal:
    def __init__(self, terminal_id: str, process: subprocess.Popen, run_id: str):
        self.terminal_id = terminal_id
        self.process = process
        self.run_id = run_id
        self.parts: deque[str] = deque()
        self.size = 0
        self.limit = 1024 * 1024
        self.lock = threading.Lock()
        self.reader_thread: threading.Thread | None = None

    def append(self, text: str) -> None:
        with self.lock:
            self.parts.append(text)
            self.size += len(text.encode("utf-8"))
            while self.size > self.limit and self.parts:
                removed = self.parts.popleft()
                self.size -= len(removed.encode("utf-8"))

    def output(self) -> str:
        with self.lock:
            return "".join(self.parts)


class ACPClient:
    def __init__(self, command: str, cwd: Path, timeout: int,
                 policy: ExecutionPolicy | None = None,
                 approval_handler: Callable[[dict], bool] | None = None,
                 log_handler: Callable[[str], None] | None = None,
                 store: ControlStore | None = None, task_id: str | None = None,
                 turn_id: str | None = None):
        executable = str(Path(command).resolve()) if "/" in command else command
        self.process = subprocess.Popen(
            [executable, "acp"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.cwd = cwd
        self.timeout = timeout
        self.policy = policy
        self.approval_handler = approval_handler
        self.log_handler = log_handler
        self.store = store
        self.task_id = task_id
        self.turn_id = turn_id
        self.next_id = 0
        self.message_parts: list[str] = []
        self.errors: deque[str] = deque(maxlen=20)
        self.messages: queue.Queue[dict | None] = queue.Queue()
        self.terminals: dict[str, ACPTerminal] = {}
        self.active_session: str | None = None
        self.cancelled = threading.Event()
        self.error_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self.output_reader = threading.Thread(target=self._drain_stdout, daemon=True)
        self.error_reader.start()
        self.output_reader.start()

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.errors.append(line.rstrip())

    def _drain_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                if line.strip():
                    try:
                        self.messages.put(json.loads(line))
                    except json.JSONDecodeError as exc:
                        self.errors.append(f"Invalid ACP JSON: {exc}")
        finally:
            self.messages.put(None)

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read(self) -> dict:
        try:
            message = self.messages.get(timeout=self.timeout)
        except queue.Empty:
            details = "\n".join(self.errors)
            raise TimeoutError(f"ACP request timed out. Kimi Code logs:\n{details}") from None
        if message is None:
            details = "\n".join(self.errors)
            raise RuntimeError(f"Kimi Code ACP exited unexpectedly. Logs:\n{details}")
        if self.cancelled.is_set():
            raise RuntimeError("Task cancellation requested")
        return message

    def _request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self._read()
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise RuntimeError(f"ACP {method} failed: {message['error']}")
                return message["result"]
            self._handle_other(message)

    def _handle_other(self, message: dict) -> None:
        method = message.get("method")
        if method == "session/update":
            update = message.get("params", {}).get("update", {})
            update_type = update.get("sessionUpdate")
            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    self.message_parts.append(content.get("text", ""))
            elif update_type in {"tool_call", "tool_call_update", "plan"} and self.log_handler:
                self.log_handler(json.dumps(update, ensure_ascii=False)[:4000])
        elif method and "id" in message:
            self._answer_server_request(message)

    def _answer_server_request(self, message: dict) -> None:
        try:
            method = message.get("method")
            params = message.get("params", {})
            if method == "session/request_permission":
                result = self._permission_result(params)
            elif method == "fs/read_text_file" and self.policy:
                result = self._read_file(params)
            elif method == "fs/write_text_file" and self.policy:
                result = self._write_file(params)
            elif method and method.startswith("terminal/") and self.policy:
                result = self._terminal(method, params)
            else:
                self._send({
                    "jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32601, "message": "Client capability is disabled"},
                })
                return
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except Exception as exc:
            self._send({
                "jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"},
            })

    def _permission_result(self, params: dict) -> dict:
        options = params.get("options", [])
        reject = next((item for item in options if str(item.get("kind", "")).startswith("reject")), None)
        allow = next((item for item in options if item.get("kind") == "allow_once"), None)
        decision, reason = ("deny", "No execution policy configured")
        if self.policy:
            decision, reason = self.policy.permission_decision(params.get("toolCall", {}))
        approved = False
        if decision == "allow":
            approved = allow is not None
        elif decision == "approve" and self.approval_handler and allow:
            approved = self.approval_handler(self.policy.audit_record(params))
        if self.log_handler:
            self.log_handler(json.dumps({
                "permission": decision, "reason": reason, "approved": approved,
                "tool": params.get("toolCall", {}).get("title"),
            }, ensure_ascii=False))
        if approved:
            outcome = {"outcome": "selected", "optionId": allow["optionId"]}
        elif reject:
            outcome = {"outcome": "selected", "optionId": reject["optionId"]}
        else:
            outcome = {"outcome": "cancelled"}
        return outcome

    def _read_file(self, params: dict) -> dict:
        path = self.policy.check_path(params["path"])
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        start = max(1, int(params.get("line") or 1)) - 1
        limit = params.get("limit")
        selected = lines[start:start + int(limit)] if limit is not None else lines[start:]
        return {"content": "".join(selected)}

    def _write_file(self, params: dict) -> dict:
        path = self.policy.check_path(params["path"], write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params["content"], encoding="utf-8")
        if self.log_handler:
            self.log_handler(f"Wrote file: {path}")
        return {}

    def _terminal(self, method: str, params: dict) -> dict:
        name = method.split("/", 1)[1]
        if name == "create":
            command = normalize_command(params)
            decision, reason = self.policy.command_decision(params, "ACP terminal")
            if decision != "allow":
                if decision != "approve" or not self.approval_handler or not self.approval_handler({
                    "kind": "execute", "title": "ACP terminal", "raw_input": params,
                }):
                    raise PermissionError(reason)
            cwd = self.policy.check_cwd(params.get("cwd"))
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            terminal_id, run_id = "term_" + uuid.uuid4().hex, "RUN-" + uuid.uuid4().hex[:16]
            terminal = ACPTerminal(terminal_id, process, run_id)
            self.terminals[terminal_id] = terminal
            if self.store and self.task_id and self.turn_id:
                self.store.record_external_run(run_id, self.task_id, self.turn_id, process.pid, command, cwd)

            def read_output() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    terminal.append(line)
                code = process.wait()
                process.stdout.close()
                if self.store:
                    self.store.finish_external_run(run_id, code)
            terminal.reader_thread = threading.Thread(target=read_output, daemon=True)
            terminal.reader_thread.start()
            return {"terminalId": terminal_id}
        terminal = self.terminals.get(params.get("terminalId"))
        if terminal is None:
            raise KeyError(f"Unknown terminal: {params.get('terminalId')}")
        if name == "output":
            code = terminal.process.poll()
            return {
                "output": terminal.output(), "truncated": terminal.size >= terminal.limit,
                "exitStatus": {"exitCode": code, "signal": None} if code is not None else None,
            }
        if name == "wait_for_exit":
            return {"exitCode": terminal.process.wait(), "signal": None}
        if name == "kill":
            terminal.process.terminate()
            return {}
        if name == "release":
            if terminal.process.poll() is None:
                terminal.process.terminate()
            terminal.process.wait(timeout=15)
            if terminal.reader_thread:
                terminal.reader_thread.join(timeout=15)
            self.terminals.pop(terminal.terminal_id, None)
            return {}
        raise ValueError(f"Unsupported terminal method: {method}")

    def initialize(self) -> dict:
        execute = self.policy is not None
        return self._request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "personal-tools-relay", "version": "2.0.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": execute, "writeTextFile": execute},
                "terminal": execute,
            },
        })

    def new_session(self, mcp_servers: list[dict] | None = None) -> str:
        result = self._request("session/new", {
            "cwd": str(self.cwd), "mcpServers": mcp_servers or [],
        })
        self.active_session = result["sessionId"]
        return self.active_session

    def load_session(self, session_id: str, mcp_servers: list[dict] | None = None) -> None:
        self.message_parts = []
        self._request("session/load", {
            "sessionId": session_id, "cwd": str(self.cwd), "mcpServers": mcp_servers or [],
        })
        self.message_parts = []
        self.active_session = session_id

    def prompt(self, session_id: str, text: str) -> tuple[str, str]:
        self.message_parts = []
        self.active_session = session_id
        result = self._request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        })
        content = "".join(self.message_parts).strip()
        if not content:
            raise RuntimeError("Kimi Code returned no text content")
        return content, str(result.get("stopReason") or "unknown")

    def cancel(self) -> None:
        self.cancelled.set()
        if self.active_session and self.process.poll() is None:
            try:
                self._send({
                    "jsonrpc": "2.0", "method": "session/cancel",
                    "params": {"sessionId": self.active_session},
                })
            except (BrokenPipeError, OSError):
                pass
        for terminal in list(self.terminals.values()):
            if terminal.process.poll() is None:
                terminal.process.terminate()

    def close(self) -> None:
        for terminal in list(self.terminals.values()):
            if terminal.process.poll() is None:
                terminal.process.terminate()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
