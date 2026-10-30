from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Iterator

from .core import Orchestrator
from .relay import RelayBridge, encode_relay_message
from .workers import MockWorker

LARK_ENV_VARS = (
    "OPENCLAW_HOME", "OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "HERMES_HOME", "LARK_CHANNEL"
)


class LarkCLI:
    """Small subprocess adapter; task content is never interpolated into a shell command."""

    def __init__(self, executable: str = "lark-cli"):
        self.executable = executable

    def _command(self, *args: str) -> list[str]:
        return [self.executable, *args]

    def _environment(self) -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key not in LARK_ENV_VARS}

    def reply(self, message_id: str, text: str, idempotency_key: str) -> dict:
        result = subprocess.run(
            self._command(
                "im", "+messages-reply", "--as", "bot", "--message-id", message_id,
                "--text", text, "--idempotency-key", idempotency_key, "--json",
            ),
            check=True, capture_output=True, text=True, env=self._environment(),
        )
        return json.loads(result.stdout)

    def events(self, timeout: str | None = None, max_events: int = 0) -> Iterator[dict]:
        command = self._command("event", "consume", "im.message.receive_v1", "--as", "bot")
        if timeout:
            command.extend(["--timeout", timeout])
        if max_events:
            command.extend(["--max-events", str(max_events)])
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self._environment(),
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    raise RuntimeError("Lark event consumer exited before its ready marker")
                if "[event] ready event_key=im.message.receive_v1" in line:
                    break

            def drain_errors() -> None:
                for _ in process.stderr:
                    pass

            error_reader = threading.Thread(target=drain_errors, daemon=True)
            error_reader.start()
            for line in process.stdout:
                if line.strip():
                    yield json.loads(line)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)


class LarkRelayService:
    def __init__(self, bridge: RelayBridge, cli: LarkCLI,
                 allowed_chat_ids: set[str], allowed_sender_ids: set[str]):
        if not allowed_chat_ids or not allowed_sender_ids:
            raise ValueError("Both chat and sender allowlists are required")
        self.bridge = bridge
        self.cli = cli
        self.allowed_chat_ids = allowed_chat_ids
        self.allowed_sender_ids = allowed_sender_ids

    def process_event(self, event: dict) -> dict | None:
        if event.get("chat_id") not in self.allowed_chat_ids:
            return None
        if event.get("sender_id") not in self.allowed_sender_ids:
            return None
        if event.get("message_type") != "text" or not event.get("message_id"):
            return None
        response = self.bridge.handle_event(event)
        if response is None:
            return None
        reply_target = event["message_id"]
        delivery_key = hashlib.sha256(
            f"relay-result:{event.get('event_id') or reply_target}".encode()
        ).hexdigest()[:32]
        self.cli.reply(reply_target, encode_relay_message(response), delivery_key)
        return response


def build_service(args: argparse.Namespace) -> LarkRelayService:
    from .acp_worker import KimiCodeWorker

    controller = Orchestrator(args.db, args.artifacts)
    workers = {}
    for specification in args.worker or ["mock"]:
        name, separator, kind = specification.partition("=")
        kind = kind if separator else "mock"
        if not name:
            raise ValueError("Worker name must be non-empty")
        if kind == "mock":
            workers[name] = MockWorker()
        elif kind == "acp":
            if not args.acp_cwd:
                raise ValueError("--acp-cwd is required for an ACP worker")
            workers[name] = KimiCodeWorker(args.acp_cwd, args.acp_command, args.acp_timeout)
        else:
            raise ValueError(f"Unsupported worker type: {kind}")
    bridge = RelayBridge(args.bridge_id, controller, workers)
    return LarkRelayService(
        bridge, LarkCLI(args.lark_cli), set(args.chat_id), set(args.sender_open_id)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded Feishu relay for isolated local workers")
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--chat-id", action="append", required=True)
    parser.add_argument("--sender-open-id", action="append", required=True)
    parser.add_argument("--worker", action="append", help="NAME, NAME=mock, or NAME=acp; repeatable")
    parser.add_argument("--db", type=Path, default=Path("runtime/relay.db"))
    parser.add_argument("--artifacts", type=Path, default=Path("runtime/artifacts"))
    parser.add_argument("--lark-cli", default="lark-cli")
    parser.add_argument("--acp-cwd", type=Path, help="Working directory for Kimi Code sessions")
    parser.add_argument("--acp-command", default="kimi")
    parser.add_argument("--acp-timeout", type=int, default=600)
    parser.add_argument("--timeout", help="For example 10m; omit only for an interactive long run")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()
    service = build_service(args)
    for event in service.cli.events(timeout=args.timeout, max_events=args.max_events):
        response = service.process_event(event)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
