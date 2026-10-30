from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Iterator

from .agent_store import ControlStore
from .commands import (
    HELP_TEXT, format_agent_result, format_goal, format_logs, format_natural_result,
    format_status, format_tasks, natural_task_envelope, parse_goal, parse_natural_command,
    split_task_argument,
)
from .core import Orchestrator
from .relay import RelayBridge, encode_relay_message
from .runtime import AgentRuntime
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
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                 allowed_chat_ids: set[str], allowed_sender_ids: set[str],
                 runtime: AgentRuntime | None = None):
        if not allowed_chat_ids or not allowed_sender_ids:
            raise ValueError("Both chat and sender allowlists are required")
        self.bridge = bridge
        self.cli = cli
        self.allowed_chat_ids = allowed_chat_ids
        self.allowed_sender_ids = allowed_sender_ids
        self.runtime = runtime

    def process_event(self, event: dict) -> dict | None:
        if event.get("chat_id") not in self.allowed_chat_ids:
            return None
        if event.get("sender_id") not in self.allowed_sender_ids:
            return None
        if event.get("message_type") != "text" or not event.get("message_id"):
            return None
        reply_target = event["message_id"]
        command = parse_natural_command(event.get("content", ""))
        if command:
            event_id = event.get("event_id") or reply_target
            if self.runtime:
                previous = self.runtime.store.command_response(event_id)
                if previous is not None:
                    return previous
            try:
                response, reply_text = self._process_command(event, command)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                response = {"kind": "command_error", "status": "failed", "error": detail}
                reply_text = f"❌ 命令失败\n{detail}"
            if self.runtime:
                self.runtime.store.save_command_response(event_id, response)
        else:
            response = self.bridge.handle_event(event)
            if response is None:
                return None
            reply_text = encode_relay_message(response)
        delivery_key = hashlib.sha256(
            f"relay-result:{event.get('event_id') or reply_target}".encode()
        ).hexdigest()[:32]
        self.cli.reply(reply_target, reply_text, delivery_key)
        return response

    def _process_command(self, event: dict, command: dict) -> tuple[dict, str]:
        name, instructions = command["command"], command["instructions"]
        if name == "help":
            return {"kind": "help", "status": "succeeded"}, HELP_TEXT
        if name in {"glm", "kimi", "mock"}:
            if not instructions:
                return {"kind": "help", "status": "succeeded"}, HELP_TEXT
            task_event = dict(event)
            task_event["content"] = encode_relay_message(
                natural_task_envelope(event, command, self.bridge.bridge_id)
            )
            response = self.bridge.handle_event(task_event)
            return response, format_natural_result(response)
        if self.runtime is None:
            raise ValueError("Long-running Agent runtime is not configured")
        store = self.runtime.store
        if name == "agent":
            if not instructions:
                raise ValueError("/agent 后需要任务内容")
            task, turn = self.runtime.create_task(instructions, reply_to_message_id=event["message_id"])
            response = {"kind": "accepted", "status": "queued", "task_id": task["task_id"],
                        "turn_id": turn["turn_id"], "worker_id": "agent"}
            text = (f"✅ 已创建长期任务\n任务：{task['task_id']}\nTurn：{turn['turn_id']}\n"
                    f"后续用 /continue {task['task_id']} 新指令 继续同一 Session。")
            return response, text
        if name == "continue":
            task_id, next_instruction = split_task_argument(instructions, require_instruction=True)
            turn = self.runtime.continue_task(task_id, next_instruction, event["message_id"])
            response = {"kind": "accepted", "status": "queued", "task_id": task_id,
                        "turn_id": turn["turn_id"], "worker_id": "agent"}
            return response, f"✅ 已加入队列\n任务：{task_id}\nTurn：{turn['turn_id']}\n恢复原 Session 后执行。"
        if name == "goal":
            if not instructions:
                raise ValueError("/goal 后需要目标内容或 JSON 计划")
            title, steps = parse_goal(instructions)
            goal = self.runtime.create_goal(title, steps)
            return {"kind": "goal", "status": goal["status"], "goal_id": goal["goal_id"]}, format_goal(goal)
        if name == "tasks":
            tasks = store.list_tasks()
            return {"kind": "tasks", "status": "succeeded", "tasks": tasks}, format_tasks(tasks)
        task_id, remainder = split_task_argument(instructions)
        if name == "status":
            if task_id.startswith("GOAL-"):
                text = format_goal(store.get_goal(task_id))
            else:
                text = format_status(store, task_id)
            return {"kind": "status", "status": "succeeded", "task_id": task_id}, text
        if name == "logs":
            logs = store.logs(task_id)
            return {"kind": "logs", "status": "succeeded", "task_id": task_id}, format_logs(logs)
        if name == "cancel":
            turn_ids = self.runtime.cancel(task_id)
            return {"kind": "cancel", "status": "cancelled", "task_id": task_id}, (
                f"已请求取消 {task_id}\n涉及 Turns：{', '.join(turn_ids) or '无运行中 Turn'}"
            )
        if name == "close":
            task = store.close_task(task_id)
            return {"kind": "close", "status": task["status"], "task_id": task_id}, f"已关闭 {task_id}，Session 映射保留。"
        if name in {"approve", "deny"}:
            approval = self.runtime.decide_approval(task_id, name == "approve", remainder)
            return {"kind": "approval", "status": approval["status"], "task_id": task_id}, (
                f"审批：{approval['approval_id']}\n决定：{approval['status']}\n任务：{task_id}"
            )
        raise ValueError(f"Unsupported command: {name}")

    def notify(self, event_type: str, data: dict) -> None:
        turn = data.get("turn", {})
        reply_target = turn.get("reply_to_message_id")
        if not reply_target:
            return
        if event_type == "turn_finished":
            result = data["result"]
            response = {
                **result, "task_id": data["task"]["task_id"], "worker_id": data["task"]["worker_id"],
                "error": turn.get("error") or result.get("error"),
            }
            text = format_agent_result(response)
        elif event_type == "approval":
            approval = data["approval"]
            request = json.loads(approval["request_json"])
            text = (
                f"🔐 等待权限审批\n任务：{data['task']['task_id']}\n审批：{approval['approval_id']}\n"
                f"操作：{request.get('title') or request.get('kind')}\n"
                f"输入：{json.dumps(request.get('raw_input'), ensure_ascii=False)[:2000]}\n\n"
                f"批准：/approve {data['task']['task_id']}\n拒绝：/deny {data['task']['task_id']} 原因"
            )
        else:
            return
        key = hashlib.sha256(f"{event_type}:{turn['turn_id']}:{approval['approval_id'] if event_type == 'approval' else ''}".encode()).hexdigest()[:32]
        self.cli.reply(reply_target, text, key)


def build_service(args: argparse.Namespace) -> LarkRelayService:
    from .acp_worker import KimiCodeWorker, PersistentKimiAgent
    from .workers import OpenAICompatibleWorker

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
        elif kind == "openai":
            if not args.openai_base_url or not args.openai_model:
                raise ValueError("--openai-base-url and --openai-model are required")
            workers[name] = OpenAICompatibleWorker(
                args.openai_base_url, args.openai_model,
                args.openai_api_key_env, args.openai_timeout,
            )
        else:
            raise ValueError(f"Unsupported worker type: {kind}")
    bridge = RelayBridge(args.bridge_id, controller, workers)
    roots = args.agent_root or ([args.acp_cwd] if args.acp_cwd else [Path.cwd()])
    store = ControlStore(args.db)
    agent = PersistentKimiAgent(store, args.acp_command, args.agent_timeout)
    service = LarkRelayService(bridge, LarkCLI(args.lark_cli), set(args.chat_id), set(args.sender_open_id))
    runtime = AgentRuntime(
        store, agent, roots, set(args.ssh_host or []), notifier=service.notify,
        max_workers=args.max_agent_workers, approval_timeout=args.approval_timeout,
    )
    service.runtime = runtime
    return service


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded Feishu relay for isolated local workers")
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--chat-id", action="append", required=True)
    parser.add_argument("--sender-open-id", action="append", required=True)
    parser.add_argument("--worker", action="append", help="NAME=mock|acp|openai; repeatable")
    parser.add_argument("--db", type=Path, default=Path("runtime/relay.db"))
    parser.add_argument("--artifacts", type=Path, default=Path("runtime/artifacts"))
    parser.add_argument("--lark-cli", default="lark-cli")
    parser.add_argument("--acp-cwd", type=Path, help="Working directory for Kimi Code sessions")
    parser.add_argument("--acp-command", default="kimi")
    parser.add_argument("--acp-timeout", type=int, default=600)
    parser.add_argument("--agent-root", type=Path, action="append", help="Authorized file/data root; repeatable")
    parser.add_argument("--ssh-host", action="append", help="Allowed SSH hostname; repeatable")
    parser.add_argument("--agent-timeout", type=int, default=7200)
    parser.add_argument("--approval-timeout", type=int, default=3600)
    parser.add_argument("--max-agent-workers", type=int, default=2)
    parser.add_argument("--openai-base-url", default=os.environ.get("WORKER_BASE_URL"))
    parser.add_argument("--openai-model", default=os.environ.get("WORKER_MODEL"))
    parser.add_argument("--openai-api-key-env", default="WORKER_API_KEY")
    parser.add_argument("--openai-timeout", type=int, default=600)
    parser.add_argument("--timeout", help="For example 10m; omit only for an interactive long run")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()
    service = build_service(args)
    try:
        for event in service.cli.events(timeout=args.timeout, max_events=args.max_events):
            response = service.process_event(event)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        if service.runtime:
            service.runtime.shutdown()


if __name__ == "__main__":
    main()
