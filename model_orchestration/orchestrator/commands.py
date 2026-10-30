from __future__ import annotations

import hashlib
import re

COMMAND_PATTERN = re.compile(r"(?<![\w/])/(glm|kimi|mock|help)(?:\s+|$)", re.IGNORECASE)
WORKER_ALIASES = {"glm": "glm", "kimi": "kimi_code", "mock": "mock"}
HELP_TEXT = """可用命令：
/glm 任务内容 — 调用 GLM
/kimi 任务内容 — 调用 Kimi Code
/mock 任务内容 — 调用 Mock 测试 Worker
/help — 显示帮助

命令前可以 @机器人，也可以直接输入。原有 JSON 任务格式仍然支持。"""


def parse_natural_command(content: str) -> dict | None:
    if not isinstance(content, str):
        return None
    match = COMMAND_PATTERN.search(content)
    if not match:
        return None
    command = match.group(1).lower()
    return {
        "command": command,
        "worker_id": WORKER_ALIASES.get(command),
        "instructions": content[match.end():].strip(),
    }


def natural_task_envelope(event: dict, command: dict, bridge_id: str) -> dict:
    identity = event.get("event_id") or event["message_id"]
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return {
        "protocol": "model-relay-v1",
        "kind": "task",
        "task_id": "TASK-" + digest[:16],
        "idempotency_key": "feishu-" + digest[:24],
        "target_bridge": bridge_id,
        "assigned_worker": command["worker_id"],
        "task_type": "generic",
        "role": "feishu_user",
        "input_ref": "feishu-message:" + event["message_id"],
        "instructions": command["instructions"],
        "reply_to_message_id": event["message_id"],
    }


def format_natural_result(response: dict) -> str:
    labels = {"succeeded": "✅ 执行成功", "blocked": "⚠️ 任务受阻", "failed": "❌ 执行失败"}
    lines = [
        labels.get(response["status"], "任务结果"),
        f"Worker：{response['worker_id']}",
        f"任务：{response['task_id']}",
        "",
        response["summary"],
    ]
    if response.get("unknowns"):
        lines.extend(["", "待确认：", *[f"- {item}" for item in response["unknowns"]]])
    if response.get("blocking_issues"):
        lines.extend(["", "阻塞问题：", *[f"- {item}" for item in response["blocking_issues"]]])
    return "\n".join(lines)
