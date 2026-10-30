from __future__ import annotations

import json
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path

from .workers import validate_result


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
            content = client.prompt(session_id, prompt)
        finally:
            client.close()
        return validate_result(parse_worker_json(content))


def parse_worker_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if lines[0].strip() in {"```", "```json"}:
            text = "\n".join(lines[1:-1]).strip()
    return json.loads(text)


class ACPClient:
    def __init__(self, command: str, cwd: Path, timeout: int):
        self.process = subprocess.Popen(
            [command, "acp"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.cwd = cwd
        self.timeout = timeout
        self.next_id = 0
        self.message_parts: list[str] = []
        self.errors: deque[str] = deque(maxlen=20)
        self.messages: queue.Queue[dict | None] = queue.Queue()
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
        for line in self.process.stdout:
            if line.strip():
                self.messages.put(json.loads(line))
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
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    self.message_parts.append(content.get("text", ""))
        elif method and "id" in message:
            self._answer_server_request(message)

    def _answer_server_request(self, message: dict) -> None:
        if message.get("method") == "session/request_permission":
            options = message.get("params", {}).get("options", [])
            reject = next((item for item in options if str(item.get("kind", "")).startswith("reject")), None)
            outcome = (
                {"outcome": "selected", "optionId": reject["optionId"]}
                if reject else {"outcome": "cancelled"}
            )
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": outcome})
        else:
            self._send({
                "jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": "Client capability is disabled"},
            })

    def initialize(self) -> None:
        self._request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "personal-tools-relay", "version": "1.0.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
        })

    def new_session(self) -> str:
        result = self._request("session/new", {"cwd": str(self.cwd), "mcpServers": []})
        return result["sessionId"]

    def prompt(self, session_id: str, text: str) -> str:
        self.message_parts = []
        self._request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        })
        content = "".join(self.message_parts).strip()
        if not content:
            raise RuntimeError("Kimi Code returned no text content")
        return content

    def close(self) -> None:
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
