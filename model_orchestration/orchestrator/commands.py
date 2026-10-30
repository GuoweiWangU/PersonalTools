from __future__ import annotations

import hashlib
import json
import re

COMMAND_PATTERN = re.compile(
    r"(?<![\w/])/(glm|kimi|mock|agent|continue|goal|tasks|status|logs|cancel|close|approve|deny|help)(?:\s+|$)",
    re.IGNORECASE,
)
WORKER_ALIASES = {"glm": "glm", "kimi": "kimi_code", "mock": "mock"}
HELP_TEXT = """可用命令：
/agent 任务内容 — 创建可执行的长期 Task（固定 Session）
/continue TASK-xxx 新指令 — 继续同一 Task 和 Session
/goal 目标内容 — 创建目标；也支持含 steps 的 JSON DAG
/tasks — 查看最近任务
/status TASK-xxx — 查看状态、Turn 和已验证产物
/logs TASK-xxx — 查看审计与 Worker 日志
/cancel TASK-xxx — 取消执行
/close TASK-xxx — 关闭长期 Task
/approve TASK-xxx — 批准当前高风险权限请求
/deny TASK-xxx 原因 — 拒绝权限请求
/glm 任务内容 — 调用 GLM 做独立分析
/kimi 任务内容 — 调用只读 Kimi Code
/mock 任务内容 — 链路测试
/help — 显示帮助

/agent 与 /continue 为异步执行，完成、阻塞或等待审批时会再次通知。"""


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


def split_task_argument(instructions: str, require_instruction: bool = False) -> tuple[str, str]:
    task_id, separator, remainder = instructions.strip().partition(" ")
    if not task_id:
        raise ValueError("缺少 TASK-xxx")
    if require_instruction and (not separator or not remainder.strip()):
        raise ValueError("缺少要继续执行的指令")
    return task_id, remainder.strip()


def parse_goal(instructions: str) -> tuple[str, list[dict]]:
    if instructions.startswith("{"):
        value = json.loads(instructions)
        title = value.get("title") or value.get("goal")
        steps = value.get("steps")
        if not isinstance(title, str) or not isinstance(steps, list):
            raise ValueError("Goal JSON 需要 title（或 goal）和 steps 数组")
        return title, steps
    return instructions, [{"id": "S1", "instructions": instructions, "depends_on": []}]


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
    labels = {"succeeded": "✅ 执行成功", "blocked": "⚠️ 任务受阻",
              "failed": "❌ 执行失败", "cancelled": "🛑 已取消"}
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
    if response.get("error") and response["error"] not in response.get("blocking_issues", []):
        lines.extend(["", "错误详情：", response["error"]])
    return "\n".join(lines)


def format_agent_result(response: dict) -> str:
    lines = [format_natural_result({**response, "worker_id": response.get("worker_id", "agent")})]
    artifacts = response.get("artifacts", [])
    valid = [item for item in artifacts if isinstance(item, dict) and item.get("valid")]
    if valid:
        lines.extend(["", "已校验产物：", *[
            f"- {item['path']}（{item['size_bytes']} bytes，SHA256 {item['sha256'][:12]}…）"
            for item in valid[:10]
        ]])
    return "\n".join(lines)


def format_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "暂无长期任务。"
    return "最近任务：\n" + "\n".join(
        f"- {task['task_id']}｜{task['status']}｜{task['turn_count']} Turns｜{task['updated_at']}"
        for task in tasks
    )


def format_status(store, task_id: str) -> str:
    task = store.get_task(task_id)
    turns = store.task_turns(task_id, limit=5)
    lines = [
        f"任务：{task_id}", f"状态：{task['status']}", f"Worker：{task['worker_id']}",
        f"目录：{task['cwd']}", f"Session：{task['session_id'] or '尚未创建'}",
        f"Goal：{task['goal_id'] or '无'}", "", "最近 Turns：",
    ]
    for turn in turns:
        lines.append(f"- {turn['turn_id']}｜{turn['status']}｜{turn.get('stop_reason') or '-'}")
        if turn.get("error"):
            lines.append(f"  错误：{turn['error']}")
    artifacts = store.artifacts(task_id)
    if artifacts:
        lines.extend(["", "产物："])
        lines.extend(
            f"- {'✓' if item['valid'] else '✗'} {item['path']}（{item['size_bytes']} bytes）"
            for item in artifacts[-10:]
        )
    pending = store.pending_approval(task_id)
    if pending:
        lines.extend(["", f"等待审批：{pending['approval_id']}"])
    return "\n".join(lines)


def format_logs(logs: list[dict], limit: int = 12000) -> str:
    text = "\n".join(f"[{item['created_at']}] {item['event_type']}: {item['message']}" for item in logs)
    return ("最近日志：\n" + text)[:limit] if text else "暂无日志。"


def format_goal(goal: dict) -> str:
    lines = [f"目标：{goal['goal_id']}", f"状态：{goal['status']}", goal["title"], "", "步骤："]
    for step in goal["steps"]:
        lines.append(
            f"- {step['step_id']}｜{step['status']}｜{step['task_id'] or '未调度'}｜{step['instructions']}"
        )
    return "\n".join(lines)
